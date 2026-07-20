"""Unit tests for the isolated PDF parse (OOM hotfix).

The parse runs in a worker subprocess (``anyio.to_process``) with a memory
rlimit and a wall-clock timeout so a pathological PDF fails *that document*
instead of OOM-killing the pod. These tests pin:
  * the failure classification (oom / timeout / error) of the async wrapper;
  * ``_apply_mem_limit`` rlimit computation (mocked, never applied in-process);
  * the PyMuPDF processor wiring: settings forwarded, success path, and a
    graceful ``success=False`` result on a permanent parse failure.

The real subprocess + rlimit enforcement is exercised by the local end-to-end
check on the sample PDFs, not here (unit tests must not spawn the heavy worker
or depend on the sample files).
"""

import sys

import anyio
import anyio.to_process
import pymupdf
import pytest
from anyio import BrokenWorkerProcess

from nextcloud_mcp_server.document_processors import _isolation
from nextcloud_mcp_server.document_processors._isolation import (
    PdfParseFailed,
    run_isolated_pdf_parse,
)

# ``resource`` is a Unix-only stdlib module (absent on Windows, #877). Guard the
# import so this test module stays importable on Windows; the rlimit-computation
# tests below are skipped there via the ``requires_resource`` marker.
try:
    import resource
except ImportError:  # pragma: no cover - only reached on Windows
    resource = None  # type: ignore[assignment]

pytestmark = pytest.mark.unit

requires_resource = pytest.mark.skipif(
    resource is None, reason="resource module is Unix-only (absent on Windows)"
)


def _tiny_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Hello world")
    data: bytes = doc.tobytes()
    doc.close()
    return data


async def _run(monkeypatch, fake_run_sync) -> list:
    monkeypatch.setattr(anyio.to_process, "run_sync", fake_run_sync)
    return await run_isolated_pdf_parse(
        b"%PDF-1.7",
        write_images=False,
        image_path=None,
        graphics_limit=5000,
        timeout_seconds=5,
        mem_limit_mb=1536,
        markdown_max_pages=150,
    )


# --- failure classification of the async wrapper ----------------------------


async def test_success_returns_worker_value(monkeypatch):
    page_chunks = [{"text": "ok", "metadata": {"page": 1}}]

    async def fake(*args, **kwargs):
        return page_chunks

    assert await _run(monkeypatch, fake) == page_chunks


async def test_memory_error_classified_as_oom(monkeypatch):
    async def fake(*args, **kwargs):
        raise MemoryError("rlimit hit")

    with pytest.raises(PdfParseFailed) as exc:
        await _run(monkeypatch, fake)
    assert exc.value.reason == "oom"


async def test_broken_worker_classified_as_oom(monkeypatch):
    async def fake(*args, **kwargs):
        raise BrokenWorkerProcess("worker died")

    with pytest.raises(PdfParseFailed) as exc:
        await _run(monkeypatch, fake)
    assert exc.value.reason == "oom"


async def test_other_exception_classified_as_error(monkeypatch):
    async def fake(*args, **kwargs):
        raise ValueError("not a pdf")

    with pytest.raises(PdfParseFailed) as exc:
        await _run(monkeypatch, fake)
    assert exc.value.reason == "error"


async def test_timeout_kills_and_classifies_as_timeout(monkeypatch):
    async def fake(*args, **kwargs):
        # Simulate a hung worker; the move_on_after timeout must win.
        await anyio.sleep(30)

    monkeypatch.setattr(anyio.to_process, "run_sync", fake)
    with pytest.raises(PdfParseFailed) as exc:
        await run_isolated_pdf_parse(
            b"%PDF-1.7",
            write_images=False,
            image_path=None,
            graphics_limit=5000,
            timeout_seconds=0.2,
            mem_limit_mb=1536,
            markdown_max_pages=150,
        )
    assert exc.value.reason == "timeout"


# --- _apply_mem_limit computation (mocked; never applied to the test proc) ---


