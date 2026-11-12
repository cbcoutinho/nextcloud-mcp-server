"""Contact mutation receipts through the real client and MCP serialization stack."""

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from mcp.types import TextContent
from nextcloud_client.contacts import ContactsClient
from pytest_mock import MockerFixture

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.models.contacts import (
    AddressBook,
    Contact,
    CreateAddressBookResponse,
    CreateContactResponse,
    DeleteAddressBookResponse,
    DeleteContactResponse,
)
from nextcloud_mcp_server.server.contacts import configure_contacts_tools

pytestmark = pytest.mark.unit


def test_existing_create_response_constructors() -> None:
    """Existing callers can still supply full objects without receipt metadata."""
    contact = Contact(uid="alice", fn="Alice")
    response = CreateContactResponse(contact=contact, addressbook="friends")
    assert response.contact == contact
    assert response.uid is None
    addressbook = AddressBook(uri="friends", displayname="My Friends")
    assert CreateAddressBookResponse(addressbook=addressbook).addressbook == addressbook


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
    """Contact writes expose receipts without re-reading the written resource."""
    addressbook = "friends"
    base = f"/remote.php/dav/addressbooks/users/canonical-user/{addressbook}"
    path = f"{base}/{object_name}"
    status = 201 if operation == "create" else 204
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.method == "PROPFIND":
            assert operation == "delete"
            assert request.url.path == base
            return httpx2.Response(
                207,
                text=f'<d:multistatus xmlns:d="DAV:"><d:response>'
                f"<d:href>{path}</d:href></d:response></d:multistatus>",
            )
        assert request.url.path == path
        assert request.method == ("PUT" if operation == "create" else "DELETE")
        if operation == "create":
            assert request.headers["If-None-Match"] == "*"
            assert f"UID:{uid}" in request.content.decode()
        return httpx2.Response(status, headers={"ETag": etag} if etag else {})

    async with httpx2.AsyncClient(
        base_url="https://nextcloud.example", transport=httpx2.MockTransport(handle)
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
            if operation == "create":
                CreateContactResponse.model_validate(payload)
                assert payload["contact"] is None
            else:
                DeleteContactResponse.model_validate(payload)
        else:
            payload = await getattr(contacts, f"{operation}_contact")(**arguments)

    assert payload["uid" if operation == "create" else "deleted_uid"] == uid
    assert payload["addressbook"] == addressbook
    assert "resource_path" not in payload
    assert payload["status_code"] == status
    assert payload.get("etag") == etag
    assert payload.get("contact") is None
    assert [r.method for r in requests] == (
        ["PUT"] if operation == "create" else ["PROPFIND", "DELETE"]
    )


@pytest.mark.parametrize("via_mcp", [False, True], ids=["direct", "mcp"])
@pytest.mark.parametrize("operation", ["create", "delete"])
@pytest.mark.parametrize("failure_status", [None, 403, 404, 409, 412])
async def test_addressbook_mutation_receipt(
    mocker: MockerFixture,
    mutation_server: NextcloudMCPServer,
    via_mcp: bool,
    operation: str,
    failure_status: int | None,
) -> None:
    """Address book writes return typed receipts only when the DAV request succeeds."""
    requests: list[httpx2.Request] = []
    status = failure_status or (201 if operation == "create" else 204)

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == ("MKCOL" if operation == "create" else "DELETE")
        assert request.url.path == (
            "/remote.php/dav/addressbooks/users/canonical-user/friends/"
        )
        return httpx2.Response(status)

    async with httpx2.AsyncClient(
        base_url="https://nextcloud.example", transport=httpx2.MockTransport(handle)
    ) as http:
        contacts = ContactsClient(http, "login-name")
        contacts._principal_id = "canonical-user"
        contacts._principal_discovered = True
        mocker.patch(
            "nextcloud_mcp_server.server.contacts.get_client",
            return_value=SimpleNamespace(contacts=contacts),
        )
        arguments = {"name": "friends"}
        if operation == "create":
            arguments["display_name"] = "My Friends"
        if via_mcp:
            async with Client(mutation_server) as client:
                result = await client.call_tool(
                    f"nc_contacts_{operation}_addressbook", arguments
                )
            if failure_status:
                assert result.is_error
                assert result.content
                assert result.structured_content is None
                assert len(requests) == 1
                return
            assert not result.is_error
            assert len(result.content) == 1
            assert isinstance(result.content[0], TextContent)
            payload = json.loads(result.content[0].text)
            assert payload == result.structured_content
            assert payload["success"] is True
            assert payload["timestamp"]
            if operation == "create":
                CreateAddressBookResponse.model_validate(payload)
            else:
                DeleteAddressBookResponse.model_validate(payload)
        else:
            mutation = getattr(contacts, f"{operation}_addressbook")(**arguments)
            if failure_status:
                with pytest.raises(httpx2.HTTPStatusError) as exc:
                    await mutation
                assert exc.value.response.status_code == failure_status
                assert len(requests) == 1
                return
            payload = await mutation

    assert payload["status_code"] == status
    assert "resource_path" not in payload
    if operation == "create":
        assert payload["addressbook"]["uri"] == "friends"
        assert payload["addressbook"]["displayname"] == "My Friends"
        assert payload["addressbook"].get("ctag") is None
    else:
        assert payload["deleted_name"] == "friends"
    assert len(requests) == 1


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
    """Failed contact writes remain errors through the client and MCP boundary."""
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.method == "PROPFIND":
            # No match retains the existing fallback to <uid>.vcf, including 404.
            return httpx2.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')
        assert request.url.path.endswith("/contacts/missing.vcf")
        return httpx2.Response(status)

    async with httpx2.AsyncClient(
        base_url="https://nextcloud.example", transport=httpx2.MockTransport(handle)
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
            with pytest.raises(httpx2.HTTPStatusError) as exc:
                await mutation
            assert exc.value.response.status_code == status

    assert [r.method for r in requests] == (
        ["PUT"] if operation == "create" else ["PROPFIND", "DELETE"]
    )
