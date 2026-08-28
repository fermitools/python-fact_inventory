"""Handler for timeout errors.

Handles both the builtin ``TimeoutError`` and ``sqlalchemy.exc.TimeoutError``
(e.g. connection pool exhaustion). The two are unrelated exception
hierarchies -- ``sqlalchemy.exc.TimeoutError`` subclasses
``sqlalchemy.exc.SQLAlchemyError``, not the builtin -- so this handler must be
registered under both exception classes in the router's exception_handlers map.
"""

from typing import Any

from litestar import Response
from litestar.status_codes import HTTP_504_GATEWAY_TIMEOUT

from fact_inventory.presentation.api.v1.exception_handlers.helpers import (
    _client_address,
    _error_response,
)

__all__ = ["timeout_error_handler"]


def timeout_error_handler(
    request: Any,
    exc: Exception,
) -> Response[dict[str, Any]]:
    """Map timeout failures to HTTP 504."""
    request.logger.error(
        "Request timeout",
        client_address=_client_address(request),
        error=str(exc),
    )
    return _error_response("Request timeout", HTTP_504_GATEWAY_TIMEOUT)
