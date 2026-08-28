"""Tests for application settings and configuration management.

Covers:
- Version detection from git or package metadata
- Settings validation (body sizes, required fields)
- Environment variable handling (DATABASE_URI, APP_PREFIX, DEPLOYMENT)
- Integration with app_factory.create_app()

Design Notes:
- Version detection tests use mocks to avoid depending on system git availability
- Body size validation tests use boundary testing (min/max/off-by-one)
- Subprocess-based tests ensure DEPLOYMENT env var is truly required at import time
"""

import subprocess
import sys
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

import pytest

from fact_inventory.lib.settings import (
    Settings,
    _expand_env_vars_in_string,
    _get_version,
    get_settings,
)

TIMEOUT_SECONDS = 5
TEST_PACKAGE_NAME = "test-package"
TEST_DATABASE_URI = "sqlite+aiosqlite:///:memory:"
POSTGRES_URI = "postgresql://user:pass@localhost:5432/db"
DEFAULT_SETTINGS_KWARGS = {"database_uri": TEST_DATABASE_URI}


def build_settings(**overrides) -> Settings:
    """Create a Settings instance with a default test database URI."""
    values = {**DEFAULT_SETTINGS_KWARGS, **overrides}
    return Settings(**values)


def assert_import_requires_deployment(stderr: str) -> None:
    """Assert the settings instantiation failure explains the missing DEPLOYMENT."""
    assert "DEPLOYMENT" in stderr
    assert "export DEPLOYMENT=" in stderr


@contextmanager
def mocked_git_detection(
    return_stdout: str | None = None,
    side_effect: Exception | None = None,
    *,
    git_on_path: bool = False,
):
    """Mock git detection for version string testing."""
    if return_stdout is not None and git_on_path:
        raise ValueError("Specify either return_stdout or git_on_path=True, not both")
    if side_effect is not None and return_stdout is None and not git_on_path:
        raise ValueError(
            "side_effect requires git on PATH; provide return_stdout or git_on_path=True"
        )
    git_available = return_stdout is not None or git_on_path
    git_path = None
    if git_available:
        git_path = "/usr/bin/git"
    # Always miss installed package metadata so _get_version() falls back to git.
    with (
        patch(
            "fact_inventory.lib.settings._package_version",
            side_effect=PackageNotFoundError,
        ),
        patch("fact_inventory.lib.settings.shutil.which", return_value=git_path),
    ):
        if git_available:
            # Mock subprocess.run to return git hash
            mock_result = MagicMock(stdout=return_stdout)
            with patch(
                "fact_inventory.lib.settings.subprocess.run",
                return_value=mock_result,
                side_effect=side_effect,
            ) as mock_run:
                yield mock_run
        else:
            yield None


def test_get_version_package_metadata_takes_precedence() -> None:
    """Installed package metadata wins without consulting git."""
    with (
        patch(
            "fact_inventory.lib.settings._package_version",
            return_value="1.2.3",
        ),
        patch("fact_inventory.lib.settings.shutil.which") as mock_which,
        patch("fact_inventory.lib.settings.subprocess.run") as mock_run,
    ):
        assert _get_version(TEST_PACKAGE_NAME) == "1.2.3"

    mock_which.assert_not_called()
    mock_run.assert_not_called()


def test_get_version_git_not_on_path_returns_unknown() -> None:
    """Version detection returns 'unknown' when git is not available on PATH."""
    with mocked_git_detection(return_stdout=None):
        assert _get_version(TEST_PACKAGE_NAME) == "unknown"


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("abc1234\n", "git-abc1234"),
        ("  def5678  \n\n", "git-def5678"),
        ("1a2b3c4d\n", "git-1a2b3c4d"),
    ],
)
def test_get_version_git_command_success(stdout: str, expected: str) -> None:
    """Version detection returns 'git-<hash>' from git rev-parse output."""
    with mocked_git_detection(return_stdout=stdout):
        assert _get_version(TEST_PACKAGE_NAME) == expected


@pytest.mark.parametrize(
    "side_effect,expected_version",
    [
        (subprocess.CalledProcessError(128, "git"), "git-unknown"),
        (subprocess.TimeoutExpired("git", TIMEOUT_SECONDS), "git-unknown"),
    ],
)
def test_get_version_errors_return_unknown(side_effect, expected_version: str) -> None:
    """Git errors (CalledProcessError, TimeoutExpired) return 'git-unknown'."""
    with mocked_git_detection(git_on_path=True, side_effect=side_effect):
        assert _get_version(TEST_PACKAGE_NAME) == expected_version


