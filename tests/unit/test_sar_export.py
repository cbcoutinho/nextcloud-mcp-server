"""Unit tests for SAR export archives (ADR-040). Synthetic names only."""

import io
import json
import zipfile

import anyio
import httpx
import pymupdf
import pytest

from nextcloud_mcp_server import sar_export
from nextcloud_mcp_server.models.sar import (
    SarExportRequest,
    SarExportStatus,
    SarItem,
)
from nextcloud_mcp_server.providers.ner import NerError
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit

NAMES = ("Jane Doe", "Karen Smith", "Tom Brown")


class FakeNer:
    """Detects the synthetic NAMES, like tests/fixtures/ner_stub.py."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def detect(self, texts: list[str]) -> list[set[str]]:
        if self.fail:
            raise NerError("down")
        return [{n for n in NAMES if n in t} for t in texts]


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://nc/remote.php/dav/files/dpo/x")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeWebDAV:
    def __init__(self, existing: set[str] = frozenset()) -> None:
        self.files: dict[str, bytes] = {}
        self.existing = set(existing)
        self.fail_writes_with: int | None = None

    async def write_file(self, path, content, content_type=None, if_match=None):
        if self.fail_writes_with is not None:
            raise _http_error(self.fail_writes_with)
        if if_match is None and (path in self.files or path in self.existing):
            return {"status_code": 412, "message": "exists"}
        self.files[path] = content
        return {"status_code": 201, "etag": "e"}

    async def read_file(self, path):
        if path not in self.files:
            raise _http_error(404)
        return self.files[path], "application/json", "e"


class FakeClient:
    def __init__(self, webdav: FakeWebDAV) -> None:
        self.username = "dpo"
        self.webdav = webdav
        self.closed = False

    async def close(self) -> None:
        self.closed = True


DOCS = {
    "1": (
        "Letter re Karen Smith",
        "Jane Doe met Karen Smith (karen@example.org). Later Smith left.",
    ),
    "2": ("Minutes", "Tom Brown chaired. Jane Doe, 07700 900111, attended."),
}


@pytest.fixture
def indexed(monkeypatch):
    async def fake_document_text(nc, user_id, item):
        if item.doc_id not in DOCS:
            raise sar_export._ItemError("not in the search index")
        return DOCS[item.doc_id]

    monkeypatch.setattr(sar_export, "document_text", fake_document_text)


def _status(tmp: str = "/Team/SAR") -> SarExportStatus:
    archive, status = sar_export.archive_paths(tmp, "SAR-1")
    return SarExportStatus(
        state="running",
        archive_path=archive,
        status_path=status,
        total=3,
        processed=0,
        failed=0,
        started_at="t",
        updated_at="t",
    )


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "".join(page.get_text() for page in doc)


async def test_run_export_builds_redacted_archive(indexed):
    webdav = FakeWebDAV()
    nc = FakeClient(webdav)
    status = _status()
    items = [
        SarItem(
            doc_type="file",
            doc_id="1",
            reason="Karen Smith's complaint about the subject",
        ),
        SarItem(doc_type="note", doc_id="2", reason="Subject attended"),
        SarItem(doc_type="file", doc_id="99", reason="missing"),
    ]
    await sar_export.run_export(
        nc,
        FakeNer(),
        status,
        "SAR-1",
        keep=["Jane Doe", "07700 900111"],
        items=items,
        queries=["Jane Doe", "Karen Smith complaint"],
    )

    assert status.state == "done"
    assert status.processed == 3
    assert status.failed == 1
    assert status.failed_items[0].doc_id == "99"
    saved = json.loads(webdav.files["/Team/SAR/SAR-1.status.json"])
    assert saved["state"] == "done"

    zf = zipfile.ZipFile(io.BytesIO(webdav.files["/Team/SAR/SAR-1.zip"]))
    names = sorted(zf.namelist())
    assert names == [
        "documents/001-Letter-re-[PERSON_1].pdf",
        "documents/002-Minutes.pdf",
        "index.pdf",
        "searches.pdf",
    ]
    everything = " ".join(_pdf_text(zf.read(n)) for n in names)
    for leak in ("Karen", "Smith", "Tom Brown", "karen@example.org"):
        assert leak not in everything, leak
    assert "Jane Doe" in everything
    assert "07700 900111" in everything  # the subject's own number is kept
    assert "[EMAIL_1]" in everything
    # Archive-wide numbering: Karen Smith is PERSON_1 in the title, reason
    # and the query log alike.
    assert "[PERSON_1]'s complaint" in _pdf_text(zf.read("index.pdf"))
    assert "[PERSON_1] complaint" in _pdf_text(zf.read("searches.pdf"))
    index = _pdf_text(zf.read("index.pdf"))
    assert "Not exported: not in the search index" in index


async def test_ner_failure_fails_the_export_and_writes_nothing(indexed):
    webdav = FakeWebDAV()
    nc = FakeClient(webdav)
    status = _status()
    await sar_export._run_and_close(
        nc,
        FakeNer(fail=True),
        status,
        "SAR-1",
        ["Jane Doe"],
        [SarItem(doc_type="file", doc_id="1", reason="r")],
        [],
    )
    assert "/Team/SAR/SAR-1.zip" not in webdav.files
    saved = json.loads(webdav.files["/Team/SAR/SAR-1.status.json"])
    assert saved["state"] == "failed"
    assert saved["message"] == "export failed (NerError)"
    assert nc.closed


def _request(folder: str = "/Team/SAR") -> SarExportRequest:
    return SarExportRequest(
        output_folder=folder,
        name="SAR-1",
        subject=["Jane Doe"],
        items=[SarItem(doc_type="file", doc_id="1", reason="r")],
    )


@pytest.fixture
def background(monkeypatch):
    """submit_export's collaborators: the background client and NER client."""
    webdav = FakeWebDAV()
    nc = FakeClient(webdav)

    async def resolve(user_id):
        return nc

    async def ner_client(settings):
        return FakeNer()

    monkeypatch.setattr(sar_export, "resolve_background_client", resolve)
    monkeypatch.setattr(sar_export, "get_ner_client", ner_client)
    return nc


