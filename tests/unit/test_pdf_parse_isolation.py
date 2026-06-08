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
