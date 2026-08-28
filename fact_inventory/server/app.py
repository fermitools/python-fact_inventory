"""Application factory pattern implementation for the Litestar ASGI application.

This module is part of the server assembly layer and wires together:

- Database via SQLAlchemy async using ``advanced_alchemy``.
- Observability via OpenTelemetry and Prometheus.
- Rate limiting baked into the router via ``create_router``.
- Background job scheduler via ``AsyncBackgroundJobPlugin``.
- All route handlers.

Configuration is sourced from ``fact_inventory.lib`` (application infrastructure)
while application assembly and plugin wiring happens here in ``fact_inventory.server``.

Notes
-----
The factory accepts an optional ``Settings`` instance. When omitted it falls
back to the cached application settings. This keeps production wiring simple
while making tests fully deterministic.
"""

import logging
from typing import Any

from advanced_alchemy.extensions.litestar import (
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.config.cors import CORSConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.plugins.opentelemetry import OpenTelemetryConfig, OpenTelemetryPlugin
from litestar.plugins.prometheus import PrometheusConfig
from litestar.plugins.structlog import StructlogConfig, StructlogPlugin

from fact_inventory.infrastructure.db.db_migrations import check_migrations_up_to_date
from fact_inventory.lib.settings import Settings, get_settings
from fact_inventory.presentation.metrics import create_metrics_controller
from fact_inventory.presentation.router import create_router
from fact_inventory.server.background_job.history_cleanup import (
    create_history_cleanup_job,
)
from fact_inventory.server.background_job.retain_cleanup import (
    create_retention_cleanup_job,
)
from fact_inventory.server.config.database import create_sqlalchemy_config
from fact_inventory.server.config.observability import (
    create_logging_config,
    create_tracer_provider,
)
from fact_inventory.server.middleware.traceparent import TraceparentMiddleware

__all__ = ["create_app"]

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Litestar:
    """Assemble and return a fully configured Litestar ASGI application.

    The application factory pattern wires together the database plugin,
    background cleanup tasks, observability middleware (Prometheus
    optional), and rate-limited route handlers.
    All configuration tunables are read from the provided ``Settings``.

    OpenAPI documentation is enabled only when DEBUG=true to avoid
    exposing the schema on production deployments.

    Parameters
    ----------
    settings : Settings | None
        Application configuration. When ``None`` the cached application
        settings are used.

    Returns
    -------
    Litestar
        Fully configured Litestar application ready to be served by
        an ASGI server.
    """
    settings = settings or get_settings()

    if settings.database_uri is None:  # pragma: no cover
        raise ValueError(  # noqa: TRY003
            "DATABASE_URI is required. Set it to a valid database connection string."
        )

    async def _check_db_migrations() -> None:
        """Check migrations using the application's configured engine.

        The check does not create a second engine or connection pool.
        """
        await check_migrations_up_to_date(engine=alchemy_config.get_engine())

    alchemy_config = create_sqlalchemy_config(
        database_uri=settings.database_uri,
        debug=settings.debug,
        pool_settings={
            "pool_size": settings.db_pool_size,
            "pool_recycle": settings.db_pool_recycle_seconds,
            "max_overflow": settings.db_pool_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
            "statement_timeout_ms": settings.db_statement_timeout_ms,
        },
    )

    retention_cleanup_plugin = create_retention_cleanup_job(settings, alchemy_config)
    history_cleanup_plugin = create_history_cleanup_job(settings, alchemy_config)

    logging_config: StructlogConfig = create_logging_config(settings)
    tracer_provider, shutdown_hooks = create_tracer_provider(settings)

    cors_config = CORSConfig(allow_origins=[])

    app_kwargs: dict[str, Any] = {
        "route_handlers": [create_router(path=settings.app_prefix, settings=settings)],
        "plugins": [
            SQLAlchemyPlugin(config=alchemy_config),
            OpenTelemetryPlugin(OpenTelemetryConfig(tracer_provider=tracer_provider)),
            StructlogPlugin(config=logging_config),
        ],
        "middleware": [OpenTelemetryConfig(tracer_provider=tracer_provider).middleware],
        "compression_config": CompressionConfig(backend="gzip"),
        "cors_config": cors_config,
        "logging_config": logging_config.structlog_logging_config,
        "openapi_config": None,
        "debug": settings.debug,
        "request_max_body_size": settings.max_request_body_mb * 1024 * 1024,
        "on_startup": [_check_db_migrations],
        "on_shutdown": shutdown_hooks,
    }

    if settings.debug:
        app_kwargs["openapi_config"] = OpenAPIConfig(
            title=settings.app_name,
            version=settings.version,
        )

    if settings.enable_retention_cleanup_job:
        app_kwargs["plugins"].append(retention_cleanup_plugin)

    if settings.enable_history_cleanup_job:
        app_kwargs["plugins"].append(history_cleanup_plugin)

    if settings.enable_metrics:
        prometheus_config = PrometheusConfig(app_name=settings.app_name)
        metrics_path = (
            f"{settings.app_prefix.rstrip('/')}/metrics"
            if settings.app_prefix != "/"
            else "/metrics"
        )
        app_kwargs["route_handlers"].append(create_metrics_controller(metrics_path))
        app_kwargs["middleware"].append(prometheus_config.middleware)

    logger.info("Fact Inventory application starting")

    app = Litestar(**app_kwargs)

    app.asgi_handler = TraceparentMiddleware()(app.asgi_handler)

    return app
