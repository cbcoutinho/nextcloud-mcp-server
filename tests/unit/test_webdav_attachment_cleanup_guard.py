"""Unit tests for the old-attachment-directory cleanup guard.

``NotesClient.update`` calls ``cleanup_old_attachment_directory`` after a
category change. The Notes app relocates ``.attachments.<note_id>`` to the new
category itself as part of that change, so the old path is normally already
gone and the DELETE is a no-op.

The hazard this pins: when the server has *not* relocated the files yet, the
old directory still holds the user's live attachments — and an unconditional
DELETE there destroys them. The cleanup must remove only an empty husk.
"""

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


def _make_client(mocker) -> Any:
    # Any so the mocked list_directory/delete_resource assignments don't trip
    # ty's invalid-assignment on the real signatures.
    client: Any = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    return client


def _not_found() -> httpx.HTTPStatusError:
    request = httpx.Request("PROPFIND", "http://nc/dav")
    return httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )


async def test_non_empty_old_directory_is_not_deleted(mocker):
    """The server hasn't moved the files yet -- deleting would destroy them."""
    client = _make_client(mocker)
    client.list_directory = AsyncMock(
        return_value=[{"path": "/Notes/Old/.attachments.7/receipt.pdf"}]
    )
    client.delete_resource = AsyncMock()

    result = await client.cleanup_old_attachment_directory(
        note_id=7, old_category="Old"
    )

    client.delete_resource.assert_not_called()
    assert result["deleted"] is False
    assert result["status_code"] == 412


async def test_empty_old_directory_is_deleted(mocker):
    """The husk left behind after a successful server-side move is removed."""
    client = _make_client(mocker)
    client.list_directory = AsyncMock(return_value=[])
    client.delete_resource = AsyncMock(return_value={"status_code": 204})

    result = await client.cleanup_old_attachment_directory(
        note_id=7, old_category="Old"
    )

    client.delete_resource.assert_awaited_once_with(path="Notes/Old/.attachments.7/")
    assert result["status_code"] == 204


async def test_already_gone_old_directory_still_issues_the_delete(mocker):
    """A 404 from the probe means the move already happened; DELETE no-ops."""
    client = _make_client(mocker)
    client.list_directory = AsyncMock(side_effect=_not_found())
    client.delete_resource = AsyncMock(return_value={"status_code": 404})

    result = await client.cleanup_old_attachment_directory(
        note_id=7, old_category="Old"
    )

    client.delete_resource.assert_awaited_once()
    assert result["status_code"] == 404


async def test_uncategorised_note_probes_the_root_notes_path(mocker):
    """An empty old category means the attachments dir sits directly in Notes/."""
    client = _make_client(mocker)
    client.list_directory = AsyncMock(return_value=[])
    client.delete_resource = AsyncMock(return_value={"status_code": 204})

    await client.cleanup_old_attachment_directory(note_id=7, old_category="")

    client.list_directory.assert_awaited_once_with("Notes/.attachments.7/")
