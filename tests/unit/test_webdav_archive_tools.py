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
