"""BearerAuth must send OCS-APIRequest: Nextcloud refuses a bearer on index.php
API routes without it (401), which broke Deck get_stack/get_archived_stacks and
every other method that sends no app headers (hackbarn card 741)."""

import httpx
import pytest

from nextcloud_mcp_server.auth.bearer_auth import BearerAuth


def _sent_headers(**request_headers) -> httpx.Headers:
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["h"] = request.headers
        return httpx.Response(200, json=[])

    client = httpx.Client(
        transport=httpx.MockTransport(handler), auth=BearerAuth("tok")
    )
    client.get(
        "https://nc.example/index.php/apps/deck/api/v1.0/boards/4/stacks/68",
        headers=request_headers,
    )
    return seen["h"]


def test_bearer_request_carries_ocs_header():
    h = _sent_headers()
    assert h["Authorization"] == "Bearer tok"
    assert h["OCS-APIRequest"] == "true"


def test_explicit_header_is_not_overridden():
    h = _sent_headers(**{"OCS-APIRequest": "false"})
    assert h["OCS-APIRequest"] == "false"


@pytest.mark.asyncio
async def test_async_client_too():
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["h"] = request.headers
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=BearerAuth("tok")
    ) as c:
        await c.get(
            "https://nc.example/index.php/apps/deck/api/v1.0/boards/4/stacks/archived"
        )
    assert seen["h"]["OCS-APIRequest"] == "true"
