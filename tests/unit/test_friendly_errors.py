"""Unit tests for the LLM-friendly tool-error formatter (GH #1208)."""

import httpx
import pytest
from httpx import HTTPStatusError
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.client.collectives import OCSError
from nextcloud_mcp_server.errors import NextcloudMCPServer, friendly_tool_error
from nextcloud_mcp_server.server.collectives import _raise_collectives_error

pytestmark = pytest.mark.unit

DAV_URL = "http://app/remote.php/dav/files/admin/FileUpload/test.txtg"


def _http_error(
    status: int,
    url: str = DAV_URL,
    body: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, text=body, headers=headers, request=request)
    return httpx.HTTPStatusError("raw httpx text", request=request, response=response)


def test_dav_404_names_the_user_relative_path_and_no_internals():
    message = friendly_tool_error(_http_error(404), "nc_webdav_read_file")

    assert message is not None
    assert 'nc_webdav_read_file failed: Not found — "FileUpload/test.txtg"' in message
    assert "HTTP 404" in message
    assert "typos" in message
    # The whole point: no internal URL, no host, no MDN link.
    assert "remote.php" not in message
    assert "developer.mozilla.org" not in message
    assert "http://app" not in message


def test_app_api_path_drops_the_index_php_entry_point():
    message = friendly_tool_error(
        _http_error(404, url="http://app/index.php/apps/notes/api/v1/notes/5"),
        "nc_notes_get_note",
    )

    assert message is not None
    assert '"apps/notes/api/v1/notes/5"' in message
    assert "index.php" not in message


def test_dav_home_root_does_not_render_as_an_empty_resource():
    message = friendly_tool_error(
        _http_error(403, url="http://app/remote.php/dav/files/admin/"),
        "nc_webdav_list_directory",
    )

    assert message is not None
    assert '""' not in message
    assert "dav/files/admin/" in message


def test_412_tells_the_model_to_re_read_the_etag():
    message = friendly_tool_error(_http_error(412), "nc_webdav_write_file")

    assert message is not None
    assert "changed since it was read" in message
    assert "etag" in message


def test_429_says_back_off_not_re_check_the_arguments():
    """_stream_request re-raises a 429 once its own retries are exhausted."""
    message = friendly_tool_error(_http_error(429), "nc_webdav_read_file")

    assert message is not None
    assert "Rate limited" in message
    assert "Wait before retrying" in message


def test_5xx_is_described_as_transient():
    message = friendly_tool_error(_http_error(503), "nc_tables_insert_row")

    assert message is not None
    assert "Nextcloud server error" in message
    assert "transient" in message


def test_unmapped_4xx_falls_back_to_the_generic_client_hint():
    message = friendly_tool_error(_http_error(418), "nc_notes_create_note")

    assert message is not None
    assert "Request rejected" in message
    assert "HTTP 418" in message


def test_ocs_json_message_is_appended():
    body = '{"ocs": {"meta": {"status": "failure", "message": "Path already exists"}}}'
    message = friendly_tool_error(
        _http_error(409, body=body, headers={"content-type": "application/json"}),
        "nc_share_create",
    )

    assert message is not None
    assert "Server said: Path already exists" in message


def test_plain_json_message_and_error_keys_are_used():
    for body, expected in (
        ('{"message": "Quota exceeded"}', "Quota exceeded"),
        ('{"error": "invalid table id"}', "invalid table id"),
    ):
        message = friendly_tool_error(_http_error(400, body=body), "nc_tool")
        assert message is not None
        assert f"Server said: {expected}" in message


def test_dav_xml_message_is_appended():
    body = (
        '<?xml version="1.0"?><d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">'
        "<s:message>File with name /foo could not be located</s:message></d:error>"
    )
    message = friendly_tool_error(_http_error(404, body=body), "nc_webdav_read_file")

    assert message is not None
    assert "Server said: File with name /foo could not be located" in message