def test_get_version_uses_short_hash_flag() -> None:
    """Git command uses 'git rev-parse --short HEAD' for short hash."""
    with mocked_git_detection(return_stdout="1a2b3c4d\n") as mock_run:
        _get_version(TEST_PACKAGE_NAME)
        assert mock_run.call_args.args[0][1:] == ["rev-parse", "--short", "HEAD"]


def test_get_version_subprocess_call_includes_timeout() -> None:
    """Git subprocess call includes timeout to prevent hanging."""
    with mocked_git_detection(return_stdout="hash\n") as mock_run:
        _get_version(TEST_PACKAGE_NAME)
        assert mock_run.call_args[1]["timeout"] == TIMEOUT_SECONDS


def test_settings_body_size_valid_accepted() -> None:
    """max_request_body_mb=17 is accepted when max_json_field_mb=4 (17 > 4*4=16)."""
    s = build_settings(
        max_json_field_mb=4,
        max_request_body_mb=17,
    )
    assert s.max_request_body_mb == 17
    assert s.max_json_field_mb == 4


@pytest.mark.parametrize("body_mb", [15, 16])
def test_settings_body_size_invalid_rejected(body_mb: int) -> None:
    """max_request_body_mb <= 4*max_json_field_mb is rejected."""
    with pytest.raises(ValueError):
        build_settings(
            max_json_field_mb=4,
            max_request_body_mb=body_mb,
        )


@pytest.mark.parametrize("pool_recycle_seconds", [60, 3600, 86400])
def test_settings_db_pool_recycle_seconds_valid_accepted(
    pool_recycle_seconds: int,
) -> None:
    """db_pool_recycle_seconds within [60, 86400] is accepted."""
    s = build_settings(db_pool_recycle_seconds=pool_recycle_seconds)
    assert s.db_pool_recycle_seconds == pool_recycle_seconds


@pytest.mark.parametrize("pool_recycle_seconds", [0, 59, 86401])
def test_settings_db_pool_recycle_seconds_invalid_rejected(
    pool_recycle_seconds: int,
) -> None:
    """db_pool_recycle_seconds outside [60, 86400] is rejected."""
    with pytest.raises(ValueError):
        build_settings(db_pool_recycle_seconds=pool_recycle_seconds)


@pytest.mark.parametrize("statement_timeout_ms", [0, 60000, 3_600_000])
def test_settings_db_statement_timeout_ms_valid_accepted(
    statement_timeout_ms: int,
) -> None:
    """db_statement_timeout_ms within [0, 3600000] is accepted."""
    s = build_settings(db_statement_timeout_ms=statement_timeout_ms)
    assert s.db_statement_timeout_ms == statement_timeout_ms


@pytest.mark.parametrize("statement_timeout_ms", [-1, 3_600_001])
def test_settings_db_statement_timeout_ms_invalid_rejected(
    statement_timeout_ms: int,
) -> None:
    """db_statement_timeout_ms outside [0, 3600000] is rejected."""
    with pytest.raises(ValueError):
        build_settings(db_statement_timeout_ms=statement_timeout_ms)


def test_settings_version_explicit_overrides_detection() -> None:
    """Explicit version passed to Settings is used; _get_version is not called."""
    with patch("fact_inventory.lib.settings._get_version") as mock_get_version:
        s = build_settings(
            app_name="any-package",
            version="custom-1.2.3",
        )
        assert s.version == "custom-1.2.3"
        mock_get_version.assert_not_called()


def test_settings_version_auto_detected_uses_configured_app_name() -> None:
    """Unknown versions are resolved via _get_version(app_name)."""
    with patch(
        "fact_inventory.lib.settings._get_version",
        return_value="1.2.3",
    ) as mock_get_version:
        s = build_settings(app_name="fact-inventory")

    assert s.version == "1.2.3"
    mock_get_version.assert_called_once_with("fact-inventory")


def test_settings_version_stores_get_version_fallback_value() -> None:
    """Settings stores the fallback value returned by _get_version()."""
    with patch(
        "fact_inventory.lib.settings._get_version",
        return_value="git-abc1234",
    ) as mock_get_version:
        s = build_settings(app_name="nonexistent-package-xyz-12345")

    assert s.version == "git-abc1234"
    mock_get_version.assert_called_once_with("nonexistent-package-xyz-12345")


