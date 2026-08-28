"""Application configuration from environment variables and .env files.

Loads application configuration from environment variables and .env files.
Set the DEPLOYMENT environment variable to select .env.${DEPLOYMENT}.

Notes
-----
Configuration is exposed through ``get_settings()``. Consumers should import
and call that function rather than a module-level object, keeping configuration
access explicit and lazy:

    from fact_inventory.lib.settings import get_settings

    uri = get_settings().database_uri
    app_name = get_settings().app_name

Configuration is sourced from environment variables with optional override
from .env files. The following configuration parameters are supported:

**Application Identity**

APP_NAME
    Application name used in logs and OpenAPI documentation.
    Default: "fact_inventory".

APP_PREFIX
    URL prefix for all application routes. In production, set this to "/" since
    the reverse proxy strips the external prefix (e.g., "/fact_inventory").
    For development direct access to the app, set it to "/" + APP_NAME.
    Default: "/".

VERSION
    Application version from package metadata, git commit, or fallback.
    Default: "unknown".

DEPLOYMENT
    Selects .env.{DEPLOYMENT} file and sets deployment.environment.
    Required. Valid: any string identifier.

**Database & Connection Pooling**

DATABASE_URI
    The database connection string (required). Default: None.

DB_POOL_SIZE
    The database connection pool size. Default: 10. Valid: >=1.

DB_POOL_MAX_OVERFLOW
    Maximum connections beyond pool size. Default: 5. Valid: >=0.

DB_POOL_TIMEOUT
    Seconds to wait for a connection from the pool. Default: 30. Valid: >=1.

DB_POOL_RECYCLE_SECONDS
    Seconds before a pooled connection is recycled. Lower this if an
    intermediate firewall or load balancer silently drops idle connections
    before this interval elapses. Default: 3600. Valid: 60-86400.
    PostgreSQL only; ignored for SQLite.

DB_STATEMENT_TIMEOUT_MS
    Milliseconds before PostgreSQL aborts a running statement on any
    connection from this application. Applied via asyncpg server_settings at
    connection startup, so it covers every query (requests and background
    jobs), not just retention deletes. Default: 60000. Valid: 0-3600000.
    0 disables the timeout (PostgreSQL default: no limit).
    PostgreSQL only; ignored for SQLite.

**Rate Limiting**

API_RATE_LIMIT_UNIT
    Unit over which API requests are rate limited. Default: "hour".
    Valid: "day", "hour", "minute", "second".

API_RATE_LIMIT_MAX_REQUESTS
    Maximum API requests allowed within the rate limit window. Default: 2.
    Valid: >=1.

API_RATE_LIMIT_HEADERS
    Enable ratelimit-* headers in HTTP responses. Default: True.
    Set to False to suppress rate limit headers for client compatibility.

**Data Retention & Cleanup**

ENABLE_RETENTION_CLEANUP_JOB
    Enable the retention cleanup background job. Default: True.

RETENTION_DAYS
    Days before a record expires. Default: 400. Valid: >=1.

RETENTION_CHECK_INTERVAL_HOURS
    Hours between successive retention checks. Default: 20. Valid: >=1.

RETENTION_CHECK_JITTER_MINUTES
    Maximum random offset (minutes) added to each retention check sleep cycle.
    Default: 200. Valid: >=0.

ENABLE_HISTORY_CLEANUP_JOB
    Enable the history cleanup background job. Default: True.

HISTORY_CHECK_INTERVAL_HOURS
    Hours between successive history cleanup checks. Default: 20. Valid: >=1.

HISTORY_MAX_ENTRIES
    Maximum fact records to keep per client_address. Oldest records are
    deleted when exceeded. Default: 5. Valid: >=1.

HISTORY_CHECK_JITTER_MINUTES
    Maximum random offset (minutes) added to each history cleanup sleep cycle.
    Default: 200. Valid: >=0.

**Payload Constraints**

MAX_JSON_FIELD_MB
    Maximum size (MB) for a single JSON field. Default: 4. Valid: >=1.

MAX_REQUEST_BODY_MB
    Maximum total HTTP request body size (MB) enforced at the HTTP layer.
    Default: 17. Valid: > len(JSON_FIELDS) x MAX_JSON_FIELD_MB.

**Logging & Observability**

DEBUG
    Enable debug mode (forces LOG_LEVEL to DEBUG and enables OpenAPI docs).
    Default: False.

LOG_LEVEL
    Minimum log level to emit. Default: "INFO".
    Valid: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL".

ENABLE_METRICS
    Enable Prometheus /metrics endpoint and middleware.
    OpenTelemetry tracing is always active. Default: False.

**Health & Readiness Probes**

ENABLE_HEALTH_ENDPOINT
    Enable /health liveness probe endpoint. Default: False.

ENABLE_READY_ENDPOINT
    Enable /ready readiness probe endpoint. Default: False.

**Development Server (uvicorn)**

HOST
    Host to bind to. Default: "localhost".

PORT
    Port to bind to. Default: 8000.
"""

import contextlib
import functools
import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fact_inventory.domain.validation.size import JsonPayloadSizeValidator