@requires_resource
def test_apply_mem_limit_caps_soft_below_finite_hard(monkeypatch):
    captured = {}
    monkeypatch.setattr(_isolation, "_MEM_LIMIT_APPLIED", False)
    monkeypatch.setattr(
        _isolation.resource,
        "getrlimit",
        lambda _w: (resource.RLIM_INFINITY, 4 * 1024**3),
    )
    monkeypatch.setattr(
        _isolation.resource, "setrlimit", lambda _w, pair: captured.update(pair=pair)
    )
    # target = 1536 MiB < hard (4 GiB) -> soft becomes the target, hard untouched
    _isolation._apply_mem_limit(1536)
    soft, hard = captured["pair"]
    assert soft == 1536 * 1024 * 1024
    assert hard == 4 * 1024**3


@requires_resource
def test_apply_mem_limit_uses_target_when_hard_unlimited(monkeypatch):
    captured = {}
    monkeypatch.setattr(_isolation, "_MEM_LIMIT_APPLIED", False)
    monkeypatch.setattr(
        _isolation.resource,
        "getrlimit",
        lambda _w: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        _isolation.resource, "setrlimit", lambda _w, pair: captured.update(pair=pair)
    )
    # hard is unbounded -> soft is exactly the target, hard stays RLIM_INFINITY
    _isolation._apply_mem_limit(1536)
    soft, hard = captured["pair"]
    assert soft == 1536 * 1024 * 1024
    assert hard == resource.RLIM_INFINITY


