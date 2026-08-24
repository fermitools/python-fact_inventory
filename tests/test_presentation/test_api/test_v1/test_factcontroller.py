"""Tests for POST /v1/facts controller."""

from contextlib import asynccontextmanager
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from advanced_alchemy.exceptions import IntegrityError as AAIntegrityError
from advanced_alchemy.exceptions import NotFoundError as AANotFoundError
from advanced_alchemy.exceptions import RepositoryError
from litestar.exceptions import HTTPException as LitestarHTTPException
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
    HTTP_409_CONFLICT,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)
from litestar.testing import AsyncTestClient
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from fact_inventory.application.services.fact import FactInventoryService
from fact_inventory.lib.settings import get_settings
from tests.factories import (
    FACT_FIELDS,
    create_empty,
    create_minimal,
    create_partial,
    create_payload_over_body_limit,
    create_payload_under_body_limit,
    create_payload_with_field_at_limit,
    create_payload_with_oversized_field,
    create_valid,
)
from tests.support.http import assert_status, post_facts, request_id_headers

FACTS_ENDPOINT = f"{get_settings().app_prefix}/api/v1/facts"

REQUEST_ID_CASES = [
    pytest.param(None, id="without-request-id"),
    pytest.param("test-request-id", id="with-request-id"),
]

SINGLE_FACT_CASES = [
    pytest.param("system_facts", {"os": "RHEL"}, id="system_facts"),
    pytest.param("package_facts", {"glibc": "2.36"}, id="package_facts"),
    pytest.param("local_facts", {"env": "prod"}, id="local_facts"),
    pytest.param("client_facts", {"target_url": "/example/path"}, id="client_facts"),
]

EDGE_CASE_PAYLOADS = [
    pytest.param(
        {
            "system_facts": {"host": "\u670d\u52a1\u5668-01"},
            "package_facts": {},
            "local_facts": {},
            "client_facts": {},
        },
        id="unicode-values",
    ),
    pytest.param(
        {
            "system_facts": {"key": None},
            "package_facts": {},
            "local_facts": {},
            "client_facts": {},
        },
        id="null-values",
    ),
    pytest.param(
        {
            "system_facts": {"on": True},
            "package_facts": {},
            "local_facts": {},
            "client_facts": {},
        },
        id="boolean-values",
    ),
    pytest.param(
        {
            "system_facts": {"n": 42},
            "package_facts": {},
            "local_facts": {},
            "client_facts": {},
        },
        id="numeric-values",
    ),
    pytest.param(
        {
            "system_facts": {"tags": ["a", "b"]},
            "package_facts": {"v": [1, 2]},
            "local_facts": {"k": [3]},
            "client_facts": {},
        },
        id="list-values",
    ),
    pytest.param(
        {
            "system_facts": {"notes": r"!@#$%^&*(){}[]|\\:;\"'<>,.?/"},
            "package_facts": {},
            "local_facts": {},
            "client_facts": {},
        },
        id="special-characters",
    ),
]

SERVICE_ERROR_CASES = [
    pytest.param(
        SQLAlchemyError("db error"),
        HTTP_500_INTERNAL_SERVER_ERROR,
        None,
        id="sqlalchemy-error",
    ),
    pytest.param(
        IntegrityError("constraint", {}, Exception("constraint")),
        HTTP_409_CONFLICT,
        None,
        id="integrity-error",
    ),
    pytest.param(
        OperationalError("database unavailable", {}, Exception("unavailable")),
        HTTP_503_SERVICE_UNAVAILABLE,
        None,
        id="operational-error",
    ),
    pytest.param(
        RuntimeError("boom"),
        HTTP_500_INTERNAL_SERVER_ERROR,
        None,
        id="unexpected-error",
    ),
    pytest.param(
        TimeoutError("timeout"),
        HTTP_504_GATEWAY_TIMEOUT,
        "timeout",
        id="timeout-error",
    ),
    pytest.param(
        AAIntegrityError("duplicate key"),
        HTTP_409_CONFLICT,
        "unable to store record",
        id="advanced-alchemy-integrity-error",
    ),
    pytest.param(
        AANotFoundError("no such row"),
        HTTP_404_NOT_FOUND,
        "record not found",
        id="advanced-alchemy-not-found-error",
    ),
    pytest.param(
        RepositoryError("backend failure"),
        HTTP_500_INTERNAL_SERVER_ERROR,
        "internal server error",
        id="advanced-alchemy-repository-error",
    ),
]


@asynccontextmanager
async def mock_service_error(exception: Exception):
    """Patch the controller's service call to raise a specific error."""
    with patch.object(
        FactInventoryService,
        "insert_record",
        new_callable=AsyncMock,
        side_effect=exception,
    ) as mock:
        yield mock