__all__ = ["get_settings"]

#: Deployment name selected via the DEPLOYMENT environment variable. ``None``
#: means no deployment was selected; validation is deferred until the settings
#: object is actually used. This keeps the module importable for documentation
#: builds, CLI help, and tooling that does not need runtime config.
DEPLOYMENT = os.environ.get("DEPLOYMENT")
_ENV_FILE = Path(f".env.{DEPLOYMENT}") if DEPLOYMENT is not None else None

#: Valid values for Settings.api_rate_limit_unit field.
DurationUnit = Literal["second", "minute", "hour", "day"]


def _expand_env_vars_in_string(value: str) -> str:
    """Expand environment variables in a string.

    Supports ${VAR} and ${VAR:default} syntax where VAR is expanded from
    environment variables. If VAR is not set and a default is provided,
    the default value is used. If VAR is not set and no default is provided,
    the original ${VAR} pattern is left unchanged.

    Parameters
    ----------
    value : str
        String potentially containing ${VAR} or ${VAR:default} patterns.

    Returns
    -------
    str
        String with environment variables expanded.
    """
    pattern = r"\$\{([^:}]+)(?::([^}]*))?\}"

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)
        env_value = os.environ.get(var_name)

        if env_value is not None:
            return env_value
        if default_value is not None:
            return default_value
        return match.group(0)

    return re.sub(pattern, _replace, value)