@requires_resource
def test_apply_mem_limit_is_applied_once(monkeypatch):
    calls = []
    monkeypatch.setattr(_isolation, "_MEM_LIMIT_APPLIED", False)
    monkeypatch.setattr(
        _isolation.resource,
        "getrlimit",
        lambda _w: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(_isolation.resource, "setrlimit", lambda *a: calls.append(a))
    _isolation._apply_mem_limit(1536)
    _isolation._apply_mem_limit(1536)
    assert len(calls) == 1  # second call is a no-op


# --- Windows / no-``resource`` platform compatibility (#877) -----------------


def test_apply_mem_limit_noop_when_resource_unavailable(monkeypatch):
    """On a platform without ``resource`` (e.g. Windows) the cap is skipped.

    Regression for #877: ``resource`` is Unix-only, so ``_apply_mem_limit`` must
    degrade to a no-op (rather than crash) when the module is unavailable.
    """
    monkeypatch.setattr(_isolation, "_MEM_LIMIT_APPLIED", False)
    monkeypatch.setattr(_isolation, "resource", None)
    _isolation._apply_mem_limit(1536)  # must not raise
    assert _isolation._MEM_LIMIT_APPLIED is True


def test_isolation_imports_on_windows_without_resource(monkeypatch):
    """Importing ``_isolation`` on Windows must not crash on ``import resource``.

    Regression for #877: a module-scope ``import resource`` raised
    ``ModuleNotFoundError`` on Windows and took down server startup. With
    ``sys.platform == 'win32'`` the module must import cleanly and bind
    ``resource`` to ``None``.
    """
    import importlib

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delitem(
        sys.modules,
        "nextcloud_mcp_server.document_processors._isolation",
        raising=False,
    )
    mod = importlib.import_module("nextcloud_mcp_server.document_processors._isolation")
    assert mod.resource is None


def _fake_parse_returning(n_pages: int):
    """Build a ``run_isolated_pdf_parse`` double yielding ``n_pages`` chunks.

    Must be a coroutine function -- the production call site awaits it -- which
    is why it has no ``await`` of its own.
    """

    async def fake_parse(content, **kwargs):
        return [{"text": "x", "metadata": {"page": i + 1}} for i in range(n_pages)]

    return fake_parse


# --- PyMuPDF processor wiring ------------------------------------------------


async def test_processor_success_builds_page_boundaries(monkeypatch):
    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc

    seen = {}

    async def fake_parse(content, **kwargs):
        seen.update(kwargs)
        return [{"text": "Hello world", "metadata": {"page": 1}}]

    monkeypatch.setattr(pymupdf_proc, "run_isolated_pdf_parse", fake_parse)

    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    result = await proc.process(_tiny_pdf(), "application/pdf", filename="t.pdf")

    assert result.success is True
    assert "Hello world" in result.text
    assert result.metadata["page_boundaries"][0]["page"] == 1
    # settings forwarded to the isolated parse
    assert seen["graphics_limit"] == 1000
    assert seen["timeout_seconds"] == 120
    assert seen["mem_limit_mb"] == 1536
    assert seen["markdown_max_pages"] == 150


async def test_page_boundaries_read_page_key_not_position(monkeypatch):
    """``page_boundaries`` must come from the metadata key, not chunk position.

    ``_build_page_boundaries`` falls back to a positional page number when the
    key is missing, so a wrong/renamed key degrades *silently* -- and every
    other test here uses chunks whose page numbers equal their positions, which
    the fallback reproduces exactly. Pinning the mapping with page numbers that
    deliberately differ from their positions is what makes this a real guard:
    If the worker ever switches to pymupdf4llm's layout extractor (which
    names this field ``page_number``), this fails instead of quietly
    emitting wrong citations off the positional fallback.
    """
    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc

    # Must be a coroutine function (the production call site awaits it), which
    # is why it has no await of its own; same shape as _fake_parse_returning
    # above. The suppression below has to sit on the flagged line to anchor.
    async def fake_parse(content, **kwargs):  # NOSONAR(S7503)
        # Pages 3 and 4 at positions 0 and 1 -- the positional fallback would
        # yield [1, 2] here.
        return [
            {"text": "page three", "metadata": {"page": 3}},
            {"text": "page four", "metadata": {"page": 4}},
        ]

    monkeypatch.setattr(pymupdf_proc, "run_isolated_pdf_parse", fake_parse)

    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    result = await proc.process(_tiny_pdf(), "application/pdf", filename="t.pdf")

    assert [b["page"] for b in result.metadata["page_boundaries"]] == [3, 4]


async def test_processor_reports_parse_mode(monkeypatch):
    """The mode must be observable: a silent gate is untunable.

    Derived in the parent from the returned page count (both worker paths emit
    one chunk per page) because a Counter incremented in the subprocess would
    never reach this process's registry.
    """
    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc

    # 3 pages against the default 150-page ceiling -> markdown.
    monkeypatch.setattr(
        pymupdf_proc, "run_isolated_pdf_parse", _fake_parse_returning(3)
    )
    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    result = await proc.process(_tiny_pdf(), "application/pdf", filename="t.pdf")
    assert result.metadata["parse_mode"] == "markdown"


async def test_processor_reports_text_only_mode_above_ceiling(monkeypatch):
    """A REAL 5-page document against a 4-page ceiling.

    The mode is derived from the authoritative ``page_count`` read off the
    document, not from ``len(page_chunks)``, so this must feed a genuinely
    multi-page PDF rather than a fake chunk list.
    """
    from types import SimpleNamespace

    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc

    def fake_settings():
        # Only the attributes process_source reads from settings.
        return SimpleNamespace(
            document_pdf_graphics_limit=1000,
            document_parse_timeout_seconds=120.0,
            document_parse_mem_limit_mb=1536,
            document_parse_process_slots=2,
            document_markdown_max_pages=4,  # 5 pages > 4 -> text_only
        )

    monkeypatch.setattr(
        pymupdf_proc, "run_isolated_pdf_parse", _fake_parse_returning(5)
    )
    monkeypatch.setattr(pymupdf_proc, "get_settings", fake_settings)

    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    result = await proc.process(_pdf_with_pages(5), "application/pdf", filename="t.pdf")
    assert result.metadata["parse_mode"] == "text_only"


async def test_processor_parse_failure_returns_success_false(monkeypatch):
    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc

    async def fake_parse(content, **kwargs):
        raise PdfParseFailed("oom", "killed")

    monkeypatch.setattr(pymupdf_proc, "run_isolated_pdf_parse", fake_parse)

    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    result = await proc.process(_tiny_pdf(), "application/pdf", filename="bomb.pdf")

    assert result.success is False
    assert result.text == ""
    assert result.metadata["parse_failed_reason"] == "oom"


# --- text-layer fallback when pymupdf4llm markdown under-extracts (drawings) ---


def _doc_with_text(text: str) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(36, 36, 559, 806), text, fontsize=10)
    return doc


