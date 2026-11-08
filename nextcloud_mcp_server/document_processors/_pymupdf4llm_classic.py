"""Load pymupdf4llm pinned to its classic (``pymupdf_rag``) extractor.

Every caller of ``pymupdf4llm.to_markdown`` in this codebase must go through
:func:`load_classic_pymupdf4llm`. Two independent reasons:

* **Memory.** Layout mode (pymupdf4llm's default since 1.27.2.1) routes
  ``to_markdown`` through an ONNX layout-detection model, which costs ~700-1150
  MiB of address space. The isolated parse worker runs under a 1536 MiB
  RLIMIT_AS, so paying that cost leaves ~117 MiB for the parse itself -- which is
  how a perfectly healthy document ends up dead-lettered (Deck #911).

  *When* the cost lands depends on the major version. In 1.x, ``import
  pymupdf4llm`` imported ``pymupdf.layout`` (and onnxruntime) on the spot:
  VmSize 1191 MiB with layout vs 499 MiB without for the import alone, and 1476
  vs 891 MiB for the ingest worker carrying the whole app. Since 2.0 (where both
  modules ship inside the pymupdf wheel) the import is cheap and the model
  loads lazily on the first ``to_markdown`` with layout enabled: measured on
  2.1, VmSize after a 20-page parse is 1166 MiB with layout vs 478 MiB through
  this loader, and onnxruntime is never imported.
* **Consistency.** The classic and layout extractors emit different markdown
  (and name the page key ``page`` vs ``page_number``). ``search/pdf_highlighter``
  recomputes offsets that must match what indexing produced, so the two must
  agree on the extractor or highlight offsets silently misalign.

pymupdf4llm picks its default at import time: 1.x by trying ``import
pymupdf.layout``, 2.x by ``pymupdf._layout_available()`` (a ``find_spec`` on
the same name). Binding ``pymupdf.layout`` to None in ``sys.modules`` makes
both report "not installed", so the default comes out classic and nothing can
import the layout module later in this process. The explicit
``use_layout(False)`` -- which in 2.x clears the process-wide
``pymupdf._layout_enabled`` gate on the lazy model load -- still disables
inference if the block ever stops working (upstream renaming the module, say).

In 1.x the block could not undo an import that already happened, so **this
must run before anything else imports pymupdf4llm** -- which is why every
caller imports it lazily rather than at module scope. 2.x defers the cost to
``to_markdown``, so a stray early import is no longer fatal on its own, but
keep the rule: ``anyio.to_process`` re-imports the parent's ``__main__`` inside
every parse worker, and only this loader guarantees layout is off there.

Both effects are process-wide and sticky for the life of the process; nothing in
this codebase needs real layout mode. Anything that later does must not share a
process with these callers, because ``setdefault`` cannot be undone by a
subsequent import.
"""

import sys
from typing import Any

__all__ = ["load_classic_pymupdf4llm"]


def load_classic_pymupdf4llm() -> Any:
    """Import pymupdf4llm with layout mode disabled, and return the module."""
    sys.modules.setdefault("pymupdf.layout", None)  # type: ignore[assignment, ty:no-matching-overload]

    import pymupdf4llm  # noqa: PLC0415

    pymupdf4llm.use_layout(False)
    return pymupdf4llm
