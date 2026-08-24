"""Controller for v1 API fact submissions.

Defines the HTTP endpoint for submitting system and package facts.
Intentionally thin since it is an abstraction layer.

Controller Layer Rationale
--------------------------
Controllers translate HTTP requests into service method calls and responses.
They do not contain business logic - that lives in services. This separation
keeps endpoints stable even as rules evolve, and makes business logic testable
without mocking HTTP machinery.

Request validation is performed by the Pydantic request model. JSON field size
validation is performed by the service layer (domain constraint enforcement);
the resulting domain exceptions are converted to HTTP responses by the
application-level exception handlers registered in the app factory.
"""

from typing import Annotated, Any

from litestar import Controller, Request, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.openapi.datastructures import ResponseSpec
from litestar.openapi.spec import Example
from litestar.params import Body
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_409_CONFLICT,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_504_GATEWAY_TIMEOUT,
)

from fact_inventory.application.services.fact import FactInventoryService
from fact_inventory.presentation.api.v1.schemas.requests import (
    FactInventoryCreateRequest,
)
from fact_inventory.presentation.api.v1.schemas.responses import APIResponse

__all__ = ["FactInventoryController"]


#: OpenAPI response specifications for POST /api/v1/facts.
_FACT_RESPONSES = {
    HTTP_201_CREATED: ResponseSpec(
        data_container=APIResponse,
        description="Facts stored successfully",
        examples=[
            Example(
                summary="Record created",
                description="The facts have been stored in the database."
                " Note: this does not return a URL to review the record.",
                value={
                    "status": "ok",
                    "detail": "Facts stored successfully for 192.0.2.1",
                    "data": {
                        "client_address": "192.0.2.1",
                        "record_id": "5b30857f-0bfa-48b5-ac0b-5c64e28078d1",
                    },
                },
            )
        ],
    ),
    HTTP_400_BAD_REQUEST: ResponseSpec(
        data_container=APIResponse,
        description="Bad Request -- the client address could not be determined"
        " or all fact categories are empty",
        examples=[
            Example(
                summary="Missing client address",
                description=(
                    "The server could not resolve the connecting client's IP address."
                ),
                value={
                    "status": "error",
                    "detail": "Validation failed",
                    "data": None,
                },
            ),
            Example(
                summary="Empty facts",
                description="All fact categories are empty.",
                value={
                    "status": "error",
                    "detail": "Validation failed",
                    "data": None,
                },
            ),
        ],
    ),
    HTTP_409_CONFLICT: ResponseSpec(
        data_container=APIResponse,
        description="Conflict -- the record violates a database constraint",
        examples=[
            Example(
                summary="Record could not be stored",
                description="The database rejected the record because of a constraint.",
                value={
                    "status": "error",
                    "detail": "Unable to store record",
                    "data": None,
                },
            ),
            Example(
                summary="Constraint conflict",
                description=(
                    "The submitted record conflicts with an existing constraint."
                ),
                value={
                    "status": "error",
                    "detail": "Unable to store record",
                    "data": None,
                },
            ),
        ],
    ),
    HTTP_413_REQUEST_ENTITY_TOO_LARGE: ResponseSpec(
        data_container=APIResponse,
        description=(
            "Payload Too Large -- the request body exceeds the configured size limit"
        ),
        examples=[
            Example(
                summary="Body too large",
                description=("The submitted payload exceeds the maximum allowed size."),
                value={
                    "status": "error",
                    "detail": "Request entity too large",
                    "data": None,
                },
            ),
            Example(
                summary="JSON field too large",
                description=(
                    "A JSON field in the request exceeds the configured size limit."
                ),
                value={
                    "status": "error",
                    "detail": "Request entity too large",
                    "data": None,
                },
            ),
        ],
    ),
    HTTP_429_TOO_MANY_REQUESTS: ResponseSpec(
        data_container=None,
        description=(
            "Too Many Requests -- the client has exceeded the rate limit."
            " Raised by Litestar's RateLimitMiddleware before the handler runs,"
            " so the body uses Litestar's default error shape rather than the"
            " APIResponse envelope. RateLimit-* headers are sent when"
            " DEBUG=true."
        ),
        examples=[
            Example(
                summary="Rate limit exceeded",
                description=(
                    "The client must wait before submitting again."
                    " Handled automatically by litestar.middleware.rate_limit."
                ),
                value={
                    "status_code": 429,
                    "detail": "Too Many Requests",
                },
            ),
        ],
    ),
    HTTP_500_INTERNAL_SERVER_ERROR: ResponseSpec(
        data_container=APIResponse,
        description="Internal Server Error -- an unexpected error occurred",
        examples=[
            Example(
                summary="Unexpected server error",
                description=(
                    "An unexpected error occurred; details are recorded server-side."
                ),
                value={
                    "status": "error",
                    "detail": "Internal server error",
                    "data": None,
                },
            ),
            Example(
                summary="Unexpected error with context",
                description=("An unexpected error occurred during fact processing."),
                value={
                    "status": "error",
                    "detail": "Internal server error",
                    "data": None,
                },
            ),
        ],
    ),
    HTTP_504_GATEWAY_TIMEOUT: ResponseSpec(
        data_container=APIResponse,
        description="Gateway Timeout -- an upstream operation timed out",
        examples=[
            Example(
                summary="Database timeout",
                description=("The database operation exceeded the configured timeout."),
                value={
                    "status": "error",
                    "detail": "Request timeout",
                    "data": None,
                },
            ),
            Example(
                summary="Operation timeout with context",
                description=("An upstream service or database operation timed out."),
                value={
                    "status": "error",
                    "detail": "Request timeout",
                    "data": None,
                },
            ),
        ],
    ),
}