def test_settings_defaults_has_version() -> None:
    """Default settings include non-empty version string."""
    assert len(get_settings().version) > 0


def test_settings_defaults_has_app_name() -> None:
    """Default app_name is 'fact_inventory'."""
    assert get_settings().app_name == "fact_inventory"


def test_settings_defaults_has_app_prefix() -> None:
    """Default app_prefix is '/fact_inventory' (set by .env.testing for tests)."""
    # In testing, APP_PREFIX is set to /fact_inventory in .env.testing
    assert get_settings().app_prefix == "/fact_inventory"


def test_settings_requires_deployment() -> None:
    """Settings raises ValueError when DEPLOYMENT env var is missing."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fact_inventory.lib.settings import get_settings; get_settings()",
        ],
        env={},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert_import_requires_deployment(result.stderr)


@pytest.mark.parametrize(
    "prefix",
    ["/", "/my_service", "/fact_inventory"],
)
def test_settings_app_prefix(prefix: str) -> None:
    """Explicit app_prefix constructor argument is stored verbatim."""
    s = build_settings(app_prefix=prefix)
    assert s.app_prefix == prefix


@pytest.mark.parametrize(
    "prefix",
    ["", "test", "fact_inventory", "api/v1"],
)
def test_settings_app_prefix_invalid_rejected(prefix: str) -> None:
    """app_prefix not starting with '/' is rejected."""
    with pytest.raises(ValueError, match="APP_PREFIX must start with '/'"):
        build_settings(app_prefix=prefix)


def test_settings_explicit_app_name_is_preserved() -> None:
    """app_name is stored verbatim when provided explicitly."""
    s = build_settings(
        app_prefix="/my_service",
        app_name="my_service",
    )
    assert s.app_name == "my_service"


@pytest.mark.parametrize(
    "attr",
    ["debug", "enable_retention_cleanup_job", "enable_history_cleanup_job"],
)
def test_settings_defaults_valid_types(attr: str) -> None:
    """Default boolean settings are bool type."""
    assert isinstance(getattr(get_settings(), attr), bool)


def test_settings_log_level_is_valid_string() -> None:
    """Default log_level is a recognised Python logging level name."""
    assert get_settings().log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@pytest.mark.parametrize("database_uri", [POSTGRES_URI, TEST_DATABASE_URI])
def test_settings_stores_explicit_database_uri(database_uri: str) -> None:
    """Settings stores an explicitly provided database_uri verbatim."""
    s = build_settings(database_uri=database_uri)
    assert s.database_uri == database_uri


def test_expand_env_vars_in_string_with_existing_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_expand_env_vars_in_string expands ${VAR} to environment value."""
    monkeypatch.setenv("USER", "alice")
    result = _expand_env_vars_in_string("sqlite://${USER}_test")
    assert result == "sqlite://alice_test"


def test_expand_env_vars_in_string_with_multiple_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_expand_env_vars_in_string expands multiple ${VAR} in one string."""
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("ENV", "testing")
    result = _expand_env_vars_in_string("path/${USER}/${ENV}/data")
    assert result == "path/alice/testing/data"


def test_expand_env_vars_in_string_with_default() -> None:
    """_expand_env_vars_in_string uses default for missing variables."""
    result = _expand_env_vars_in_string("path/${MISSING:default_user}/file")
    assert result == "path/default_user/file"


def test_expand_env_vars_in_string_without_default() -> None:
    """_expand_env_vars_in_string leaves unmatched patterns unchanged."""
    result = _expand_env_vars_in_string("path/${MISSING}/file")
    assert result == "path/${MISSING}/file"


def test_settings_expands_env_vars_in_database_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings.database_uri expands environment variables."""
    monkeypatch.setenv("USER", "testuser")
    s = build_settings(
        database_uri="sqlite+aiosqlite:///${USER}:memory:",
    )
    assert s.database_uri == "sqlite+aiosqlite:///testuser:memory:"


def test_settings_requires_database_uri() -> None:
    """Settings raises ValueError if DATABASE_URI is not set."""
    with pytest.raises(ValueError):
        Settings(database_uri=None)


def test_settings_requires_deployment_directly() -> None:
    """Settings raises ValueError when deployment_environment is None."""
    with pytest.raises(ValueError, match="DEPLOYMENT environment variable is not set"):
        Settings(deployment_environment=None, database_uri=TEST_DATABASE_URI)
