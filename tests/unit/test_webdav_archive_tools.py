"""Unit tests for WebDAV archive-member and temp-download tools.

All tests call the real production functions (_list_zip_members,
_read_zip_member, _cleanup_temp_path, _cleanup_temp_files_on_exit,
_temp_registry) so that regressions in the implementation are caught rather
than just verifying stdlib zipfile behaviour.
"""

import io
import os
import zipfile

import pytest

import nextcloud_mcp_server.server.webdav as webdav_module
from nextcloud_mcp_server.server.webdav import (
    _cleanup_temp_path,
    _list_zip_members,
    _read_zip_member,
    _temp_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_zip(members: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP archive from a {name: content} mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _list_zip_members
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_members_returns_expected_structure():
    """_list_zip_members returns correct member names, sizes, and metadata."""
    content = make_zip(
        {
            "mimetype": b"application/vnd.oasis.opendocument.spreadsheet",
            "content.xml": b"<office:document/>",
            "META-INF/manifest.xml": b"<manifest/>",
        }
    )
    result = _list_zip_members(
        content, "test.ods", "application/vnd.oasis.opendocument.spreadsheet"
    )

    assert result["path"] == "test.ods"
    assert result["member_count"] == 3
    assert result["archive_size"] == len(content)

    names = {m["name"] for m in result["members"]}
    assert names == {"mimetype", "content.xml", "META-INF/manifest.xml"}

    content_xml = next(m for m in result["members"] if m["name"] == "content.xml")
    assert content_xml["size"] == len(b"<office:document/>")
    assert content_xml["is_dir"] is False


@pytest.mark.unit
def test_list_members_bad_zip_raises_value_error():
    """_list_zip_members raises ValueError (not BadZipFile) for non-ZIP bytes."""
    with pytest.raises(ValueError, match="not a valid ZIP archive"):
        _list_zip_members(b"this is not a zip", "bad.ods", "application/octet-stream")


@pytest.mark.unit
def test_list_members_truncated_when_over_limit():
    """_list_zip_members truncates results and sets truncated=True when limit exceeded."""
    members = {f"file_{i}.xml": b"<x/>" for i in range(10)}
    content = make_zip(members)
    result = _list_zip_members(content, "big.zip", "application/zip", max_members=3)

    assert result["member_count"] == 10
    assert len(result["members"]) == 3
    assert result["truncated"] is True
    assert result["truncated_at"] == 3


@pytest.mark.unit
def test_list_members_no_truncation_flag_when_within_limit():
    """_list_zip_members does not set truncated when all members fit."""
    content = make_zip({"a.xml": b"<x/>", "b.xml": b"<y/>"})
    result = _list_zip_members(content, "small.zip", "application/zip", max_members=10)

    assert result["member_count"] == 2
    assert len(result["members"]) == 2
    assert "truncated" not in result


@pytest.mark.unit
def test_list_members_includes_content_type_in_result():
    """content_type from Nextcloud is passed through to the result dict."""
    content = make_zip({"x.xml": b"<x/>"})
    mime = "application/vnd.oasis.opendocument.spreadsheet"
    result = _list_zip_members(content, "sheet.ods", mime)
    assert result["content_type"] == mime


# ---------------------------------------------------------------------------
# _read_zip_member — text detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_member_xml_returned_as_utf8():
    """XML member (content.xml) is returned as a UTF-8 string, not base64."""
    xml = b"<office:document>hello</office:document>"
    content = make_zip({"content.xml": xml})
    result = _read_zip_member(content, "test.ods", "content.xml")

    assert result["content"] == xml.decode("utf-8")
    assert "encoding" not in result
    assert result["size"] == len(xml)


@pytest.mark.unit
def test_read_member_rels_returned_as_utf8():
    """.rels files (OOXML relationship files) are returned as text, not base64."""
    rels = b'<?xml version="1.0"?><Relationships/>'
    content = make_zip({"_rels/.rels": rels})
    result = _read_zip_member(content, "test.docx", "_rels/.rels")
    assert result["content"] == rels.decode("utf-8")
    assert "encoding" not in result


@pytest.mark.unit
def test_read_member_xhtml_returned_as_utf8():
    """.xhtml files (common in EPUB) are returned as text."""
    xhtml = b"<html><body>hello</body></html>"
    content = make_zip({"OEBPS/chapter1.xhtml": xhtml})
    result = _read_zip_member(content, "book.epub", "OEBPS/chapter1.xhtml")
    assert result["content"] == xhtml.decode("utf-8")
    assert "encoding" not in result


@pytest.mark.unit
def test_read_member_opf_returned_as_utf8():
    """.opf files (EPUB Open Packaging Format) are returned as text."""
    opf = b"<?xml version='1.0'?><package/>"
    content = make_zip({"OEBPS/content.opf": opf})
    result = _read_zip_member(content, "book.epub", "OEBPS/content.opf")
    assert result["content"] == opf.decode("utf-8")
    assert "encoding" not in result


@pytest.mark.unit
def test_read_member_extensionless_text_returned_as_utf8():
    """Extensionless text members (e.g. ODF 'mimetype') are detected via content sniff."""
    mime_content = b"application/vnd.oasis.opendocument.spreadsheet"
    content = make_zip({"mimetype": mime_content})
    result = _read_zip_member(content, "test.ods", "mimetype")
    assert result["content"] == mime_content.decode("utf-8")
    assert "encoding" not in result


@pytest.mark.unit
def test_read_member_binary_returned_as_base64():
    """Binary members (e.g. embedded images) are base64-encoded."""
    import base64

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    content = make_zip({"image.png": png_bytes})
    result = _read_zip_member(content, "test.ods", "image.png")

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == png_bytes


# ---------------------------------------------------------------------------
# _read_zip_member — error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_member_directory_entry_raises_value_error():
    """Passing a directory member path raises ValueError, not a stdlib error."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # ZipFile.mkdir creates a directory entry
        zf.mkdir("subdir/")
        zf.writestr("subdir/content.xml", b"<x/>")
    content = buf.getvalue()

    with pytest.raises(ValueError, match="directory entry"):
        _read_zip_member(content, "test.ods", "subdir/")


@pytest.mark.unit
def test_read_member_missing_member_raises_value_error():
    """Missing member raises ValueError with the available file list."""
    content = make_zip({"content.xml": b"<x/>"})
    with pytest.raises(ValueError, match="not found"):
        _read_zip_member(content, "test.ods", "nonexistent.xml")


@pytest.mark.unit
def test_read_member_bad_zip_raises_value_error():
    """Non-ZIP bytes raise ValueError (not BadZipFile)."""
    with pytest.raises(ValueError, match="not a valid ZIP archive"):
        _read_zip_member(b"garbage", "test.ods", "content.xml")


@pytest.mark.unit
def test_read_member_size_limit_enforced(monkeypatch):
    """Members exceeding _MAX_MEMBER_BYTES raise ValueError before extraction."""
    monkeypatch.setattr(webdav_module, "_MAX_MEMBER_BYTES", 10)

    large_data = b"x" * 100  # well above the patched 10-byte limit
    content = make_zip({"big.xml": large_data})

    with pytest.raises(ValueError, match="exceeds the"):
        _read_zip_member(content, "test.ods", "big.xml")


@pytest.mark.unit
def test_read_member_size_limit_not_triggered_for_small_member(monkeypatch):
    """Members within _MAX_MEMBER_BYTES are extracted without error."""
    monkeypatch.setattr(webdav_module, "_MAX_MEMBER_BYTES", 200)

    small_data = b"<x/>" * 10  # 40 bytes, well within 200
    content = make_zip({"small.xml": small_data})

    result = _read_zip_member(content, "test.ods", "small.xml")
    assert result["content"] == small_data.decode("utf-8")


# ---------------------------------------------------------------------------
# _cleanup_temp_files_on_exit and _temp_registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atexit_handler_removes_registered_files(tmp_path):
    """atexit handler deletes every path currently in the registry."""
    paths = []
    for i in range(3):
        p = tmp_path / f"nc_download_test_{i}.bin"
        p.write_bytes(b"data")
        paths.append(str(p))
        _temp_registry[str(p)] = "testuser"

    try:
        webdav_module._cleanup_temp_files_on_exit()
        for path in paths:
            assert not os.path.exists(path)
    finally:
        for path in paths:
            _temp_registry.pop(path, None)


@pytest.mark.unit
def test_atexit_handler_tolerates_already_deleted_files(tmp_path):
    """atexit handler does not raise if a registered file was already removed."""
    p = tmp_path / "nc_download_gone.bin"
    _temp_registry[str(p)] = "testuser"
    try:
        webdav_module._cleanup_temp_files_on_exit()  # must not raise
    finally:
        _temp_registry.pop(str(p), None)


# ---------------------------------------------------------------------------
# _cleanup_temp_path — calls real production helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cleanup_temp_rejects_unregistered_path(tmp_path):
    """_cleanup_temp_path returns an error dict for paths not in _temp_registry."""
    p = tmp_path / "arbitrary.bin"
    p.write_bytes(b"secret")
    path = str(p)

    assert path not in _temp_registry

    result = _cleanup_temp_path(path, owner="alice")

    assert result["status"] == "error"
    assert "session" in result["message"].lower()
    # File must be untouched
    assert os.path.exists(path)


@pytest.mark.unit
def test_cleanup_temp_rejects_wrong_owner(tmp_path):
    """_cleanup_temp_path rejects callers who don't own the file."""
    p = tmp_path / "nc_download_alice.bin"
    p.write_bytes(b"payload")
    path = str(p)
    _temp_registry[path] = "alice"

    try:
        result = _cleanup_temp_path(path, owner="bob")

        assert result["status"] == "error"
        assert "permission" in result["message"].lower()
        # File must be untouched
        assert os.path.exists(path)
        assert path in _temp_registry
    finally:
        _temp_registry.pop(path, None)
        if p.exists():
            p.unlink()


@pytest.mark.unit
def test_cleanup_temp_success(tmp_path):
    """_cleanup_temp_path deletes the file and removes it from the registry."""
    p = tmp_path / "nc_download_test.bin"
    p.write_bytes(b"payload")
    path = str(p)
    _temp_registry[path] = "alice"

    try:
        result = _cleanup_temp_path(path, owner="alice")

        assert result["status"] == "ok"
        assert result["local_path"] == path
        assert not os.path.exists(path)
        assert path not in _temp_registry
    finally:
        _temp_registry.pop(path, None)


@pytest.mark.unit
def test_cleanup_temp_registry_preserved_on_oserror(tmp_path, monkeypatch):
    """Registry entry is NOT discarded when os.unlink raises OSError (allows retry)."""
    p = tmp_path / "nc_download_locked.bin"
    p.write_bytes(b"payload")
    path = str(p)
    _temp_registry[path] = "alice"

    def _raise(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "unlink", _raise)

    try:
        result = _cleanup_temp_path(path, owner="alice")

        assert result["status"] == "error"
        assert "permission denied" in result["message"]
        # Entry must remain so the caller can retry.
        assert path in _temp_registry
    finally:
        _temp_registry.pop(path, None)
        monkeypatch.undo()
        if p.exists():
            p.unlink()


@pytest.mark.unit
def test_cleanup_temp_file_not_found_discards_registry(tmp_path):
    """FileNotFoundError (file already gone) still removes the registry entry."""
    path = str(tmp_path / "nc_download_gone.bin")
    # Register a path for a file that does NOT exist on disk.
    _temp_registry[path] = "alice"

    try:
        result = _cleanup_temp_path(path, owner="alice")

        assert result["status"] == "ok"
        assert path not in _temp_registry
    finally:
        _temp_registry.pop(path, None)


# ---------------------------------------------------------------------------
# MCP tool wiring — invoke real tool closures via FastMCP tool.run()
#
# tool.run(args_dict, context=None) passes ctx=None to the tool function.
# require_scopes treats ctx=None as BasicAuth mode and skips scope checks,
# so we can test the full wiring (get_client → read_file → helper → result)
# by mocking get_client at the module level.
# ---------------------------------------------------------------------------


def _make_tool_map():
    """Build a FastMCP instance and return its tool map (name → Tool)."""
    from mcp.server.fastmcp import FastMCP

    from nextcloud_mcp_server.server.webdav import configure_webdav_tools

    mcp = FastMCP("test")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.mark.unit
async def test_tool_list_archive_members_wiring(mocker):
    """nc_webdav_list_archive_members calls read_file and delegates to _list_zip_members."""
    zip_bytes = make_zip({"content.xml": b"<root/>", "mimetype": b"application/ods"})
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(zip_bytes, "application/vnd.oasis.opendocument.spreadsheet")
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )

    tools = _make_tool_map()
    result = await tools["nc_webdav_list_archive_members"].run(
        {"path": "docs/test.ods"}, context=None
    )

    mock_client.webdav.read_file.assert_awaited_once_with("docs/test.ods")
    assert result["path"] == "docs/test.ods"
    assert result["member_count"] == 2
    assert result["content_type"] == "application/vnd.oasis.opendocument.spreadsheet"


@pytest.mark.unit
async def test_tool_read_archive_member_wiring(mocker):
    """nc_webdav_read_archive_member calls read_file and delegates to _read_zip_member."""
    xml = b"<office:document/>"
    zip_bytes = make_zip({"content.xml": xml})
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(zip_bytes, "application/zip")
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )

    tools = _make_tool_map()
    result = await tools["nc_webdav_read_archive_member"].run(
        {"path": "docs/test.ods", "member_path": "content.xml"}, context=None
    )

    mock_client.webdav.read_file.assert_awaited_once_with("docs/test.ods")
    assert result["content"] == xml.decode("utf-8")
    assert "encoding" not in result