def _get_version(package_name: str) -> str:
    """Determine the application version from installed package metadata or git.

    Attempts to resolve version from three sources in priority order:

    1. Installed package metadata (importlib.metadata)
    2. Current git commit short-hash (git rev-parse --short HEAD)
    3. Fallback literal string "unknown"

    Parameters
    ----------
    package_name : str
        Name of the installed package to query for version metadata.

    Returns
    -------
    str
        Version string from metadata, git commit hash, or "unknown" fallback.
    """
    with contextlib.suppress(PackageNotFoundError):
        return _package_version(package_name)

    git = shutil.which("git")
    if git is None:
        return "unknown"

    try:
        result = subprocess.run(  # noqa: S603
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return f"git-{result.stdout.strip()}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "git-unknown"


# ----------------------------------------------------------------------
# Settings model - reads from environment and .env file
# ----------------------------------------------------------------------
class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file.

    See fact_inventory/lib/settings.py module docstring for the complete
    list of configuration parameters and their defaults.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE is not None else None,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(
        default="fact_inventory",
        description="Application name used in logs and OpenAPI documentation.",
    )
    app_prefix: str = Field(
        default="/",
        description="URL prefix for all application routes.",
    )
    version: str = Field(
        default="unknown",
        description=(
            "Application version from package metadata, git commit, or fallback."
        ),
    )
    deployment_environment: str | None = Field(
        default=DEPLOYMENT,
        alias="DEPLOYMENT",
        description="Selects .env.{DEPLOYMENT} file and sets deployment.environment.",
    )

    database_uri: str | None = Field(
        default=None,
        description="The database connection string (required).",
    )
    db_pool_size: int = Field(
        default=10,
        ge=1,
        description="The database connection pool size.",
    )
    db_pool_max_overflow: int = Field(
        default=5,
        ge=0,
        description="Maximum connections beyond pool size.",
    )
    db_pool_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds to wait for a connection from the pool.",
    )
    db_pool_recycle_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description=(
            "Seconds before a pooled connection is recycled. PostgreSQL only."
        ),
    )
    db_statement_timeout_ms: int = Field(
        default=60000,
        ge=0,
        le=3_600_000,
        description=(
            "Milliseconds before PostgreSQL aborts a running statement on any"
            " connection from this application. 0 disables the timeout."
            " PostgreSQL only."
        ),
    )

    api_rate_limit_unit: DurationUnit = Field(
        default="hour",
        description="Unit over which API requests are rate limited.",
    )
    api_rate_limit_max_requests: int = Field(
        default=2,
        ge=1,
        description="Maximum API requests allowed within the rate limit window.",
    )
    api_rate_limit_headers: bool = Field(
        default=True,
        description="Enable ratelimit-* headers in responses.",
    )

    enable_retention_cleanup_job: bool = Field(
        default=True,
        description="Enable the retention cleanup background job.",
    )
    retention_days: int = Field(
        default=400,
        ge=1,
        description="Days before a record expires.",
    )
    retention_check_interval_hours: int = Field(
        default=20,
        ge=1,
        description="Hours between successive retention checks.",
    )
    retention_check_jitter_minutes: int = Field(
        default=200,
        ge=0,
        description="Maximum random offset (minutes) added to each retention check.",
    )

    enable_history_cleanup_job: bool = Field(
        default=True,
        description="Enable the history cleanup background job.",
    )
    history_check_interval_hours: int = Field(
        default=20,
        ge=1,
        description="Hours between successive history cleanup checks.",
    )
    history_max_entries: int = Field(
        default=5,
        ge=1,
        description="Maximum fact records to keep per client_address.",
    )
    history_check_jitter_minutes: int = Field(
        default=200,
        ge=0,
        description=(
            "Maximum random offset (minutes) added to each history cleanup check."
        ),
    )

    max_json_field_mb: int = Field(
        default=4,
        ge=1,
        description="Maximum size (MB) for a single JSON field.",
    )
    max_request_body_mb: int = Field(
        default=17,
        ge=1,
        description=(
            "Maximum total HTTP request body size (MB) enforced at the HTTP layer."
        ),
    )

    debug: bool = Field(
        default=False,
        description=(
            "Enable debug mode (forces LOG_LEVEL to DEBUG and enables OpenAPI docs)."
        ),
    )
    log_level: str = Field(
        default="INFO",
        description="Minimum log level to emit.",
    )

    enable_metrics: bool = Field(
        default=False,
        description="Enable Prometheus /metrics endpoint and middleware.",
    )
    enable_health_endpoint: bool = Field(
        default=False,
        description="Enable /health liveness probe endpoint.",
    )
    enable_ready_endpoint: bool = Field(
        default=False,
        description="Enable /ready readiness probe endpoint.",
    )

    @model_validator(mode="after")
    def _expand_env_vars(self) -> Self:
        """Expand shell-style environment variables in string fields.

        Supports ${VAR} and ${VAR:default} syntax. For ${VAR:default}, if VAR
        is not set, uses 'default' as fallback value. This runs first so other
        validators see the fully expanded values.

        Returns
        -------
        Self
            The validated Settings instance with expanded environment variables.
        """
        for field_name in self.__class__.model_fields:
            value = getattr(self, field_name)
            if isinstance(value, str):
                expanded = _expand_env_vars_in_string(value)
                setattr(self, field_name, expanded)
        return self

    @model_validator(mode="after")
    def _require_deployment(self) -> Self:
        """Verify DEPLOYMENT was selected.

        This check runs early so a missing DEPLOYMENT variable produces a clear,
        specific error before other validations.

        Returns
        -------
        Self
            The validated Settings instance.

        Raises
        ------
        ValueError
            If DEPLOYMENT is unset.
        """
        if self.deployment_environment is None:
            raise ValueError(  # noqa: TRY003
                "DEPLOYMENT environment variable is not set. "
                "Set it to your deployment name before starting the application, "
                "e.g.: export DEPLOYMENT=production"
            )
        return self

    @model_validator(mode="after")
    def _check_database_uri(self) -> Self:
        """Verify database_uri is set at runtime.

        This check runs after DEPLOYMENT is resolved so the correct .env file
        can be used if provided.

        Returns
        -------
        Self
            The validated Settings instance.

        Raises
        ------
        ValueError
            If database_uri is not set.
        """
        if self.database_uri is None:
            raise ValueError(  # noqa: TRY003
                "DATABASE_URI is required. "
                "Set it to a valid database connection string."
            )
        return self

    @model_validator(mode="after")
    def _resolve_version(self) -> Self:
        """Resolve version from package metadata if not explicitly set.

        Attempts to determine the application version from importlib.metadata
        using the app_name configuration value. If the package is not installed,
        falls back to git commit hash detection.

        Returns
        -------
        Self
            The validated Settings instance with version field populated.
        """
        if self.version == "unknown":
            self.version = _get_version(self.app_name)
        return self

    @model_validator(mode="after")
    def _check_app_prefix(self) -> Self:
        """Verify app_prefix starts with '/'.

        Returns
        -------
        Self
            The validated Settings instance.

        Raises
        ------
        ValueError
            If app_prefix does not start with '/'.
        """
        if not self.app_prefix.startswith("/"):
            raise ValueError(  # noqa: TRY003
                f"APP_PREFIX must start with '/': '{self.app_prefix}'"
            )
        return self

    @model_validator(mode="after")
    def _check_body_size(self) -> Self:
        """Verify the request body limit can hold the JSON fields and envelope.

        The request body must be strictly larger than the JSON fields combined
        so there is room for the surrounding JSON envelope and other request
        overhead. Requiring max_request_body_mb > N * max_json_field_mb preserves
        this invariant regardless of the chosen field size, where N is the number
        of JSON field names defined in JsonPayloadSizeValidator.

        Returns
        -------
        Self
            The validated Settings instance.

        Raises
        ------
        ValueError
            If max_request_body_mb is not greater than N x max_json_field_mb.
        """
        num_json_fields = len(JsonPayloadSizeValidator.JSON_FIELD_NAMES)
        if self.max_request_body_mb <= num_json_fields * self.max_json_field_mb:
            raise ValueError(  # noqa: TRY003
                f"max_request_body_mb ({self.max_request_body_mb}) must be greater"
                f" than {num_json_fields} x max_json_field_mb"
                f" ({num_json_fields * self.max_json_field_mb})"
            )
        return self


# ----------------------------------------------------------------------
# Application-wide settings singleton
# ----------------------------------------------------------------------
@functools.lru_cache
def get_settings() -> Settings:
    """Return the application settings singleton.

    The settings object is constructed lazily on first call and then cached.
    Callers should import this function rather than a module-level object,
    keeping configuration access explicit.
    """
    return Settings()
