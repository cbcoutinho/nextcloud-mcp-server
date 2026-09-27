"""Unit tests for SAR cases (ADR-040): the case file, its state machine,
ETag-guarded changes, and exporting a case. Synthetic names only."""

import io
import json
import posixpath
import re
import unicodedata
import zipfile

import anyio
import httpx
import pymupdf
import pytest

from nextcloud_mcp_server import sar_case, sar_export
from nextcloud_mcp_server.models.sar import (
    SarCaseItem,
    SarCaseItemsChange,
    SarCaseUpdate,
    SarItemRef,
    SarQueryIn,
    SarSearchFilters,
)
from nextcloud_mcp_server.providers.ner import NerError

pytestmark = pytest.mark.unit

NAMES = ("Jane Doe", "Karen Smith")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://nc/remote.php/dav/files/dpo/x")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeWebDAV:
    """In-memory WebDAV with file ids, ETags and SEARCH by fileid/name."""

    def __init__(self) -> None:
        self.dirs = {"", "/Team"}
        self.files: dict[str, tuple[bytes, int, int]] = {}  # path -> (body, id, rev)
        self._next_id = 100
        # Hooks simulating a writer in another process (bypassing our lock).
        self.before_write = None
        self.after_write = None

    async def create_directory(self, path):
        path = path.rstrip("/")
        if path in self.dirs:
            return {"status_code": 405}
        if posixpath.dirname(path) not in self.dirs:
            raise _http_error(409)
        self.dirs.add(path)
        return {"status_code": 201}

    async def write_file(self, path, content, content_type=None, if_match=None):
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            await hook()
        if posixpath.dirname(path) not in self.dirs:
            raise _http_error(409)
        current = self.files.get(path)
        if if_match is None and current is not None:
            return {"status_code": 412}
        if if_match == "*" and current is None:
            return {"status_code": 412}
        if if_match not in (None, "*") and (
            current is None or f"rev{current[2]}" != if_match
        ):
            return {"status_code": 412}
        if current is None:
            self._next_id += 1
            file_id, rev = self._next_id, 0
        else:
            _, file_id, rev = current
        self.files[path] = (content, file_id, rev + 1)
        if self.after_write is not None:
            hook, self.after_write = self.after_write, None
            await hook()
        return {"status_code": 201, "etag": f"rev{rev + 1}"}

    async def read_file(self, path):
        if path not in self.files:
            raise _http_error(404)
        body, _, rev = self.files[path]
        return body, "application/json", f"rev{rev}"

    async def get_fileid(self, path):
        return str(self.files[path][1])

    async def search_files(
        self, scope="", where_conditions="", properties=None, limit=None
    ):
        if m := re.search(r"<d:literal>(\d+)</d:literal>", where_conditions):
            wanted = int(m.group(1))
            hits = [p for p, (_, fid, _) in self.files.items() if fid == wanted]
        else:
            hits = [p for p in self.files if p.endswith("/sar-case.json")]
        return [
            {
                "path": p.lstrip("/"),
                "name": posixpath.basename(p),
                "file_id": self.files[p][1],
            }
            for p in hits
        ]


class FakeClient:
    def __init__(self, webdav: FakeWebDAV, username: str = "dpo") -> None:
        self.username = username
        self.webdav = webdav
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeNer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def detect(self, texts, labels=("person",)):
        if self.fail:
            raise NerError("down")
        return [{("person", n) for n in NAMES if n in t} for t in texts]


@pytest.fixture
def indexed(monkeypatch):
    async def document_text(nc, user_id, item):
        return "Letter re Karen Smith", "Jane Doe met Karen Smith."

    monkeypatch.setattr(sar_export, "document_text", document_text)


@pytest.fixture
def webdav():
    return FakeWebDAV()


@pytest.fixture
def nc(webdav):
    return FakeClient(webdav)


async def _create(nc):
    return await sar_case.create_case(
        nc, folder="/Team", name="SAR-1", subject=["Jane Doe"], description="ref 7"
    )


def _add(*doc_ids: str, reason: str = "mentions the subject") -> SarCaseItemsChange:
    return SarCaseItemsChange(
        add=[SarCaseItem(doc_type="note", doc_id=d, reason=reason) for d in doc_ids]
    )


# --- Create, get, list ---------------------------------------------------------


async def test_create_writes_case_file_in_its_own_folder(nc, webdav):
    created = await _create(nc)
    assert created.path == "/Team/SAR-1/sar-case.json"
    assert created.case.state == "open"
    assert created.case.created_by == "dpo"
    stored = json.loads(webdav.files["/Team/SAR-1/sar-case.json"][0])
    assert stored["subject"] == ["Jane Doe"]
    got = await sar_case.get_case(nc, created.case_id)
    assert got.case.description == "ref 7"