async def test_submit_runs_job_in_background(indexed, background):
    async with anyio.create_task_group() as tg:
        status = await sar_export.submit_export("dpo", _request(), tg)
        assert status.state == "running"
    assert "/Team/SAR/SAR-1.zip" in background.webdav.files
    assert background.closed
    done = await sar_export.read_status(background, "/Team/SAR", "SAR-1")
    assert done.state == "done"


async def test_submit_refuses_existing_name(indexed, background):
    background.webdav.existing.add("/Team/SAR/SAR-1.status.json")
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="already exists") as info:
            await sar_export.submit_export("dpo", _request("Team/SAR/"), tg)
    assert info.value.status == 409
    assert background.closed


async def test_submit_refuses_unwritable_folder(indexed, background):
    background.webdav.fail_writes_with = 403
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="writable") as info:
            await sar_export.submit_export("dpo", _request(), tg)
    assert info.value.status == 403
    assert background.closed


async def test_submit_needs_background_access(monkeypatch):
    async def resolve(user_id):
        raise NotProvisionedError("no app password")

    monkeypatch.setattr(sar_export, "resolve_background_client", resolve)
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="background access") as info:
            await sar_export.submit_export("dpo", _request(), tg)
    assert info.value.status == 403


async def test_submit_without_task_group_is_unavailable():
    with pytest.raises(sar_export.ExportError) as info:
        await sar_export.submit_export("dpo", _request(), None)
    assert info.value.status == 503


async def test_read_status_of_unknown_export_is_404():
    nc = FakeClient(FakeWebDAV())
    with pytest.raises(sar_export.ExportError, match="no export named") as info:
        await sar_export.read_status(nc, "/Team", "SAR-9")
    assert info.value.status == 404


@pytest.mark.parametrize("name", ["", "../x", "a/b", "-x", "x" * 101, "a..b"])
def test_archive_name_is_one_plain_segment(name):
    with pytest.raises(sar_export.ExportError):
        sar_export.archive_paths("/Team", name)


