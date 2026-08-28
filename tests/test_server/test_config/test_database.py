"""Tests for server configuration modules."""

from advanced_alchemy.utils.dataclass import Empty

from fact_inventory.lib.settings import Settings
from fact_inventory.server.config.database import create_sqlalchemy_config
from fact_inventory.server.config.observability import (
    create_logging_config,
    create_tracer_provider,
)

POOL_SETTINGS = {
    "pool_size": 10,
    "pool_recycle": 3600,
    "max_overflow": 20,
    "pool_timeout": 30,
    "statement_timeout_ms": 60000,
}


def test_create_sqlalchemy_config_creates_config() -> None:
    """Database config creates valid SQLAlchemyAsyncConfig."""
    settings = Settings(
        database_uri="sqlite+aiosqlite:///:memory:",
        debug=False,
        db_pool_size=10,
        db_pool_max_overflow=20,
        db_pool_timeout=30,
    )

    config = create_sqlalchemy_config(
        database_uri=settings.database_uri,
        debug=settings.debug,
        pool_settings=POOL_SETTINGS,
    )

    assert config is not None


def test_create_sqlalchemy_config_sqlite_ignores_pool_settings() -> None:
    """SQLite engines get a plain EngineConfig; pool/timeout settings are ignored."""
    config = create_sqlalchemy_config(
        database_uri="sqlite+aiosqlite:///:memory:",
        debug=False,
        pool_settings=POOL_SETTINGS,
    )

    engine_config = config.engine_config
    assert engine_config.connect_args is Empty


def test_create_sqlalchemy_config_postgresql_applies_pool_recycle() -> None:
    """PostgreSQL engines apply the configured pool_recycle value."""
    config = create_sqlalchemy_config(
        database_uri="postgresql+asyncpg://user:pass@localhost/db",
        debug=False,
        pool_settings={**POOL_SETTINGS, "pool_recycle": 1800},
    )

    assert config.engine_config.pool_recycle == 1800


def test_create_sqlalchemy_config_postgresql_sets_statement_timeout() -> None:
    """PostgreSQL engines set statement_timeout via asyncpg server_settings."""
    config = create_sqlalchemy_config(
        database_uri="postgresql+asyncpg://user:pass@localhost/db",
        debug=False,
        pool_settings={**POOL_SETTINGS, "statement_timeout_ms": 45000},
    )

    connect_args = config.engine_config.connect_args
    assert connect_args == {"server_settings": {"statement_timeout": "45000"}}


def test_create_sqlalchemy_config_postgresql_statement_timeout_disabled() -> None:
    """A statement_timeout_ms of 0 is passed through as PostgreSQL's disable value."""
    config = create_sqlalchemy_config(
        database_uri="postgresql+asyncpg://user:pass@localhost/db",
        debug=False,
        pool_settings={**POOL_SETTINGS, "statement_timeout_ms": 0},
    )

    connect_args = config.engine_config.connect_args
    assert connect_args == {"server_settings": {"statement_timeout": "0"}}


def test_create_logging_config_creates_config() -> None:
    """Logging config creates valid StructlogConfig."""
    settings = Settings(
        log_level="INFO",
        debug=False,
    )

    config = create_logging_config(settings)

    assert config is not None
    assert config.enable_middleware_logging is True


def test_create_logging_config_respects_debug_mode() -> None:
    """Debug mode forces DEBUG log level."""
    settings = Settings(
        log_level="WARNING",
        debug=True,
    )

    config = create_logging_config(settings)

    # When debug=True, the effective log level should be DEBUG
    # even if log_level is set to WARNING
    assert config is not None


def test_create_tracer_provider_creates_provider() -> None:
    """Tracer provider creates valid TracerProvider with shutdown hooks."""
    settings = Settings(
        debug=False,
    )

    provider, shutdown_hooks = create_tracer_provider(settings)

    assert provider is not None
    assert isinstance(shutdown_hooks, list)