async def test_create_refuses_existing_case_and_missing_folder(nc):
    await _create(nc)
    with pytest.raises(sar_export.ExportError) as info:
        await _create(nc)
    assert info.value.status == 409
    with pytest.raises(sar_export.ExportError) as info:
        await sar_case.create_case(
            nc, folder="/Nope", name="SAR-2", subject=["Jane Doe"]
        )
    assert info.value.status == 403


async def test_create_reports_a_nextcloud_failure_as_retryable(nc, monkeypatch):
    """A 5xx is Nextcloud failing, not the folder being unwritable."""

    async def unavailable(path):
        raise _http_error(503)

    monkeypatch.setattr(nc.webdav, "create_directory", unavailable)
    with pytest.raises(sar_export.ExportError, match="try again") as info:
        await _create(nc)
    assert info.value.status == 503


async def test_create_validates_name_and_folder(nc):
    for folder, name in (("/Team", "../x"), ("/Team/../Other", "SAR-3")):
        with pytest.raises(sar_export.ExportError) as info:
            await sar_case.create_case(
                nc, folder=folder, name=name, subject=["Jane Doe"]
            )
        assert info.value.status == 400


async def test_list_returns_cases_newest_first(nc, webdav):
    first = await _create(nc)
    await sar_case.create_case(nc, folder="/Team", name="SAR-2", subject=["J"])
    await sar_case.change_items(nc, first.case_id, _add("1"))  # now newest
    webdav.dirs.add("/Team/junk")
    webdav.files["/Team/junk/sar-case.json"] = (b"not json", 999, 1)

    listed = await sar_case.list_cases(nc)

    assert [c.name for c in listed.cases] == ["SAR-1", "SAR-2"]
    assert listed.cases[0].items == 1


async def test_unknown_or_non_case_id_is_404(nc, webdav):
    await _create(nc)
    webdav.files["/Team/other.txt"] = (b"x", 555, 1)
    for case_id in (12345, 555):
        with pytest.raises(sar_export.ExportError) as info:
            await sar_case.get_case(nc, case_id)
        assert info.value.status == 404


async def test_get_pages_items(nc):
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1", "2", "3"))
    page = await sar_case.get_case(nc, created.case_id, offset=1, limit=1)
    assert page.items_total == 3
    assert [i.doc_id for i in page.case.items] == ["2"]


# --- Items and queries -----------------------------------------------------------


async def test_items_add_update_remove_and_log_queries(nc):
    created = await _create(nc)
    first = SarCaseItemsChange(
        add=[
            SarCaseItem(
                doc_type="note", doc_id=1, reason="", title="Letter", found_by="q1"
            ),
            SarCaseItem(doc_type="file", doc_id="9", reason="r9"),
        ],
        queries=[SarQueryIn(text="q1", hits=3), SarQueryIn(text="nothing", hits=0)],
    )
    await sar_case.change_items(nc, created.case_id, first)
    other = FakeClient(nc.webdav, username="colleague")
    # Re-adding updates the reason but keeps who added it, the title and
    # the query that found it.
    second = SarCaseItemsChange(
        add=[SarCaseItem(doc_type="note", doc_id="1", reason="now with a reason")],
        remove=[SarItemRef(doc_type="file", doc_id=9)],
    )
    result = await sar_case.change_items(other, created.case_id, second)

    (item,) = result.case.items
    assert item.reason == "now with a reason"
    assert (item.added_by, item.title, item.found_by) == ("dpo", "Letter", "q1")
    assert [(q.text, q.hits, q.run_by) for q in result.case.queries] == [
        ("q1", 3, "dpo"),
        ("nothing", 0, "dpo"),
    ]


async def test_items_cap(nc, monkeypatch):
    monkeypatch.setattr(sar_case, "MAX_CASE_ITEMS", 2)
    created = await _create(nc)
    with pytest.raises(sar_export.ExportError, match="at most 2") as info:
        await sar_case.change_items(nc, created.case_id, _add("1", "2", "3"))
    assert info.value.status == 400


