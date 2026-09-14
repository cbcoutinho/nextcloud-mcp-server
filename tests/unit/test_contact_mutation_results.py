"""Contact mutation receipts through the real client and MCP serialization stack."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.types import TextContent
from pytest_mock import MockerFixture

from nextcloud_mcp_server.client.contacts import ContactsClient
from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.models.contacts import ContactMutationResponse
from nextcloud_mcp_server.server.contacts import configure_contacts_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def mutation_server(mocker: MockerFixture) -> NextcloudMCPServer:
    """Use real HTTP status handling, with no network or live Nextcloud writes."""
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    )
    server = NextcloudMCPServer("contact-mutation-results")
    configure_contacts_tools(server)
    return server


@pytest.mark.parametrize("via_mcp", [False, True], ids=["direct", "mcp"])
@pytest.mark.parametrize(
    "operation,uid,object_name,etag",
    [
        ("create", "alice", "alice.vcf", '"created-etag"'),
        ("create", "alice", "alice.vcf", None),
        ("create", "\uc5f0\ub77d\ucc98", "\uc5f0\ub77d\ucc98.vcf", None),
        ("delete", "alice", "alice.vcf", None),
        ("delete", "default", "default", None),
        ("delete", "\uc5f0\ub77d\ucc98", "\uc5f0\ub77d\ucc98", None),
    ],
)
async def test_mutation_receipt(
    mocker: MockerFixture,
    mutation_server: NextcloudMCPServer,
    via_mcp: bool,
    operation: str,
    uid: str,
    object_name: str,
    etag: str | None,
) -> None:
    addressbook = "friends"
    base = f"/remote.php/dav/addressbooks/users/canonical-user/{addressbook}"
    path = f"{base}/{object_name}"
    status = 201 if operation == "create" else 204
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PROPFIND":
            assert operation == "delete"
            assert request.url.path == base
            return httpx.Response(
                207,
                text=f'<d:multistatus xmlns:d="DAV:"><d:response>'
                f"<d:href>{path}</d:href></d:response></d:multistatus>",
            )
        assert request.url.path == path
        assert request.method == ("PUT" if operation == "create" else "DELETE")
        if operation == "create":
            assert request.headers["If-None-Match"] == "*"
            assert f"UID:{uid}" in request.content.decode()
        return httpx.Response(status, headers={"ETag": etag} if etag else {})

    async with httpx.AsyncClient(
        base_url="https://nextcloud.example", transport=httpx.MockTransport(handle)
    ) as http:
        contacts = ContactsClient(http, "login-name")
        contacts._principal_id = "canonical-user"
        contacts._principal_discovered = True
        mocker.patch(
            "nextcloud_mcp_server.server.contacts.get_client",
            return_value=SimpleNamespace(contacts=contacts),
        )
        arguments: dict[str, Any] = {"addressbook": addressbook, "uid": uid}
        if operation == "create":
            arguments["contact_data"] = {"fn": "Alice"}
        if via_mcp:
            async with Client(mutation_server) as client:
                result = await client.call_tool(
                    f"nc_contacts_{operation}_contact", arguments
                )
            assert not result.is_error
            assert len(result.content) == 1
            assert isinstance(result.content[0], TextContent)
            payload = json.loads(result.content[0].text)
            assert payload == result.structured_content
            assert payload["success"] is True
            assert payload["timestamp"]
            ContactMutationResponse.model_validate(payload)
        else:
            payload = await getattr(contacts, f"{operation}_contact")(**arguments)

    assert payload["uid"] == uid
    assert payload["addressbook"] == addressbook
    assert payload["resource_path"] == path
    assert payload["status_code"] == status
    assert payload.get("etag") == etag
    assert "contact" not in payload
    assert [r.method for r in requests] == (
        ["PUT"] if operation == "create" else ["PROPFIND", "DELETE"]
    )


@pytest.mark.parametrize("via_mcp", [False, True], ids=["direct", "mcp"])
@pytest.mark.parametrize("operation", ["create", "delete"])
@pytest.mark.parametrize("status", [403, 404, 409, 412])
async def test_mutation_http_failure(
    mocker: MockerFixture,
    mutation_server: NextcloudMCPServer,
    via_mcp: bool,
    operation: str,
    status: int,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PROPFIND":
            # No match retains the existing fallback to <uid>.vcf, including 404.
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        assert request.url.path.endswith("/contacts/missing.vcf")
        return httpx.Response(status)

    async with httpx.AsyncClient(
        base_url="https://nextcloud.example", transport=httpx.MockTransport(handle)
    ) as http:
        contacts = ContactsClient(http, "testuser")
        contacts._principal_discovered = True
        mocker.patch(
            "nextcloud_mcp_server.server.contacts.get_client",
            return_value=SimpleNamespace(contacts=contacts),
        )
        arguments: dict[str, Any] = {"addressbook": "contacts", "uid": "missing"}
        if operation == "create":
            arguments["contact_data"] = {"fn": "Alice"}
        if via_mcp:
            async with Client(mutation_server) as client:
                result = await client.call_tool(
                    f"nc_contacts_{operation}_contact", arguments
                )
            assert result.is_error
            assert result.content
            assert result.structured_content is None
        else:
            mutation = getattr(contacts, f"{operation}_contact")(**arguments)
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await mutation
            assert exc.value.response.status_code == status

    assert [r.method for r in requests] == (
        ["PUT"] if operation == "create" else ["PROPFIND", "DELETE"]
    )
