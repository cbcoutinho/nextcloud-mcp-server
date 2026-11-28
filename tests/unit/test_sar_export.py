"""Unit tests for SAR export archives (ADR-040). Synthetic names only."""

import io
import json
import re
import zipfile

import anyio
import httpx2
import pymupdf
import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

from nextcloud_mcp_server import sar_export
from nextcloud_mcp_server.models.sar import (
    SarExportStatus,
    SarItem,
)
from nextcloud_mcp_server.providers.ner import NerError
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit

NAMES = ("Jane Doe", "Karen Smith", "Tom Brown")
ADDRESSES = ("14 Mill Lane", "3 Oak Road")


class FakeNer:
    """Detects the synthetic NAMES, like tests/fixtures/ner_stub.py."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def detect(
        self, texts: list[str], labels: tuple[str, ...] = ("person",)
    ) -> list[set[tuple[str, str]]]:
        if self.fail:
            raise NerError("down")
        return [
            {("person", n) for n in NAMES if n in t}
            | {("address", a) for a in ADDRESSES if a in t and "address" in labels}
            for t in texts
        ]


def _http_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("PUT", "http://nc/remote.php/dav/files/dpo/x")
    return httpx2.HTTPStatusError(
        str(status), request=request, response=httpx2.Response(status, request=request)
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
        "Jane Doe met Karen Smith (karen@example.org). Later Smith left. "
        "Jane Doe lives at 3 Oak Road, XK1 1AA.",
    ),
    "2": (
        "Minutes",
        "Tom Brown chaired. Jane Doe, 07700 900111, attended. "
        "Tom Brown, 14 Mill Lane, XA9 8QT.",
    ),
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
        keep=["Jane Doe", "07700 900111", "3 Oak Road, XK1 1AA"],
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
    for leak in (
        "Karen",
        "Smith",
        "Tom Brown",
        "karen@example.org",
        "Mill Lane",
        "XA9 8QT",
    ):
        assert leak not in everything, leak
    assert "Jane Doe" in everything
    assert "07700 900111" in everything  # the subject's own number is kept
    assert "3 Oak Road, XK1 1AA" in everything  # and their own address
    assert "[EMAIL_1]" in everything
    assert "[ADDRESS_1], [ADDRESS_2]" in everything  # street, then postcode
    # Archive-wide numbering: Karen Smith is PERSON_1 in the title, reason
    # and the query log alike.
    assert "[PERSON_1]'s complaint" in _pdf_text(zf.read("index.pdf"))
    assert "[PERSON_1] complaint" in _pdf_text(zf.read("searches.pdf"))
    index = _pdf_text(zf.read("index.pdf"))
    assert "Not exported: not in the search index" in index


def test_blank_reason_is_left_out(monkeypatch):
    monkeypatch.setattr(sar_export, "_pdf", str.encode)
    assert b"Reason for inclusion" not in sar_export.render_document("L", " ", "b")
    assert b"Reason for inclusion" in sar_export.render_document("L", "why", "b")
    row = {"n": 1, "title": "Letter", "reason": "", "pages": "", "counts": {}}
    failed = {"n": 2, "error": "not in the search index", "pages": "", "counts": {}}
    index = sar_export.render_index("SAR-1", [row, failed])
    assert b"<td>Letter</td>" in index
    assert b"<i></i>" not in index
    assert b"Not exported:</b> not in the search index" in index
    row["reason"] = "why"
    assert b"<i>why</i>" in sar_export.render_index("SAR-1", [row])


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


async def _start(nc, tg, folder="/Team/SAR", on_finish=None):
    return await sar_export.start_export(
        nc=nc,
        ner=FakeNer(),
        task_group=tg,
        output_folder=folder,
        name="SAR-1",
        keep=["Jane Doe"],
        items=[SarItem(doc_type="file", doc_id="1", reason="r")],
        queries=[],
        on_finish=on_finish,
    )


async def test_start_runs_job_in_background_and_calls_on_finish(indexed):
    nc = FakeClient(FakeWebDAV())
    finished = []

    async def on_finish(status):
        assert not nc.closed  # runs before the job closes its client
        finished.append(status.state)

    async with anyio.create_task_group() as tg:
        status = await _start(nc, tg, on_finish=on_finish)
        assert status.state == "running"
    assert "/Team/SAR/SAR-1.zip" in nc.webdav.files
    assert nc.closed
    assert finished == ["done"]
    assert (await sar_export.read_status(nc, status.status_path)).state == "done"


async def test_start_refuses_existing_name(indexed):
    nc = FakeClient(FakeWebDAV(existing={"/Team/SAR/SAR-1.status.json"}))
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="already exists") as info:
            await _start(nc, tg, folder="Team/SAR/")
    assert info.value.status == 409
    assert not nc.closed  # the caller still owns it


async def test_start_refuses_unwritable_folder(indexed):
    webdav = FakeWebDAV()
    webdav.fail_writes_with = 403
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="writable") as info:
            await _start(FakeClient(webdav), tg)
    assert info.value.status == 403


async def test_start_without_task_group_is_unavailable():
    with pytest.raises(sar_export.ExportError) as info:
        await _start(FakeClient(FakeWebDAV()), None)
    assert info.value.status == 503


async def test_background_client_needs_provisioning(monkeypatch):
    async def resolve(user_id):
        raise NotProvisionedError("no app password")

    monkeypatch.setattr(sar_export, "resolve_background_client", resolve)
    with pytest.raises(sar_export.ExportError, match="background access") as info:
        await sar_export.background_client("dpo")
    assert info.value.status == 403


async def test_read_status_of_unknown_export_is_404():
    nc = FakeClient(FakeWebDAV())
    with pytest.raises(sar_export.ExportError, match="no export status") as info:
        await sar_export.read_status(nc, "/Team/SAR-9.status.json")
    assert info.value.status == 404


_NAME_START = "ABCXYZabcxyz0189"
_NAME_REST = _NAME_START + "._ -"
_plain_names = (
    st.tuples(st.sampled_from(_NAME_START), st.text(_NAME_REST, max_size=99))
    .map("".join)
    .filter(lambda n: ".." not in n)
)


@st.composite
def archive_names(draw) -> tuple[str, bool]:
    """An archive name and whether it is one plain segment."""
    name = draw(_plain_names)
    kind = draw(
        st.sampled_from(["plain", "empty", "dotdot", "start", "long", "foreign"])
    )
    event(kind)
    at = draw(st.integers(1, len(name)))
    if kind == "empty":
        name = ""
    elif kind == "dotdot":
        name = name[:at] + ".." + name[at:]
    elif kind == "start":  # must start with a letter or digit
        name = draw(st.sampled_from("._ -/")) + name
    elif kind == "long":
        name = name + "x" * (101 - len(name))
    elif kind == "foreign":  # "/", "\\", ":", a newline, ...
        foreign = draw(
            st.characters(codec="utf-8").filter(
                lambda c: not re.fullmatch(r"[A-Za-z0-9._ -]", c)
            )
        )
        name = name[:at] + foreign + name[at:]
    return name, kind == "plain"


@given(archive_names())
@example(("../x", False))
@example(("a/b", False))
@example(("-x", False))
@example(("a..b", False))
def test_archive_name_is_one_plain_segment(case):
    name, plain = case
    if plain:
        assert sar_export.archive_paths("/Team", name) == (
            f"/Team/{name}.zip",
            f"/Team/{name}.status.json",
        )
    else:
        with pytest.raises(sar_export.ExportError):
            sar_export.archive_paths("/Team", name)


_folder_segment = st.text("ab.-_", min_size=1, max_size=4).filter(
    lambda s: s not in (".", "..")
)


@st.composite
def output_folders(draw) -> tuple[str, str | None]:
    """A folder as typed (extra slashes, outer spaces), and its absolute form
    (None: it has a ``.`` or ``..`` segment)."""
    segments = draw(st.lists(_folder_segment, max_size=4))
    expected = "/" + "/".join(segments) if segments else ""
    if draw(st.booleans()):
        event("dot segment")
        segments.insert(
            draw(st.integers(0, len(segments))), draw(st.sampled_from([".", ".."]))
        )
        expected = None
    slashes = st.text("/", min_size=1, max_size=2)
    typed = "".join(s + draw(slashes) for s in segments)
    typed = draw(st.text("/", max_size=2)) + typed[: -1 if typed else None]
    return draw(st.text(" ", max_size=2)) + typed + draw(
        st.text(" ", max_size=2)
    ), expected


@given(output_folders())
@example(("/Team/../Other", None))
@example(("Team/./x", None))
def test_output_folder_is_normalised_and_refuses_dot_segments(case):
    folder, expected = case
    if expected is None:
        with pytest.raises(sar_export.ExportError, match="segments"):
            sar_export.archive_paths(folder, "SAR-1")
    else:
        assert sar_export.archive_paths(folder, "SAR-1")[0] == f"{expected}/SAR-1.zip"


def test_archive_paths_normalise_folder():
    assert sar_export.archive_paths("/", "SAR") == ("/SAR.zip", "/SAR.status.json")
    assert sar_export.archive_paths(" Team/SAR/ ", "SAR 1") == (
        "/Team/SAR/SAR 1.zip",
        "/Team/SAR/SAR 1.status.json",
    )


def test_stitch_drops_overlap_and_marks_gaps():
    chunks = [(10, "klmno"), (0, "abcdefghij"), (8, "ijklm"), (20, "uvw")]
    assert sar_export.stitch(chunks) == "abcdefghijklmno\nuvw"


_MAX_RUNS = 4
_MAX_CUTS = 4


@st.composite
def chunked_documents(draw):
    """``(chunks in any order, the text stitch must rebuild)``.

    The document is runs of text separated by gaps no chunk covers. Each run is
    cut into chunks that may overlap the previous one (the chunker's repeated
    tail) without starting before it. Rebuilt, the runs join with line breaks.

    Every run slot draws the same values whether it is used or not, and the
    order is a permutation of every chunk slot, so the number of draws is
    fixed: Hypothesis's mutations never outgrow the example and discard it.
    """
    n_runs = draw(st.integers(1, _MAX_RUNS))
    slots = []  # (run, cuts, overlaps, gap) per run slot
    for _ in range(_MAX_RUNS):
        run = draw(st.text(min_size=1, max_size=20))
        n_cuts = draw(st.integers(0, _MAX_CUTS))
        cut_at = [draw(st.integers(0, len(run))) for _ in range(_MAX_CUTS)]
        overlaps = [draw(st.integers(0, 20)) for _ in range(_MAX_CUTS + 1)]
        slots.append((run, set(cut_at[:n_cuts]), overlaps, draw(st.integers(1, 5))))
    order = draw(st.permutations(range(_MAX_RUNS * (_MAX_CUTS + 1))))

    runs = [run for run, *_ in slots[:n_runs]]
    chunks: list[tuple[int, tuple[int, str]]] = []  # (slot in order, chunk)
    offset = 0
    for r, (run, cut_at, overlaps, gap) in enumerate(slots[:n_runs]):
        bounds = [0, *sorted(cut_at - {0, len(run)}), len(run)]
        prev_start = 0
        for i in range(len(bounds) - 1):
            # Any overlap from none back to the previous chunk's start.
            start = bounds[i] - overlaps[i] % (bounds[i] - prev_start + 1)
            chunk = (offset + start, run[start : bounds[i + 1]])
            chunks.append((order[r * (_MAX_CUTS + 1) + i], chunk))
            prev_start = start
        offset += len(run) + gap
    chunks.sort()
    event(f"overlap: {sum(len(t) for _, (_, t) in chunks) > sum(map(len, runs))}")
    return [chunk for _, chunk in chunks], "\n".join(runs)


@given(chunked_documents())
def test_stitch_rebuilds_the_chunked_text(case):
    chunks, text = case
    assert sar_export.stitch(chunks) == text


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