async def test_a_search_already_logged_is_not_logged_again(nc):
    """Same text and filters: one entry. Different filters: a new entry."""
    created = await _create(nc)
    folder = SarSearchFilters(path_prefixes=["/HR"])
    for _ in range(3):
        result = await sar_case.change_items(
            nc,
            created.case_id,
            SarCaseItemsChange(
                queries=[
                    SarQueryIn(text="q", hits=1, filters=folder),
                    SarQueryIn(text="q", hits=1),
                ]
            ),
        )
    assert [(q.text, bool(q.filters)) for q in result.case.queries] == [
        ("q", True),
        ("q", False),
    ]


async def test_queries_cap(nc, monkeypatch):
    monkeypatch.setattr(sar_case, "MAX_CASE_QUERIES", 2)
    created = await _create(nc)
    change = SarCaseItemsChange(queries=[SarQueryIn(text=t) for t in "abc"])
    with pytest.raises(sar_export.ExportError, match="at most 2 searches") as info:
        await sar_case.change_items(nc, created.case_id, change)
    assert info.value.status == 400


def _other_process_adds(webdav, path, doc_id, base=None):
    """A write from another process, straight to storage: adds ``doc_id`` to
    ``base`` (default: the current file) without going through our lock."""

    async def write():
        body, file_id, rev = webdav.files[path]
        data = json.loads(base if base is not None else body)
        data["items"].append({"doc_type": "note", "doc_id": doc_id, "reason": "r"})
        data["recent_writes"] = [*data.get("recent_writes", []), f"other-{doc_id}"]
        webdav.files[path] = (json.dumps(data).encode(), file_id, rev + 1)

    return write


async def test_write_between_read_and_write_is_retried(nc, webdav):
    created = await _create(nc)
    webdav.before_write = _other_process_adds(webdav, created.path, "other")
    result = await sar_case.change_items(nc, created.case_id, _add("mine"))
    assert {i.doc_id for i in result.case.items} == {"other", "mine"}


async def test_lost_update_from_stale_copy_is_detected_and_reapplied(nc, webdav):
    """The stress-test bug: our write is acknowledged, then overwritten by a
    writer that read the case before it. The lineage check must notice."""
    created = await _create(nc)
    stale = webdav.files[created.path][0]
    webdav.after_write = _other_process_adds(webdav, created.path, "other", base=stale)

    result = await sar_case.change_items(nc, created.case_id, _add("mine"))

    stored = json.loads(webdav.files[created.path][0])
    assert {i["doc_id"] for i in stored["items"]} == {"other", "mine"}
    assert {i.doc_id for i in result.case.items} == {"other", "mine"}


async def test_concurrent_edits_in_this_process_all_land(nc):
    created = await _create(nc)

    async def add(doc_id: str) -> None:
        await sar_case.change_items(nc, created.case_id, _add(doc_id))

    async with anyio.create_task_group() as tg:
        for n in range(20):
            tg.start_soon(add, str(n))
    got = await sar_case.get_case(nc, created.case_id)
    assert {i.doc_id for i in got.case.items} == {str(n) for n in range(20)}


# --- State machine ------------------------------------------------------------------


async def test_close_is_final_and_read_only(nc):
    created = await _create(nc)
    closed = await sar_case.update_case(
        nc, created.case_id, SarCaseUpdate(state="closed")
    )
    assert closed.case.state == "closed"
    assert closed.case.closed_by == "dpo"
    for change in (
        lambda: sar_case.update_case(nc, created.case_id, SarCaseUpdate(state="open")),
        lambda: sar_case.update_case(
            nc, created.case_id, SarCaseUpdate(description="x")
        ),
        lambda: sar_case.change_items(nc, created.case_id, _add("1")),
    ):
        with pytest.raises(sar_export.ExportError) as info:
            await change()
        assert info.value.status == 409


async def test_update_subject_and_description_when_open(nc):
    created = await _create(nc)
    updated = await sar_case.update_case(
        nc,
        created.case_id,
        SarCaseUpdate(subject=["Jane Doe", "Ms Doe"], description="ref 8"),
    )
    assert updated.case.subject == ["Jane Doe", "Ms Doe"]
    assert updated.case.description == "ref 8"


# --- Export ----------------------------------------------------------------------------


async def _export(nc, case_id, tg, ner=None, output_folder=None):
    background = FakeClient(nc.webdav, username=nc.username)
    result = await sar_case.export_case(
        nc, background, ner or FakeNer(), tg, case_id, output_folder
    )
    return result, background


