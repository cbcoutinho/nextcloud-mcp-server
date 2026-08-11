"""Office files are discovered against a real Nextcloud, not just a mock.

The allowlist is matched with ``content_type.startswith(indexable_mime_types)``,
so every entry has to be the exact string *Nextcloud* reports for that
extension. The unit tests assert the matching logic against strings this
codebase chose; nothing checked those strings against the server that produces
them. If Nextcloud labelled a ``.docx`` differently, discovery would silently
return nothing and every test above this layer would still pass.

This also exercises the concurrent per-type SEARCH fan-out in
``_walk_tagged_dir`` against a real Depth:infinity REPORT, where the unit test
drives a mocked webdav client.
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.vector.scanner import _discover_tagged_files

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration

# Smallest bytes each format's sniffer will accept. A .docx/.xlsx is a zip whose
# first entry is `[Content_Types].xml`; Nextcloud derives the type from the
# extension, but writing plausible bytes keeps the fixture honest if that ever
# changes to content sniffing.
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 26

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
async def tagged_office_folder(nc_client: NextcloudClient):
    """A tagged folder holding a PDF, a .docx, an .xlsx and one ignorable file."""
    suffix = uuid.uuid4().hex[:8]
    tag_name = f"mcp-office-{suffix}"
    test_dir = f"mcp_office_disc_{suffix}"

    files = {
        "pdf": (f"{test_dir}/report.pdf", PDF_BYTES, "application/pdf"),
        "docx": (f"{test_dir}/contract.docx", ZIP_BYTES, DOCX_MIME),
        "xlsx": (f"{test_dir}/sheet.xlsx", ZIP_BYTES, XLSX_MIME),
        # Not in the allowlist: proves the filter still excludes, so a green
        # result cannot come from the filter having been dropped entirely.
        "png": (f"{test_dir}/diagram.png", b"\x89PNG\r\n\x1a\n", "image/png"),
    }

    await nc_client.webdav.create_directory(test_dir)
    for path, data, content_type in files.values():
        await nc_client.webdav.write_file(path, data, content_type)

    tag = await nc_client.webdav.get_or_create_tag(
        name=tag_name, user_visible=True, user_assignable=True
    )
    # Tag the FOLDER, not the files: that is the path that fans out one SEARCH
    # per configured MIME type.
    folder_info = await nc_client.webdav.get_file_info(test_dir)
    await nc_client.webdav.assign_tag_to_file(folder_info["id"], tag["id"])

    reported = {}
    for key, (path, _, _) in files.items():
        info = await nc_client.webdav.get_file_info(path)
        reported[key] = info.get("content_type", "")

    yield {"tag": tag_name, "dir": test_dir, "reported": reported}

    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception as e:
        logger.warning("failed to delete %s: %s", test_dir, e)


async def test_nextcloud_reports_the_mime_types_the_allowlist_expects(
    tagged_office_folder,
):
    """The premise of the whole allowlist, checked against the real server."""
    reported = tagged_office_folder["reported"]
    indexable = Settings().indexable_mime_types

    assert reported["pdf"].startswith("application/pdf")
    assert reported["docx"].startswith(DOCX_MIME), (
        f"Nextcloud reports {reported['docx']!r} for .docx; the allowlist entry "
        f"would never match it"
    )
    assert reported["xlsx"].startswith(XLSX_MIME), (
        f"Nextcloud reports {reported['xlsx']!r} for .xlsx"
    )
    # And each of those is actually covered by the configured default.
    for key in ("pdf", "docx", "xlsx"):
        assert reported[key].startswith(indexable), (
            f"{key}: {reported[key]!r} is not matched by {indexable}"
        )


async def test_a_tagged_folder_expands_to_its_office_files(
    tagged_office_folder, nc_client: NextcloudClient
):
    """The concurrent per-type fan-out, against a real Depth:infinity REPORT."""
    settings = Settings()
    settings.vector_sync_tag = tagged_office_folder["tag"]
    settings.vector_sync_keyword_tag = ""

    discovered = await _discover_tagged_files(nc_client, settings)

    names = sorted(f["path"].rsplit("/", 1)[-1] for f in discovered)
    assert names == ["contract.docx", "report.pdf", "sheet.xlsx"], (
        f"expected the three indexable files, got {names}"
    )
    # The png is in the same tagged folder and must not be pulled in.
    assert not any(n.endswith(".png") for n in names)
