"""Tests for GET /api API router mounted check endpoint."""

from litestar.status_codes import HTTP_200_OK, HTTP_405_METHOD_NOT_ALLOWED
from litestar.testing import AsyncTestClient

from fact_inventory.lib.settings import get_settings
from tests.support.http import assert_status

API_ROOT = f"{get_settings().app_prefix}/api"


async def test_api_root_returns_200_when_mounted(test_client: AsyncTestClient) -> None:
    """API root endpoint returns 200 when router is mounted."""
    response = await test_client.get(API_ROOT)

    assert_status(response, HTTP_200_OK)
    body = response.json()
    assert body["app"] == get_settings().app_name


async def test_api_root_post_not_allowed(test_client: AsyncTestClient) -> None:
    """API root only accepts GET."""
    response = await test_client.post(API_ROOT, json={})

    assert_status(response, HTTP_405_METHOD_NOT_ALLOWED)


async def test_api_root_put_not_allowed(test_client: AsyncTestClient) -> None:
    """API root does not accept PUT."""
    response = await test_client.put(API_ROOT, json={})

    assert_status(response, HTTP_405_METHOD_NOT_ALLOWED)


async def test_api_root_patch_not_allowed(test_client: AsyncTestClient) -> None:
    """API root does not accept PATCH."""
    response = await test_client.patch(API_ROOT, json={})

    assert_status(response, HTTP_405_METHOD_NOT_ALLOWED)


async def test_api_root_delete_not_allowed(test_client: AsyncTestClient) -> None:
    """API root does not accept DELETE."""
    response = await test_client.delete(API_ROOT)

    assert_status(response, HTTP_405_METHOD_NOT_ALLOWED)