@pytest.mark.parametrize("request_id", REQUEST_ID_CASES)
async def test_successful_submission_valid_payload_returns_201(
    test_client: AsyncTestClient,
    request_id: str | None,
) -> None:
    """Valid payloads succeed with or without a request ID header."""
    response = await post_facts(
        test_client,
        payload=create_valid(),
        headers=request_id_headers(request_id),
    )

    assert_status(response, HTTP_201_CREATED)
    assert "application/json" in response.headers["content-type"]
    assert "Facts stored successfully" in response.json()["detail"]


async def test_successful_submission_minimal_payload_returns_201(
    test_client: AsyncTestClient,
) -> None:
    """Minimal valid payloads succeed."""
    response = await post_facts(test_client)

    assert_status(response, HTTP_201_CREATED)
    assert "application/json" in response.headers["content-type"]


async def test_successful_submission_response_body_schema(
    test_client: AsyncTestClient,
) -> None:
    """Success responses contain the documented envelope."""
    response = await post_facts(
        test_client,
        payload=create_valid(),
    )
    body = response.json()

    assert_status(response, HTTP_201_CREATED)
    assert "application/json" in response.headers["content-type"]
    assert isinstance(body["detail"], str)
    assert body["detail"]


async def test_successful_submission_response_data_structure(
    test_client: AsyncTestClient,
) -> None:
    """Success responses include data with client_address."""
    response = await post_facts(
        test_client,
        payload=create_valid(),
    )
    body = response.json()

    assert_status(response, HTTP_201_CREATED)
    assert isinstance(body["data"], dict)

    assert "record_id" in body["data"]
    record_id = body["data"]["record_id"]
    assert isinstance(record_id, str)
    assert len(record_id) > 0

    assert "client_address" in body["data"]
    client_address = body["data"]["client_address"]
    assert isinstance(client_address, str)
    assert len(client_address) > 0

    # client_address should be either a valid IPv4/IPv6 or a hostname
    # (test client uses "testclient" as hostname)
    try:
        ip_address(client_address)
    except ValueError:
        # Not an IP, but that's okay - could be a hostname
        assert client_address.isalnum() or "-" in client_address


@pytest.mark.parametrize("field", FACT_FIELDS)
async def test_dto_validation_missing_required_field_returns_400(
    field: str,
    test_client: AsyncTestClient,
) -> None:
    """DTO validation rejects payloads missing any required fact field."""
    payload = create_minimal()
    del payload[field]

    response = await post_facts(test_client, payload=payload)
    assert_status(response, HTTP_400_BAD_REQUEST)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("system_facts", "not a dict", id="string"),
        pytest.param("package_facts", [], id="list"),
        pytest.param("local_facts", 123, id="integer"),
        pytest.param("client_facts", True, id="boolean"),
    ],
)
async def test_dto_validation_wrong_field_type_returns_400(
    field: str,
    value: Any,
    test_client: AsyncTestClient,
) -> None:
    """DTO validation rejects fact fields that are not dictionaries."""
    payload = create_empty()
    payload[field] = value

    response = await post_facts(test_client, payload=payload)
    assert_status(response, HTTP_400_BAD_REQUEST)


async def test_dto_validation_unknown_fields_rejected(
    test_client: AsyncTestClient,
) -> None:
    """DTO validation rejects unknown fields."""
    response = await post_facts(
        test_client,
        payload={**create_minimal(), "extra": "bad"},
    )

    assert_status(response, HTTP_400_BAD_REQUEST)


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        pytest.param(
            b"not json", {"Content-Type": "application/json"}, id="invalid-json"
        ),
        pytest.param(b"", {"Content-Type": "application/json"}, id="empty-body"),
        pytest.param(
            b"null", {"Content-Type": "application/json"}, id="null-json-body"
        ),
    ],
)
async def test_dto_validation_invalid_raw_body_returns_400(
    test_client: AsyncTestClient,
    content: bytes,
    headers: dict[str, str],
) -> None:
    """Malformed or non-object JSON bodies are rejected."""
    response = await post_facts(test_client, content=content, headers=headers)

    assert_status(response, HTTP_400_BAD_REQUEST)


@pytest.mark.parametrize("request_id", REQUEST_ID_CASES)
async def test_business_validation_all_empty_facts_returns_400(
    test_client: AsyncTestClient,
    request_id: str | None,
) -> None:
    """Business validation rejects payloads with no facts present."""
    response = await post_facts(
        test_client,
        payload=create_empty(),
        headers=request_id_headers(request_id),
    )

    assert_status(response, HTTP_400_BAD_REQUEST)
    assert response.json()["detail"] == "Validation failed"


@pytest.mark.parametrize(("field", "value"), SINGLE_FACT_CASES)
async def test_business_validation_single_non_empty_category_accepted(
    field: str,
    value: dict[str, Any],
    test_client: AsyncTestClient,
) -> None:
    """One populated fact category is sufficient."""
    payload = create_partial(field, value)
    response = await post_facts(test_client, payload=payload)

    assert_status(response, HTTP_201_CREATED)


