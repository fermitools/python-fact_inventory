"""Database configuration for the fact_inventory server.

Provides SQLAlchemyAsyncConfig setup and engine configuration.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
)
from advanced_alchemy.extensions.litestar.plugins.init.config.engine import EngineConfig

__all__ = ["create_sqlalchemy_config"]


def create_sqlalchemy_config(
    database_uri: str,
    *,  # keyword-only
    debug: bool,
    pool_settings: Mapping[str, int],
) -> SQLAlchemyAsyncConfig:
    """Create SQLAlchemyAsyncConfig with engine settings.

    Parameters
    ----------
    database_uri : str
        Database connection string.
    debug : bool
        Enable SQL echo for debugging.
    pool_settings : dict
        Pool configuration with keys: pool_size, pool_recycle, max_overflow,
        pool_timeout, statement_timeout_ms.

    Returns
    -------
    SQLAlchemyAsyncConfig
        Configured SQLAlchemy plugin configuration.
    """
    parsed = urlparse(database_uri)

    engine_config = EngineConfig(echo=debug)
    if parsed.scheme and "postgresql" in str(parsed.scheme):
        engine_config = EngineConfig(
            pool_size=pool_settings["pool_size"],
            pool_recycle=pool_settings["pool_recycle"],
            max_overflow=pool_settings["max_overflow"],
            pool_timeout=pool_settings["pool_timeout"],
            pool_pre_ping=True,
            echo=debug,
            connect_args=_postgresql_connect_args(pool_settings),
        )

    return SQLAlchemyAsyncConfig(
        engine_config=engine_config,
        connection_string=database_uri,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        create_all=False,
    )


def _postgresql_connect_args(pool_settings: Mapping[str, int]) -> dict[str, Any]:
    """Build asyncpg connect_args applying a PostgreSQL statement timeout.

    Setting ``statement_timeout`` via asyncpg's ``server_settings`` applies it
    as a connection startup parameter, so PostgreSQL enforces it for the
    lifetime of every physical connection -- including ones opened after
    ``pool_recycle`` -- without any per-query or per-transaction code. A
    value of 0 maps to PostgreSQL's own "no timeout" semantics.

    Parameters
    ----------
    pool_settings : dict
        Pool configuration containing the key ``statement_timeout_ms``.

    Returns
    -------
    dict[str, Any]
        ``connect_args`` suitable for ``EngineConfig``.
    """
    timeout_ms = pool_settings["statement_timeout_ms"]
    return {"server_settings": {"statement_timeout": str(timeout_ms)}}