@pytest.mark.unit
async def test_tool_download_to_temp_writes_file_and_registers_owner(mocker):
    """nc_webdav_download_to_temp writes bytes to disk and records username in registry."""
    file_content = b"binary payload"
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(file_content, "application/octet-stream")
    )
    mock_client.username = "alice"
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )

    tools = _make_tool_map()
    result = await tools["nc_webdav_download_to_temp"].run(
        {"path": "Videos/clip.mp4"}, context=None
    )

    local_path = result["local_path"]
    try:
        assert result["filename"] == "clip.mp4"
        assert result["size"] == len(file_content)
        assert os.path.exists(local_path)
        assert open(local_path, "rb").read() == file_content
        # Ownership must be recorded under alice's username
        assert _temp_registry.get(local_path) == "alice"
    finally:
        _temp_registry.pop(local_path, None)
        if os.path.exists(local_path):
            os.unlink(local_path)


@pytest.mark.unit
async def test_tool_cleanup_temp_passes_owner_from_client(mocker, tmp_path):
    """nc_webdav_cleanup_temp passes client.username as owner to _cleanup_temp_path."""
    p = tmp_path / "nc_download_wiring.bin"
    p.write_bytes(b"data")
    path = str(p)
    _temp_registry[path] = "alice"

    mock_client = mocker.AsyncMock()
    mock_client.username = "alice"
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )

    try:
        tools = _make_tool_map()
        result = await tools["nc_webdav_cleanup_temp"].run(
            {"local_path": path}, context=None
        )

        assert result["status"] == "ok"
        assert not os.path.exists(path)
        assert path not in _temp_registry
    finally:
        _temp_registry.pop(path, None)
        if p.exists():
            p.unlink()