#: Request body examples for POST /api/v1/facts.
_FACT_BODY_EXAMPLES = [
    Example(
        summary="Fedora System",
        description="Example facts from a Fedora 42 installation",
        value={
            "system_facts": {
                "distribution": "Fedora",
                "distribution_file_path": "/etc/redhat-release",
                "distribution_file_variety": "RedHat",
                "distribution_major_version": "42",
                "distribution_version": "42",
            },
            "package_facts": {
                "glibc": [
                    {
                        "arch": "x86_64",
                        "epoch": "null",
                        "name": "glibc",
                        "release": "11.fc42",
                        "source": "rpm",
                        "version": "2.41",
                    }
                ],
            },
            "local_facts": {"key": "value"},
            "client_facts": {"target_url": "/example/path"},
        },
    ),
    Example(
        summary="Minimal facts",
        description=(
            "Minimum valid submission: at least one fact category must be"
            " non-empty. An all-empty payload is rejected with HTTP 400."
        ),
        value={
            "system_facts": {},
            "package_facts": {},
            "local_facts": {"lsmod": ["cdrom", "sr_mod"]},
            "client_facts": {},
        },
    ),
]


class FactInventoryController(Controller):
    """Controller for v1 API fact submissions.

    Rate limiting is handled externally by Litestar's RateLimitMiddleware
    (configured in the application factory). This controller is responsible
    only for HTTP boundary enforcement and persistence delegation.

    The FactInventoryService is injected via Litestar's dependency-injection
    system using the ``provide_service`` provider defined on the v1 router.
    """

    path: str = "/facts"

    @post(
        "",
        status_code=HTTP_201_CREATED,
        description="Submit system, package, local, and client facts",
        responses=_FACT_RESPONSES,
    )
    async def submit(
        self,
        data: Annotated[
            FactInventoryCreateRequest,
            Body(examples=_FACT_BODY_EXAMPLES),
        ],
        request: Request[Any, Any, Any],
        fact_inventory_service: NamedDependency[FactInventoryService],
    ) -> APIResponse:
        """Store submitted system and package facts for the calling client.

        The calling client is identified by its IP address (from
        request.client.host). Each submission creates a new record.

        Request validation is performed by the Pydantic request model. JSON
        field size validation is performed by the service layer; the resulting
        domain exceptions are converted to HTTP responses by the application-
        level exception handlers. Rate limiting is enforced by the middleware
        before this handler runs. Persistence is delegated to the injected
        service.

        Rate Limiting
        -------------
        This endpoint is rate-limited according to the ``API_RATE_LIMIT_MAX_REQUESTS``
        and ``API_RATE_LIMIT_UNIT`` settings. When the rate limit is exceeded, the
        middleware returns HTTP 429 Too Many Requests (Litestar's default error
        body, not the APIResponse envelope). RateLimit-Limit, RateLimit-Remaining,
        and RateLimit-Reset headers are sent.

        Parameters
        ----------
        data : FactInventoryCreateRequest
            The validated fact inventory data from the request body.
        request : Request
            The HTTP request object used to extract the client IP address.
        fact_inventory_service : FactInventoryService
            The service instance injected by Litestar's dependency system.

        Returns
        -------
        APIResponse
            HTTP 201 Created with APIResponse envelope on success.

        Raises
        ------
        HTTPException
            HTTP 400 if client address cannot be determined.
        """
        if request.client is None:  # pragma: no cover
            request.logger.warning("Unable to determine client address")
            raise HTTPException(
                detail="Unable to determine client address",
                status_code=HTTP_400_BAD_REQUEST,
            )

        record = await fact_inventory_service.insert_record(
            data={
                "client_address": request.client.host,
                **data.model_dump(),
            }
        )
        request.logger.info(
            "Fact inventory record created",
            http_request_method=request.method,
            http_response_status_code=201,
            http_route=request.url.path,
            client_address=record.client_address,
            record_id=record.id,
        )
        return APIResponse(
            status="ok",
            detail="Facts stored successfully",
            data={"client_address": record.client_address, "record_id": record.id},
        )
