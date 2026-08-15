"""Unit tests for WebDAVClient file comments — wire format and parsing.

The comments collection (``/remote.php/dav/comments/files/{fileId}``) is the
only place this client speaks REPORT, and the create path is the only one that
reads its result out of a response *header*. Both are pinned here.
"""

import pytest
from httpx import AsyncClient

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


@pytest.fixture
def webdav_client(mocker):
    """WebDAVClient with a mocked underlying httpx client."""
    return WebDAVClient(mocker.AsyncMock(spec=AsyncClient), "testuser")


def _report_response(mocker, xml: str):
    response = mocker.Mock()
    response.content = xml.encode()
    response.raise_for_status = mocker.Mock()
    return response


_TWO_COMMENTS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/comments/files/42/7</d:href>
    <d:propstat>
      <d:prop>
        <oc:id>7</oc:id>
        <oc:message>Please review @"alice"</oc:message>
        <oc:actorId>bob</oc:actorId>
        <oc:actorType>users</oc:actorType>
        <oc:actorDisplayName>Bob</oc:actorDisplayName>
        <oc:creationDateTime>Sat, 15 Aug 2026 08:00:00 GMT</oc:creationDateTime>
        <oc:verb>comment</oc:verb>
        <oc:isUnread>true</oc:isUnread>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/comments/files/42/6</d:href>
    <d:propstat>
      <d:prop>
        <oc:id>6</oc:id>
        <oc:message>Looks good</oc:message>
        <oc:actorId>alice</oc:actorId>
        <oc:actorType>users</oc:actorType>
        <oc:verb>comment</oc:verb>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
    <d:propstat>
      <d:prop>
        <oc:actorDisplayName/>
        <oc:creationDateTime/>
      </d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


async def test_list_comments_request_and_parsing(webdav_client, mocker):
    """REPORT with an oc:filter-comments body; 404 propstat blocks ignored."""
    make_request = mocker.patch.object(
        WebDAVClient,
        "_make_request",
        return_value=_report_response(mocker, _TWO_COMMENTS),
    )

    comments = await webdav_client.list_comments(42, limit=5, offset=10)

    method, url = make_request.call_args.args
    assert method == "REPORT"
    assert url == "/remote.php/dav/comments/files/42"
    body = make_request.call_args.kwargs["content"]
    assert "<oc:limit>5</oc:limit>" in body
    assert "<oc:offset>10</oc:offset>" in body
    headers = make_request.call_args.kwargs["headers"]
    assert headers["Depth"] == "0"
    assert headers["Content-Type"] == "text/xml"
    # Sent on every WebDAV request in this client; a reverse proxy may enforce
    # it as a CSRF guard even where the dev stack does not.
    assert headers["OCS-APIRequest"] == "true"

    assert [c["id"] for c in comments] == [7, 6]
    assert comments[0] == {
        "id": 7,
        "message": 'Please review @"alice"',
        "actor_id": "bob",
        "actor_type": "users",
        "actor_display_name": "Bob",
        "creation_datetime": "Sat, 15 Aug 2026 08:00:00 GMT",
        "verb": "comment",
        "is_unread": True,
    }
    # The properties the server reported as 404 must not be fabricated as "".
    assert comments[1]["actor_display_name"] is None
    assert comments[1]["creation_datetime"] is None
    assert comments[1]["is_unread"] is False


async def test_list_comments_skips_row_without_id(webdav_client, mocker):
    """One malformed comment costs that comment, not the whole thread."""
    xml = """<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
      <d:response>
        <d:href>/remote.php/dav/comments/files/42/</d:href>
        <d:propstat>
          <d:prop><oc:message>collection itself, no id</oc:message></d:prop>
          <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
      </d:response>
      <d:response>
        <d:href>/remote.php/dav/comments/files/42/6</d:href>
        <d:propstat>
          <d:prop><oc:id>6</oc:id><oc:message>real one</oc:message></d:prop>
          <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
      </d:response>
    </d:multistatus>"""
    mocker.patch.object(
        WebDAVClient, "_make_request", return_value=_report_response(mocker, xml)
    )

    comments = await webdav_client.list_comments(42)

    assert [c["id"] for c in comments] == [6]


async def test_create_comment_payload_and_id(webdav_client, mocker):
    """POST the JSON body Nextcloud expects; read the id off Content-Location."""
    response = mocker.Mock()
    response.headers = {"Content-Location": "/remote.php/dav/comments/files/42/99"}
    make_request = mocker.patch.object(
        WebDAVClient, "_make_request", return_value=response
    )

    comment_id = await webdav_client.create_comment(42, 'ping @"alice"')

    assert comment_id == 99
    method, url = make_request.call_args.args
    assert method == "POST"
    assert url == "/remote.php/dav/comments/files/42"
    assert make_request.call_args.kwargs["json"] == {
        "actorType": "users",
        "verb": "comment",
        "message": 'ping @"alice"',
    }
    assert make_request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"


async def test_create_comment_without_content_location(webdav_client, mocker):
    """No usable location means no id — but the comment was still posted."""
    response = mocker.Mock()
    response.headers = {}
    mocker.patch.object(WebDAVClient, "_make_request", return_value=response)

    assert await webdav_client.create_comment(42, "hi") is None