def test_recover_replaces_underextracted_page():
    # A page whose raw text layer is rich, but whose markdown chunk recovered almost
    # nothing (the pymupdf4llm-on-a-drawing failure): fall back to get_text.
    full = " ".join(f"word{i}" for i in range(120))  # ~800 clean chars
    doc = _doc_with_text(full)
    try:
        chunks = [{"text": "tiny", "metadata": {}}]
        _isolation._recover_underextracted_pages(doc, chunks)
        assert "word100" in chunks[0]["text"]
        assert len(chunks[0]["text"]) > _isolation._TEXTLAYER_FALLBACK_MIN_CHARS
    finally:
        doc.close()


def test_recover_keeps_adequate_markdown():
    # Markdown that already captured the text (>= raw / ratio) is left untouched, so
    # structured output keeps its markdown structure for normal docs.
    full = " ".join(f"word{i}" for i in range(120))
    doc = _doc_with_text(full)
    try:
        good_md = "# Heading\n\n" + full
        chunks = [{"text": good_md, "metadata": {}}]
        _isolation._recover_underextracted_pages(doc, chunks)
        assert chunks[0]["text"] == good_md
    finally:
        doc.close()


# Lightweight fake doc/page for the edge paths (no pymupdf needed).
class _FakePage:
    def __init__(self, text: str, *, raises: bool = False) -> None:
        self._text = text
        self._raises = raises

    def get_text(self, _kind: str) -> str:
        if self._raises:
            raise RuntimeError("page extraction blew up")
        return self._text


class _FakeDoc:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages
        self.page_count = len(pages)

    def __getitem__(self, i: int) -> _FakePage:
        assert i < self.page_count, "indexed a page beyond page_count"
        return self._pages[i]


def test_recover_skips_page_on_get_text_error():
    # The per-page best-effort branch: a get_text failure leaves the chunk as-is.
    chunks = [{"text": "tiny", "metadata": {}}]
    _isolation._recover_underextracted_pages(
        _FakeDoc([_FakePage("", raises=True)]), chunks
    )
    assert chunks[0]["text"] == "tiny"


def test_recover_fires_on_empty_markdown():
    # Empty markdown (to_markdown returned nothing) + a rich raw layer -> fall back.
    rich = "x" * 200
    chunks = [{"text": "", "metadata": {}}]
    _isolation._recover_underextracted_pages(_FakeDoc([_FakePage(rich)]), chunks)
    assert chunks[0]["text"] == rich


def test_recover_ignores_chunks_beyond_page_count():
    # The i >= page_count guard: extra chunks (never expected from page_chunks=True)
    # are left untouched and no out-of-range page is indexed.
    rich = "y" * 200
    chunks = [{"text": "tiny", "metadata": {}}, {"text": "extra", "metadata": {}}]
    _isolation._recover_underextracted_pages(_FakeDoc([_FakePage(rich)]), chunks)
    assert chunks[0]["text"] == rich  # page 0 recovered
    assert chunks[1]["text"] == "extra"  # extra chunk untouched (loop broke)


# --- Parse-subprocess pool bound ---------------------------------------------
# anyio's default process limiter is os.cpu_count(), which neither --concurrency
# nor the pod memory limit constrains: on an 8-core node that permits 8 x
# document_parse_mem_limit_mb of address space inside a 3 GiB pod.


async def test_parse_process_limiter_is_bounded_by_setting():
    # async because the limiter is stored in a RunVar, which requires a running
    # event loop -- a sync test raises NoCurrentAsyncBackend.
    await anyio.lowlevel.checkpoint()
    from nextcloud_mcp_server.document_processors._isolation import (
        parse_process_limiter,
    )

    limiter = parse_process_limiter(3)

    assert limiter.total_tokens == 3