@pytest.mark.unit
async def test_tool_list_archive_members_rejects_oversized_archive(mocker):
    """nc_webdav_list_archive_members raises ToolError when archive exceeds _MAX_ARCHIVE_BYTES."""
    from mcp.server.fastmcp.exceptions import ToolError

    oversized = b"x" * 200
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(oversized, "application/zip")
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )
    mocker.patch("nextcloud_mcp_server.server.webdav._MAX_ARCHIVE_BYTES", 100)

    tools = _make_tool_map()
    with pytest.raises(ToolError, match="exceeds the"):
        await tools["nc_webdav_list_archive_members"].run(
            {"path": "huge.zip"}, context=None
        )


@pytest.mark.unit
async def test_tool_read_archive_member_rejects_oversized_archive(mocker):
    """nc_webdav_read_archive_member raises ToolError when archive exceeds _MAX_ARCHIVE_BYTES."""
    from mcp.server.fastmcp.exceptions import ToolError

    oversized = b"x" * 200
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(oversized, "application/zip")
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )
    mocker.patch("nextcloud_mcp_server.server.webdav._MAX_ARCHIVE_BYTES", 100)

    tools = _make_tool_map()
    with pytest.raises(ToolError, match="exceeds the"):
        await tools["nc_webdav_read_archive_member"].run(
            {"path": "huge.zip", "member_path": "content.xml"}, context=None
        )


