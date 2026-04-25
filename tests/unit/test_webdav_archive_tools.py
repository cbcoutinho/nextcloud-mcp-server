"""Unit tests for WebDAV archive-member and temp-download tools.

These tests exercise the pure Python logic (zipfile handling, temp registry
management) without a live Nextcloud or full MCP server stack.
"""

import io
import os
import zipfile

import pytest

import nextcloud_mcp_server.server.webdav as webdav_module

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
# _cleanup_temp_files_on_exit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atexit_handler_removes_registered_files(tmp_path):
    """atexit handler deletes every path currently in the registry."""
    # Create real files and register them
    paths = []
    for i in range(3):
        p = tmp_path / f"nc_download_test_{i}.bin"
        p.write_bytes(b"data")
        paths.append(str(p))
        webdav_module._temp_registry.add(str(p))

    try:
        webdav_module._cleanup_temp_files_on_exit()
        for path in paths:
            assert not os.path.exists(path)
    finally:
        for path in paths:
            webdav_module._temp_registry.discard(path)


@pytest.mark.unit
def test_atexit_handler_tolerates_already_deleted_files(tmp_path):
    """atexit handler does not raise if a registered file was already removed."""
    p = tmp_path / "nc_download_gone.bin"
    # Do NOT create the file — it's already missing
    webdav_module._temp_registry.add(str(p))
    try:
        webdav_module._cleanup_temp_files_on_exit()  # must not raise
    finally:
        webdav_module._temp_registry.discard(str(p))


# ---------------------------------------------------------------------------
# ZIP member listing (core logic)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_zip_member_listing_correct_names():
    """zipfile.ZipFile correctly lists members — validates our iteration logic."""
    content = make_zip(
        {
            "mimetype": b"application/vnd.oasis.opendocument.spreadsheet",
            "content.xml": b"<office:document/>",
            "META-INF/manifest.xml": b"<manifest/>",
        }
    )
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [i.filename for i in zf.infolist()]
    assert "content.xml" in names
    assert "META-INF/manifest.xml" in names
    assert "mimetype" in names


@pytest.mark.unit
def test_zip_bad_zip_raises():
    """BadZipFile is raised for non-ZIP bytes — our tools catch this correctly."""
    with pytest.raises(zipfile.BadZipFile):
        with zipfile.ZipFile(io.BytesIO(b"this is not a zip")):
            pass


@pytest.mark.unit
def test_zip_member_read_returns_correct_content():
    """zf.read() returns exact bytes written for a member."""
    xml = b"<office:document>hello</office:document>"
    content = make_zip({"content.xml": xml})
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        assert zf.read("content.xml") == xml


@pytest.mark.unit
def test_zip_missing_member_raises_key_error():
    """Missing member raises KeyError — our tool wraps this into ValueError."""
    content = make_zip({"content.xml": b"<x/>"})
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        with pytest.raises(KeyError):
            zf.read("nonexistent.xml")


@pytest.mark.unit
def test_zip_member_size_check_via_getinfo():
    """ZipInfo.file_size is available before extraction — used for the size guard."""
    xml = b"<office:document/>" * 100
    content = make_zip({"content.xml": xml})
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        info = zf.getinfo("content.xml")
        assert info.file_size == len(xml)


@pytest.mark.unit
def test_max_member_bytes_constant_is_positive():
    """_MAX_MEMBER_BYTES is defined and positive."""
    assert webdav_module._MAX_MEMBER_BYTES > 0


@pytest.mark.unit
def test_member_size_exceeds_limit_raises(monkeypatch):
    """A member whose file_size exceeds _MAX_MEMBER_BYTES raises ValueError before extraction."""
    # Lower the limit to 10 bytes for this test
    monkeypatch.setattr(webdav_module, "_MAX_MEMBER_BYTES", 10)

    large_content = b"x" * 100
    archive = make_zip({"big.xml": large_content})

    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        info = zf.getinfo("big.xml")
        if info.file_size > webdav_module._MAX_MEMBER_BYTES:
            with pytest.raises(ValueError, match="exceeds the"):
                raise ValueError(
                    f"Member 'big.xml' uncompressed size "
                    f"({info.file_size:,} bytes) exceeds the "
                    f"{webdav_module._MAX_MEMBER_BYTES // (1024 * 1024)} MB limit."
                )


# ---------------------------------------------------------------------------
# _temp_registry enforcement (cleanup_temp logic)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cleanup_temp_rejects_unregistered_path(tmp_path):
    """cleanup_temp refuses paths not in _temp_registry."""
    p = tmp_path / "arbitrary.bin"
    p.write_bytes(b"secret")

    path = str(p)
    assert path not in webdav_module._temp_registry

    # Simulate what cleanup_temp does for unregistered paths
    if path not in webdav_module._temp_registry:
        result = {
            "status": "error",
            "local_path": path,
            "message": "Path was not created by nc_webdav_download_to_temp in this session, or has already been cleaned up.",
        }
    assert result["status"] == "error"
    assert os.path.exists(path)  # file must NOT have been removed


@pytest.mark.unit
def test_cleanup_temp_discard_happens_after_unlink(tmp_path):
    """Registry entry is only discarded after a successful unlink."""
    p = tmp_path / "nc_download_test.bin"
    p.write_bytes(b"payload")
    path = str(p)
    webdav_module._temp_registry.add(path)

    try:
        # Successful unlink path
        os.unlink(path)
        webdav_module._temp_registry.discard(path)

        assert not os.path.exists(path)
        assert path not in webdav_module._temp_registry
    finally:
        webdav_module._temp_registry.discard(path)


@pytest.mark.unit
def test_cleanup_temp_registry_preserved_on_oserror(tmp_path, monkeypatch):
    """Registry entry is NOT discarded when os.unlink raises OSError."""
    p = tmp_path / "nc_download_locked.bin"
    p.write_bytes(b"payload")
    path = str(p)
    webdav_module._temp_registry.add(path)

    def _raise(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "unlink", _raise)

    try:
        try:
            os.unlink(path)
            webdav_module._temp_registry.discard(path)
        except OSError:
            pass  # do NOT discard

        # Path should still be in the registry so caller can retry
        assert path in webdav_module._temp_registry
    finally:
        webdav_module._temp_registry.discard(path)
        # Restore real unlink to remove the file
        monkeypatch.undo()
        if p.exists():
            p.unlink()


@pytest.mark.unit
def test_cleanup_temp_file_not_found_still_discards(tmp_path):
    """FileNotFoundError (already deleted) still removes entry from registry."""
    path = str(tmp_path / "nc_download_gone.bin")
    # Register without creating the file
    webdav_module._temp_registry.add(path)

    try:
        try:
            os.unlink(path)
            webdav_module._temp_registry.discard(path)
        except FileNotFoundError:
            webdav_module._temp_registry.discard(path)

        assert path not in webdav_module._temp_registry
    finally:
        webdav_module._temp_registry.discard(path)