def test_html_error_page_is_not_echoed():
    body = "<!DOCTYPE html>\n<html><body>Internal Server Error</body></html>"
    message = friendly_tool_error(_http_error(500, body=body), "nc_tool")

    assert message is not None
    assert "Server said" not in message
    assert "<html" not in message


def test_long_server_detail_is_truncated():
    body = '{"message": "%s"}' % ("x" * 500)
    message = friendly_tool_error(_http_error(400, body=body), "nc_tool")

    assert message is not None
    assert "…" in message
    assert "x" * 300 not in message


def test_unread_streaming_response_formats_without_raising():
    """The streaming download path raises with the body still unread."""
    request = httpx.Request("GET", DAV_URL)
    response = httpx.Response(404, stream=httpx.ByteStream(b"ignored"), request=request)
    exc = httpx.HTTPStatusError("raw", request=request, response=response)

    with pytest.raises(httpx.ResponseNotRead):
        _ = response.text  # precondition: the body really is unread

    message = friendly_tool_error(exc, "nc_webdav_read_file")

    assert message is not None
    assert '"FileUpload/test.txtg"' in message
    assert "Server said" not in message


def test_synthetic_response_without_a_bound_request_is_safe():
    """client/mail.py and auth/client_registration.py raise with one of these."""
    request = httpx.Request("POST", "http://app/index.php/apps/mail/api/messages")
    synthetic = httpx.Response(500, text="boom")
    exc = httpx.HTTPStatusError("raw", request=request, response=synthetic)

    with pytest.raises(RuntimeError):
        _ = synthetic.url  # precondition: reading it via the response explodes

    message = friendly_tool_error(exc, "nc_mail_send")

    assert message is not None
    assert '"apps/mail/api/messages"' in message


def test_request_error_reports_unreachable_server():
    exc = httpx.ConnectError("nope", request=httpx.Request("GET", DAV_URL))

    message = friendly_tool_error(exc, "nc_webdav_list_directory")

    assert message is not None
    assert "could not reach Nextcloud" in message
    assert "ConnectError" in message


@pytest.mark.parametrize(
    "exc",
    [
        MCPError(code=-1, message="Note 5 not found"),
        ValueError("bad argument"),
        None,
    ],
)
def test_other_exceptions_are_left_alone(exc):
    """None means 'no improvement' -- tailored messages keep their wording."""
    assert friendly_tool_error(exc, "nc_notes_get_note") is None


def test_collectives_lets_http_errors_reach_the_boundary():
    """Regression guard: re-wrapping in str(e) would shadow the new message.

    ``OCSError`` carries a real server message and still becomes an
    ``MCPError``; a transport-level error must arrive at the tool boundary
    intact so ``friendly_tool_error`` can render it.
    """
    http_error = _http_error(404)
    with pytest.raises(HTTPStatusError) as raised:
        _raise_collectives_error(http_error)
    assert raised.value is http_error

    with pytest.raises(MCPError) as wrapped:
        _raise_collectives_error(OCSError(403, "Not permitted"))
    assert "Not permitted" in str(wrapped.value)


async def test_boundary_rewrites_http_errors_but_not_tailored_ones():
    """The MCPServer override is what actually reaches the client."""
    mcp = NextcloudMCPServer("test")

    @mcp.tool()
    async def leaky_tool() -> str:
        raise _http_error(404)

    @mcp.tool()
    async def tailored_tool() -> str:
        raise MCPError(code=-1, message="Note 5 not found")

    with pytest.raises(ToolError) as leaky:
        await mcp.call_tool("leaky_tool", {})
    assert 'Not found — "FileUpload/test.txtg"' in str(leaky.value)
    assert "developer.mozilla.org" not in str(leaky.value)

    with pytest.raises(ToolError) as tailored:
        await mcp.call_tool("tailored_tool", {})
    assert "Note 5 not found" in str(tailored.value)