@pytest.mark.parametrize("folder", ["../x", "/Team/../Other", "Team/./x"])
def test_output_folder_refuses_dot_segments(folder):
    with pytest.raises(sar_export.ExportError, match="segments"):
        sar_export.archive_paths(folder, "SAR-1")


def test_archive_paths_normalise_folder():
    assert sar_export.archive_paths("/", "SAR") == ("/SAR.zip", "/SAR.status.json")
    assert sar_export.archive_paths(" Team/SAR/ ", "SAR 1") == (
        "/Team/SAR/SAR 1.zip",
        "/Team/SAR/SAR 1.status.json",
    )


def test_stitch_drops_overlap_and_marks_gaps():
    chunks = [(10, "klmno"), (0, "abcdefghij"), (8, "ijklm"), (20, "uvw")]
    assert sar_export.stitch(chunks) == "abcdefghijklmno\nuvw"


def test_item_page_range_validation():
    with pytest.raises(ValueError):
        SarItem(doc_type="file", doc_id="1", reason="r", page_start=5, page_end=2)
    assert SarItem(doc_type="file", doc_id=7, reason="r").doc_id == "7"


class FakePoint:
    def __init__(self, payload):
        self.payload = payload


class FakeQdrant:
    """Two scroll pages; chunk 1 is duplicated (indexed under two owners)."""

    def __init__(self, payloads):
        self.pages = [payloads[:2], payloads[2:]]
        self.filters = []

    async def scroll(self, *, scroll_filter, offset, **_):
        self.filters.append(scroll_filter)
        page = 0 if offset is None else 1
        return [FakePoint(p) for p in self.pages[page]], (1 if page == 0 else None)


def _chunk(i, start, text, page):
    return {
        "title": "Scan",
        "excerpt": text,
        "chunk_index": i,
        "chunk_start_offset": start,
        "page_number": page,
    }


@pytest.fixture
def qdrant(monkeypatch):
    fake = FakeQdrant(
        [
            _chunk(0, 0, "page one text", 1),
            _chunk(1, 14, "page two text", 2),
            _chunk(1, 14, "page two text", 2),
            _chunk(2, 28, "page three", 3),
        ]
    )

    async def get_qdrant_client():
        return fake

    async def list_accessible_owners(sharing, user_id):
        return [user_id, "owner"]

    monkeypatch.setattr(sar_export, "get_qdrant_client", get_qdrant_client)
    monkeypatch.setattr(sar_export, "list_accessible_owners", list_accessible_owners)
    return fake


class FakeAccessWebDAV:
    def __init__(self, accessible: bool) -> None:
        self.accessible = accessible

    async def file_accessible_by_id(self, file_id):
        return self.accessible


class FakeReader:
    def __init__(self, accessible=True):
        self.webdav = FakeAccessWebDAV(accessible)
        self.sharing = object()


async def test_document_text_reassembles_all_pages(qdrant):
    title, text = await sar_export.document_text(
        FakeReader(), "dpo", SarItem(doc_type="file", doc_id="5", reason="r")
    )
    assert title == "Scan"
    assert text == "page one text\npage two text\npage three"


async def test_document_text_honours_page_range(qdrant):
    _, text = await sar_export.document_text(
        FakeReader(),
        "dpo",
        SarItem(doc_type="file", doc_id="5", reason="r", page_start=2, page_end=2),
    )
    assert text == "page two text"
    item = SarItem(doc_type="file", doc_id="5", reason="r", page_start=9)
    with pytest.raises(sar_export._ItemError, match="requested pages"):
        await sar_export.document_text(FakeReader(), "dpo", item)


async def test_document_text_refuses_inaccessible_file(qdrant):
    item = SarItem(doc_type="file", doc_id="5", reason="r")
    with pytest.raises(sar_export._ItemError, match="not accessible"):
        await sar_export.document_text(FakeReader(accessible=False), "dpo", item)
    assert qdrant.filters == []  # refused before touching the index
