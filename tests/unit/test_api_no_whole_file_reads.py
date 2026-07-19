"""Guard: the interactive HTTP API must not buffer whole files into memory.

``/api/v1/pdf-preview`` rendered PDF pages server-side, which meant
``webdav.read_file`` pulled the entire document into the API pod before the
50 MB size guard could reject it. A 251 MB scan exhausted the pod's 1 GiB limit
and it was OOMKilled mid-request; the resulting 503 reached the user as a 5xx
and a blank chunk viewer.

Rasterization now happens in the browser against the copy already in Nextcloud,
so no interactive endpoint needs file bytes at all. Search and chunk-context
serve bboxes straight from the Qdrant payload.

These tests pin that invariant rather than the deletion alone: re-adding a
buffered read to the API package is the regression that matters, and it would
otherwise only show up as an OOMKill under a large document in production.
Ingest is deliberately out of scope — it reads documents by design, via
``webdav.stream_to_file`` with a byte cap.
"""

import ast
from pathlib import Path

import pytest

import nextcloud_mcp_server.api as api_pkg

pytestmark = pytest.mark.unit

# Buffering reads. ``stream_to_file`` is intentionally absent: it holds one
# chunk at a time and takes a max_bytes cap, so it is the safe alternative.
BUFFERING_READS = {"read_file"}


def _api_source_files() -> list[Path]:
    return sorted(Path(api_pkg.__file__).parent.glob("*.py"))


def test_api_package_has_source_files():
    """Guard the guard: a bad glob would make the scan below vacuously pass."""
    assert _api_source_files(), "no API source files found to scan"


@pytest.mark.parametrize("path", _api_source_files(), ids=lambda p: p.name)
def test_api_module_never_buffers_a_whole_file(path: Path):
    """No handler in the API package may call a whole-file read."""
    tree = ast.parse(path.read_text(), filename=str(path))

    offenders = [
        f"{path.name}:{node.lineno} calls {node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in BUFFERING_READS
    ]

    assert not offenders, (
        "Interactive API handlers must not buffer whole files into memory "
        f"(OOMKills the API pod on large documents): {offenders}. "
        "Use webdav.stream_to_file(..., max_bytes=...) if a read is genuinely "
        "required, or serve the bytes to the browser from Nextcloud directly."
    )


def test_pdf_preview_handler_is_gone():
    """The removed endpoint must not come back by import."""
    assert not hasattr(api_pkg, "get_pdf_preview")
    assert "get_pdf_preview" not in getattr(api_pkg, "__all__", [])


def test_pdf_preview_route_is_not_registered():
    """The route table must not serve /api/v1/pdf-preview."""
    app_source = (Path(api_pkg.__file__).parent.parent / "app.py").read_text()

    assert "/api/v1/pdf-preview" not in app_source
