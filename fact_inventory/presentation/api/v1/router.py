"""Router for the v1 API."""

from collections.abc import AsyncGenerator

from advanced_alchemy.exceptions import RepositoryError
from litestar import Router
from litestar.di import NamedDependency, Provide
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from fact_inventory.application.services.fact import FactInventoryService
from fact_inventory.lib.exceptions import (
    FactPayloadTooLargeError,
    FactValidationError,
)
from fact_inventory.lib.settings import Settings, get_settings
from fact_inventory.presentation.api.v1.exception_handlers import (
    fact_payload_too_large_error_handler,
    fact_validation_error_handler,
    repository_error_handler,
    sqlalchemy_error_handler,
    timeout_error_handler,
)
from fact_inventory.presentation.api.v1.factcontroller import FactInventoryController

__all__ = ["create_v1_router"]


def create_v1_router(settings: Settings | None = None) -> Router:
    """Create the v1 router with application-specific service settings."""
    app_settings = settings or get_settings()

    async def provide_service(
        db_session: NamedDependency[AsyncSession],
    ) -> AsyncGenerator[FactInventoryService, None]:
        yield FactInventoryService(session=db_session, settings=app_settings)

    return Router(
        path="/v1",
        route_handlers=[FactInventoryController],
        tags=["api", "v1"],
        dependencies={"fact_inventory_service": Provide(provide_service)},
        exception_handlers={
            FactValidationError: fact_validation_error_handler,
            FactPayloadTooLargeError: fact_payload_too_large_error_handler,
            RepositoryError: repository_error_handler,
            SQLAlchemyError: sqlalchemy_error_handler,
            # sqlalchemy.exc.TimeoutError (e.g. connection pool exhaustion) does
            # NOT subclass the builtin TimeoutError -- it subclasses
            # SQLAlchemyError instead. Both must be registered explicitly so
            # pool-timeout errors resolve to the 504 handler instead of falling
            # through to the generic SQLAlchemyError -> 500 handler.
            SQLAlchemyTimeoutError: timeout_error_handler,
            TimeoutError: timeout_error_handler,
        },
    )