@pytest.mark.unit
async def test_tool_download_to_temp_rejects_oversized_file(mocker):
    """nc_webdav_download_to_temp raises ValueError when the file exceeds _MAX_TEMP_DOWNLOAD_BYTES."""
    oversized = b"x" * 100
    mock_client = mocker.AsyncMock()
    mock_client.webdav.read_file = mocker.AsyncMock(
        return_value=(oversized, "application/octet-stream")
    )
    mock_client.username = "alice"
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )
    mocker.patch("nextcloud_mcp_server.server.webdav._MAX_TEMP_DOWNLOAD_BYTES", 10)

    from mcp.server.fastmcp.exceptions import ToolError

    tools = _make_tool_map()
    with pytest.raises(ToolError, match="exceeds the"):
        await tools["nc_webdav_download_to_temp"].run({"path": "big.bin"}, context=None)


@pytest.mark.unit
async def test_tool_cleanup_temp_rejects_wrong_owner(mocker, tmp_path):
    """nc_webdav_cleanup_temp rejects a caller whose username doesn't match the registry."""
    p = tmp_path / "nc_download_wiring2.bin"
    p.write_bytes(b"data")
    path = str(p)
    _temp_registry[path] = "alice"

    mock_client = mocker.AsyncMock()
    mock_client.username = "bob"
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", return_value=mock_client
    )

    try:
        tools = _make_tool_map()
        result = await tools["nc_webdav_cleanup_temp"].run(
            {"local_path": path}, context=None
        )

        assert result["status"] == "error"
        assert "permission" in result["message"].lower()
        assert os.path.exists(path)
    finally:
        _temp_registry.pop(path, None)
        if p.exists():
            p.unlink()