async def test_parse_process_limiter_is_reused_within_an_event_loop():
    """One limiter per loop, or the bound would not actually bind."""
    await anyio.lowlevel.checkpoint()  # RunVar needs a running loop
    from nextcloud_mcp_server.document_processors._isolation import (
        parse_process_limiter,
    )

    first = parse_process_limiter(3)
    second = parse_process_limiter(99)

    assert first is second, "a second call must not mint a fresh, wider limiter"


async def test_parse_process_limiter_never_zero():
    """A zero-slot limiter would deadlock every parse."""
    await anyio.lowlevel.checkpoint()  # RunVar needs a running loop
    from nextcloud_mcp_server.document_processors._isolation import (
        parse_process_limiter,
    )

    assert parse_process_limiter(0).total_tokens >= 1


async def test_isolated_parse_uses_the_bounded_limiter(mocker):
    """run_sync must be handed our limiter, not anyio's cpu_count default."""
    import anyio.to_process

    from nextcloud_mcp_server.document_processors import _isolation

    run_sync = mocker.patch.object(
        anyio.to_process, "run_sync", new=mocker.AsyncMock(return_value=[])
    )

    await _isolation.run_isolated_pdf_parse(
        b"%PDF-1.4",
        write_images=False,
        image_path=None,
        graphics_limit=1000,
        timeout_seconds=5.0,
        mem_limit_mb=512,
        markdown_max_pages=150,
        process_slots=2,
    )

    limiter = run_sync.await_args.kwargs["limiter"]
    assert limiter.total_tokens == 2


# --- markdown page gate (Deck #399) -----------------------------------------
#
# to_markdown is superlinear in page count, so a large document burns the whole
# parse timeout and dead-letters reason="timeout" -- throwing away a text layer
# get_text extracts in ~4.5 ms/page. These drive the REAL worker against real
# PDFs (no mocked to_markdown) so the gate is verified end to end.


def _pdf_with_pages(n: int, text: str = "Hello world") -> bytes:
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), f"{text} {i + 1}")
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _write(tmp_path, data: bytes) -> str:
    p = tmp_path / "doc.pdf"
    p.write_bytes(data)
    return str(p)


def test_gate_uses_text_only_above_ceiling(tmp_path):
    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(5))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 2)

    # One chunk per page, 1-based page numbers, real text recovered.
    assert len(chunks) == 5
    assert [c["metadata"]["page"] for c in chunks] == [1, 2, 3, 4, 5]
    assert "Hello world 1" in chunks[0]["text"]
    assert "Hello world 5" in chunks[4]["text"]


def test_gate_runs_markdown_at_or_below_ceiling(tmp_path):
    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(3))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 3)

    # Boundary is inclusive: page_count == ceiling still gets markdown, which
    # carries pymupdf4llm's richer metadata (the text-only path emits only
    # "page").
    assert len(chunks) == 3
    assert set(chunks[0]["metadata"]) > {"page"}


def test_worker_disables_pymupdf4llm_layout_mode(tmp_path):
    """The worker must stay on the classic (pymupdf_rag) extractor.

    pymupdf4llm >= 1.27.2.1 enables an ONNX layout model on import. Under this
    worker's RLIMIT_AS cap onnxruntime cannot reserve its address space and
    aborts mid-inference ("Missing Input: image_features"), failing the parse
    outright -- content-dependently, so some PDFs pass and others do not. It
    also returns unpicklable ``defaultdict(lambda: None)`` chunks and renames
    ``metadata["page"]`` to ``page_number``.

    Asserted through the metadata key because that is the externally visible
    difference between the two extractors: if ``use_layout(False)`` is dropped
    or upstream changes the default, this fails here rather than as an opaque
    parse error in production.
    """
    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(2))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 100)

    assert "page" in chunks[0]["metadata"]
    assert "page_number" not in chunks[0]["metadata"]