async def test_payload_size_limits_field_over_json_limit_returns_413(
    test_client: AsyncTestClient,
) -> None:
    """An oversized JSON field returns 413."""
    response = await post_facts(
        test_client,
        payload=create_payload_with_oversized_field(get_settings().max_json_field_mb),
    )

    assert_status(response, HTTP_413_REQUEST_ENTITY_TOO_LARGE)


@pytest.mark.parametrize("field", FACT_FIELDS)
async def test_payload_size_limits_each_field_checked_independently(
    field: str,
    test_client: AsyncTestClient,
) -> None:
    """Each fact field is checked against the JSON size limit."""
    response = await post_facts(
        test_client,
        payload=create_payload_with_oversized_field(
            get_settings().max_json_field_mb,
            field,
        ),
    )

    assert_status(response, HTTP_413_REQUEST_ENTITY_TOO_LARGE)


async def test_payload_size_limits_field_at_limit_accepted(
    test_client: AsyncTestClient,
) -> None:
    """Fields exactly at the limit are accepted."""
    response = await post_facts(
        test_client,
        payload=create_payload_with_field_at_limit(get_settings().max_json_field_mb),
    )

    assert_status(response, HTTP_201_CREATED)


@pytest.mark.parametrize(
    ("payload_factory", "expected_status"),
    [
        pytest.param(
            lambda: create_payload_over_body_limit(get_settings().max_request_body_mb),
            HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            id="body-over-limit",
        ),
        pytest.param(
            lambda: create_payload_under_body_limit(get_settings().max_request_body_mb),
            HTTP_201_CREATED,
            id="body-under-limit",
        ),
    ],
)
async def test_payload_size_limits_request_body_boundary(
    test_client: AsyncTestClient,
    payload_factory,
    expected_status: int,
) -> None:
    """The HTTP request body limit is enforced at the endpoint."""
    response = await post_facts(test_client, payload=payload_factory())

    assert_status(response, expected_status)


@pytest.mark.parametrize("payload", EDGE_CASE_PAYLOADS)
async def test_edge_case_payloads_are_accepted(
    test_client: AsyncTestClient,
    payload: dict[str, Any],
) -> None:
    """Valid JSON scalar and collection values are accepted in fact payloads."""
    response = await post_facts(test_client, payload=payload)

    assert_status(response, HTTP_201_CREATED)


@pytest.mark.parametrize("request_id", REQUEST_ID_CASES)
@pytest.mark.parametrize(
    ("exception", "expected_status", "expected_detail_substring"),
    SERVICE_ERROR_CASES,
)
async def test_error_handling_service_errors_map_to_http_responses(
    test_client: AsyncTestClient,
    request_id: str | None,
    exception: Exception,
    expected_status: int,
    expected_detail_substring: str | None,
) -> None:
    """Service-layer errors are translated to the expected HTTP status."""
    async with mock_service_error(exception) as mock:
        response = await post_facts(
            test_client,
            headers=request_id_headers(request_id),
        )

    mock.assert_awaited_once()
    assert_status(response, expected_status)
    if expected_detail_substring is not None:
        assert expected_detail_substring in response.json()["detail"].lower()


async def test_error_handling_http_exception_from_service_is_reraised(
    test_client: AsyncTestClient,
) -> None:
    """HTTP exceptions from the service pass through unchanged."""
    with patch.object(
        FactInventoryService,
        "insert_record",
        new_callable=AsyncMock,
        side_effect=LitestarHTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="direct http exc",
        ),
    ) as mock:
        response = await post_facts(test_client)

    mock.assert_awaited_once()
    assert_status(response, HTTP_400_BAD_REQUEST)


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "expected_status"),
    [
        pytest.param(
            "post",
            "/facts",
            {"json": create_minimal()},
            HTTP_404_NOT_FOUND,
            id="bare-facts",
        ),
        pytest.param(
            "post",
            FACTS_ENDPOINT,
            {"json": create_minimal()},
            HTTP_201_CREATED,
            id="versioned-facts",
        ),
        pytest.param(
            "get", "/v1/health", {}, HTTP_404_NOT_FOUND, id="health-not-under-v1"
        ),
        pytest.param(
            "get", FACTS_ENDPOINT, {}, HTTP_405_METHOD_NOT_ALLOWED, id="get-not-allowed"
        ),
        pytest.param(
            "put",
            FACTS_ENDPOINT,
            {"json": {}},
            HTTP_405_METHOD_NOT_ALLOWED,
            id="put-not-allowed",
        ),
        pytest.param(
            "patch",
            FACTS_ENDPOINT,
            {"json": {}},
            HTTP_405_METHOD_NOT_ALLOWED,
            id="patch-not-allowed",
        ),
        pytest.param(
            "delete",
            FACTS_ENDPOINT,
            {},
            HTTP_405_METHOD_NOT_ALLOWED,
            id="delete-not-allowed",
        ),
    ],
)
async def test_routing_and_method_contract(
    test_client: AsyncTestClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    expected_status: int,
) -> None:
    """The controller is only reachable on the documented POST route."""
    response = await getattr(test_client, method)(path, **kwargs)

    assert_status(response, expected_status)