async def test_export_locks_case_then_marks_it_ready_for_audit(nc, webdav, indexed):
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1"))

    async with anyio.create_task_group() as tg:
        started, background = await _export(nc, created.case_id, tg)
        assert started.case.state == "exporting"
        with pytest.raises(sar_export.ExportError) as info:
            await sar_case.change_items(nc, created.case_id, _add("2"))
        assert info.value.status == 409

    assert background.closed
    done = await sar_case.get_case(nc, created.case_id)
    assert done.case.state == "ready_for_audit"
    (export,) = done.case.exports
    assert (export.version, export.state, export.total) == (1, "done", 1)
    assert export.archive_path == "/Team/SAR-1/exports/SAR-1-v1.zip"
    assert done.latest_export is not None and done.latest_export.state == "done"
    archive = zipfile.ZipFile(io.BytesIO(webdav.files[export.archive_path][0]))
    assert "documents/001-Letter-re-[PERSON_1].pdf" in archive.namelist()

    # Reopen, change, export again: a second version next to the first.
    await sar_case.update_case(nc, created.case_id, SarCaseUpdate(state="open"))
    async with anyio.create_task_group() as tg:
        await _export(nc, created.case_id, tg)
    again = await sar_case.get_case(nc, created.case_id)
    assert [e.version for e in again.case.exports] == [1, 2]
    assert "/Team/SAR-1/exports/SAR-1-v2.zip" in webdav.files


async def test_searches_pdf_lists_each_querys_filters_redacted(nc, webdav, indexed):
    created = await _create(nc)
    filters = SarSearchFilters(
        path_prefixes=["/HR/Karen Smith"], doc_types=["file"], modified_after="2023"
    )
    request = _add("1")
    request.queries = [SarQueryIn(text="grievance", hits=1, filters=filters)]
    await sar_case.change_items(nc, created.case_id, request)

    async with anyio.create_task_group() as tg:
        await _export(nc, created.case_id, tg)

    (export,) = (await sar_case.get_case(nc, created.case_id)).case.exports
    archive = zipfile.ZipFile(io.BytesIO(webdav.files[export.archive_path][0]))
    with pymupdf.open(stream=archive.read("searches.pdf"), filetype="pdf") as pdf:
        # NFKC folds the "fi" ligature PyMuPDF extracts back to two letters.
        text = unicodedata.normalize("NFKC", "".join(p.get_text() for p in pdf))
    searches = " ".join(text.split())
    assert "grievance (folders: /HR/[PERSON_1]; types: file; modified 2023" in searches
    assert "Karen" not in searches


async def test_failed_export_reopens_case(nc, indexed):
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1"))
    async with anyio.create_task_group() as tg:
        await _export(nc, created.case_id, tg, ner=FakeNer(fail=True))
    after = await sar_case.get_case(nc, created.case_id)
    assert after.case.state == "open"
    assert after.case.exports[0].state == "failed"


async def test_export_includes_an_item_added_just_before_the_lock(nc, indexed):
    """The export works from the case as locked, not from the earlier read."""
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1"))
    # Another process adds a document between the export's read and its lock.
    nc.webdav.before_write = _other_process_adds(nc.webdav, created.path, "late")

    async with anyio.create_task_group() as tg:
        started, _ = await _export(nc, created.case_id, tg)

    (export,) = started.case.exports
    assert export.total == 2
    done = await sar_case.get_case(nc, created.case_id)
    assert done.latest_export is not None and done.latest_export.total == 2


async def test_export_needs_items_with_reasons(nc):
    created = await _create(nc)
    async with anyio.create_task_group() as tg:
        with pytest.raises(sar_export.ExportError, match="at least one") as info:
            await _export(nc, created.case_id, tg)
        assert info.value.status == 400
        await sar_case.change_items(nc, created.case_id, _add("1", reason=" "))
        with pytest.raises(sar_export.ExportError, match="reason"):
            await _export(nc, created.case_id, tg)
    assert (await sar_case.get_case(nc, created.case_id)).case.state == "open"


async def test_export_that_cannot_start_unlocks_case_and_closes_client(nc, indexed):
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1"))
    background = FakeClient(nc.webdav)
    with pytest.raises(sar_export.ExportError) as info:
        await sar_case.export_case(nc, background, FakeNer(), None, created.case_id)
    assert info.value.status == 503
    assert background.closed
    after = await sar_case.get_case(nc, created.case_id)
    assert after.case.state == "open"
    assert after.case.exports[0].state == "failed"


async def test_export_to_another_folder(nc, webdav, indexed):
    created = await _create(nc)
    await sar_case.change_items(nc, created.case_id, _add("1"))
    webdav.dirs.add("/Team/Out")
    async with anyio.create_task_group() as tg:
        await _export(nc, created.case_id, tg, output_folder="/Team/Out")
    assert "/Team/Out/SAR-1-v1.zip" in webdav.files
