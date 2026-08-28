"""Litestar application and client fixtures for testing."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.plugins.opentelemetry import OpenTelemetryConfig
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession

from fact_inventory.lib.logging import get_structlog_config
from fact_inventory.lib.settings import Settings, get_settings
from fact_inventory.presentation.router import create_router
from fact_inventory.server.app import create_app
from tests.support.db import truncate_fact_inventory

__all__ = [
    "clean_test_client",
    "cleanup_app_database",
    "client_factory",
    "client_with_custom_router_path",
    "client_with_health",
    "client_with_health_and_ready",
    "client_with_metrics",
    "client_with_ready",
    "create_router_test_app",
    "get_sqlalchemy_config",
    "test_app",
    "test_client",
    "test_db_session",
]
CUSTOM_ROUTER_PATH = "/fact_inventory"


def create_router_test_app(
    path: str,
    settings: Settings,
) -> Litestar:
    """Create a minimal app for router-composition tests."""
    alchemy_config = SQLAlchemyAsyncConfig(
        connection_string="sqlite+aiosqlite:///:memory:",
        session_config=AsyncSessionConfig(expire_on_commit=False),
        create_all=True,
    )

    logging_cfg = get_structlog_config()
    otel_config = OpenTelemetryConfig()
    return Litestar(
        route_handlers=[create_router(path=path, settings=settings)],
        plugins=[SQLAlchemyPlugin(config=alchemy_config)],
        middleware=[otel_config.middleware],
        logging_config=logging_cfg.structlog_logging_config,
        debug=settings.debug,
        openapi_config=None,
    )


def get_sqlalchemy_config(app: Litestar) -> SQLAlchemyAsyncConfig:
    """Return the first SQLAlchemy config registered with the app."""
    for p in app.plugins:
        if isinstance(p, SQLAlchemyPlugin):
            return p.config[0]
    msg = "No SQLAlchemyPlugin found in app plugins"
    raise ValueError(msg)


async def cleanup_app_database(app: Litestar) -> None:
    """Reset the FactInventory table for app-backed tests."""
    async with get_sqlalchemy_config(app).get_session() as session:
        await truncate_fact_inventory(session)


async def _dispose_app_engine(app: Litestar) -> None:
    """Dispose the app's SQLAlchemy engine to close aiosqlite worker threads.

    Litestar's SQLAlchemyPlugin shuts down the engine during app lifespan
    exit, but with SQLite the underlying aiosqlite connection thread can
    outlive the event loop and raise when the loop is closed. Explicitly
    disposing the engine after the test client exits prevents the
    ``RuntimeError: Event loop is closed`` warning.
    """
    config = get_sqlalchemy_config(app)
    engine = getattr(config, "engine_instance", None)
    if engine is not None:
        await engine.dispose()


@asynccontextmanager
async def clean_test_client(app: Litestar) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide an app client with database and engine cleanup.

    Engine disposal runs after the client shuts down so the app's lifespan
    exits first, then the aiosqlite worker thread is stopped before the
    event loop closes.
    """
    try:
        async with AsyncTestClient(app=app) as test_client:
            await cleanup_app_database(test_client.app)
            try:
                yield test_client
            finally:
                await cleanup_app_database(test_client.app)
    finally:
        await _dispose_app_engine(app)


@pytest.fixture
async def test_app() -> Litestar:
    """Create a Litestar app instance with the test settings."""
    return create_app(settings=get_settings())


@pytest.fixture
async def test_client(test_app: Litestar) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide an async test client for making requests."""
    async with clean_test_client(test_app) as test_client:
        yield test_client


@pytest.fixture
async def test_db_session(
    test_client: AsyncTestClient,
) -> AsyncGenerator[AsyncSession, None]:
    """Extract AsyncSession from app for direct DB access and state verification.

    This fixture provides a raw database session tied to the test app's
    SQLAlchemy plugin. Use this when you need to verify internal state
    directly from the database (e.g., row counts after a delete operation).

    The session is automatically closed after the test completes.
    The engine is disposed after the test completes to prevent aiosqlite
    worker thread warnings.

    Note: We use the client fixture to ensure the app is fully initialized
    with all tables created before yielding the session.
    """
    plugin = None
    for p in test_client.app.plugins:
        if isinstance(p, SQLAlchemyPlugin):
            plugin = p
            break
    configs: list[SQLAlchemyAsyncConfig] = plugin.config
    async with configs[0].get_session() as session:
        yield session
    await _dispose_app_engine(test_client.app)


@pytest.fixture
def client_factory():
    """Return an async context manager for building test clients on demand."""

    @asynccontextmanager
    async def factory(
        *,
        router_path: str | None = None,
        settings_overrides: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[AsyncTestClient, None]:
        base_settings = get_settings()
        if settings_overrides:
            merged = {**base_settings.model_dump(), **settings_overrides}
            test_settings = Settings(**merged)
        else:
            test_settings = base_settings

        app = (
            create_router_test_app(router_path, test_settings)
            if router_path is not None
            else create_app(settings=test_settings)
        )
        async with clean_test_client(app) as test_client:
            yield test_client

    return factory


@pytest.fixture
async def client_with_custom_router_path(
    client_factory,
) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide HTTP client for app with explicit custom router path."""
    async with client_factory(router_path=CUSTOM_ROUTER_PATH) as test_client:
        yield test_client


@pytest.fixture
async def client_with_health(client_factory) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide HTTP client for app with /health endpoint enabled."""
    async with client_factory(
        settings_overrides={"enable_health_endpoint": True}
    ) as test_client:
        yield test_client


@pytest.fixture
async def client_with_ready(client_factory) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide HTTP client for app with /ready endpoint enabled."""
    async with client_factory(
        settings_overrides={"enable_ready_endpoint": True}
    ) as test_client:
        yield test_client


@pytest.fixture
async def client_with_metrics(client_factory) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide HTTP client for app with Prometheus metrics enabled."""
    async with client_factory(
        settings_overrides={"enable_metrics": True}
    ) as test_client:
        yield test_client


@pytest.fixture
async def client_with_health_and_ready(
    client_factory,
) -> AsyncGenerator[AsyncTestClient, None]:
    """Provide HTTP client for app with both /health and /ready enabled."""
    async with client_factory(
        settings_overrides={
            "enable_health_endpoint": True,
            "enable_ready_endpoint": True,
        }
    ) as test_client:
        yield test_client