def test_markdown_chunks_are_picklable(tmp_path):
    """The worker's return value crosses a process boundary, so it must pickle.

    ``run_isolated_pdf_parse`` hands this list back through
    ``anyio.to_process.run_sync``, which pickles it. pymupdf4llm 1.28 builds
    each chunk as ``defaultdict(lambda: None)``, and a local lambda as
    default_factory is unpicklable -- returning the chunks unconverted failed
    every real parse with "Can't pickle local object
    'make_page_chunk.<locals>.<lambda>'" while every unit test still passed,
    because they all call ``_parse_pdf_worker`` in-process.

    Asserting picklability directly keeps that gap closed without paying for a
    subprocess in the unit tier.
    """
    import pickle

    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(2))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 100)

    # Plain dicts, not defaultdicts carrying an unpicklable factory.
    assert all(type(c) is dict for c in chunks)
    assert pickle.loads(pickle.dumps(chunks)) == chunks


def test_text_only_chunks_are_picklable(tmp_path):
    """The text-only path returns through the same pickling boundary."""
    import pickle

    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(2))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 0)

    assert all(type(c) is dict for c in chunks)
    assert pickle.loads(pickle.dumps(chunks)) == chunks


def test_gate_zero_disables_markdown_entirely(tmp_path):
    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(2))
    chunks = _parse_pdf_worker(path, False, None, 1000, 0, 0)

    assert len(chunks) == 2
    assert chunks[0]["metadata"] == {"page": 1}
    assert "Hello world 1" in chunks[0]["text"]


def test_text_only_chunks_match_markdown_page_count_and_order(tmp_path):
    """Both paths must agree on shape -- page_boundaries depends on it."""
    from nextcloud_mcp_server.document_processors._isolation import _parse_pdf_worker

    path = _write(tmp_path, _pdf_with_pages(4))
    md = _parse_pdf_worker(path, False, None, 1000, 0, 100)
    raw = _parse_pdf_worker(path, False, None, 1000, 0, 1)

    assert len(md) == len(raw) == 4
    assert [c["metadata"]["page"] for c in md] == [c["metadata"]["page"] for c in raw]
    # Text layer is the source of truth: the raw path must not lose content.
    for m, r in zip(md, raw, strict=True):
        assert m["text"].strip()
        assert r["text"].strip()


def test_text_only_path_survives_a_page_that_cannot_be_read(monkeypatch):
    """A per-page failure degrades that page, never the whole document."""
    from nextcloud_mcp_server.document_processors._isolation import _text_only_chunks

    class _Page:
        def __init__(self, ok: bool) -> None:
            self._ok = ok

        def get_text(self, _mode: str) -> str:
            if not self._ok:
                raise RuntimeError("corrupt page")
            return "fine"

    class _Doc:
        page_count = 3

        def __getitem__(self, i: int) -> _Page:
            return _Page(ok=(i != 1))

    chunks = _text_only_chunks(_Doc())
    assert [c["text"] for c in chunks] == ["fine", "", "fine"]
    assert [c["metadata"]["page"] for c in chunks] == [1, 2, 3]


def test_uses_markdown_is_the_single_source_of_truth():
    """The worker and the metric label must agree; both call this predicate."""
    from nextcloud_mcp_server.document_processors._isolation import uses_markdown

    assert uses_markdown(page_count=1, markdown_max_pages=150) is True
    assert uses_markdown(page_count=150, markdown_max_pages=150) is True  # inclusive
    assert uses_markdown(page_count=151, markdown_max_pages=150) is False
    # <=0 disables markdown entirely, however small the document.
    assert uses_markdown(page_count=1, markdown_max_pages=0) is False
    assert uses_markdown(page_count=1, markdown_max_pages=-5) is False


async def test_parse_mode_counter_increments(monkeypatch):
    """The gate must be observable in Prometheus, not just on the result."""
    from nextcloud_mcp_server.document_processors import pymupdf as pymupdf_proc
    from nextcloud_mcp_server.observability import metrics

    def _value(mode: str) -> float:
        return (
            metrics.document_parse_mode_total.labels(mode=mode)._value.get()  # noqa: SLF001
        )

    before = _value("markdown")

    monkeypatch.setattr(
        pymupdf_proc, "run_isolated_pdf_parse", _fake_parse_returning(1)
    )
    proc = pymupdf_proc.PyMuPDFProcessor(extract_images=False)
    await proc.process(_tiny_pdf(), "application/pdf", filename="t.pdf")

    assert _value("markdown") == before + 1
