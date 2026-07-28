"""Load pymupdf4llm pinned to its classic (``pymupdf_rag``) extractor.

Every caller of ``pymupdf4llm.to_markdown`` in this codebase must go through
:func:`load_classic_pymupdf4llm`. Two independent reasons:

* **Memory.** 1.27.2.1 made ``import pymupdf4llm`` initialise ``pymupdf.layout``
  and route ``to_markdown`` through an ONNX layout-detection model, which costs
  ~1157 MiB of address space. The isolated parse worker runs under a 1536 MiB
  RLIMIT_AS, so paying that cost leaves ~117 MiB for the parse itself -- which is
  how a perfectly healthy document ends up dead-lettered (Deck #911). Binding
  ``pymupdf.layout`` to None in ``sys.modules`` -- the standard way to make an
  import raise -- takes pymupdf4llm's classic branch and avoids the cost.

  Two baselines get quoted for that saving; they measure different processes, so
  to be explicit about which is which. Importing *only* pymupdf4llm: VmSize 1191
  MiB with layout, 499 MiB without. The ingest worker, which also carries the
  whole app: 1476 MiB with, 891 MiB without (its ``to_process`` parse child
  tracks ~55 MiB below each, so 1419 MiB was the figure that actually blew the
  1536 MiB cap).
* **Consistency.** The classic and layout extractors emit different markdown
  (and name the page key ``page`` vs ``page_number``). ``search/pdf_highlighter``
  recomputes offsets that must match what indexing produced, so the two must
  agree on the extractor or highlight offsets silently misalign.

pymupdf4llm decides at import time with::

    try: import pymupdf.layout
    except ImportError: use_layout(False)
    else: use_layout(True)

hence both mechanisms here: the ``sys.modules`` block avoids the address-space
cost, and the explicit ``use_layout(False)`` still disables inference if that
block ever stops working (upstream renaming the module, say). The block cannot
undo an import that already happened, so **this must run before anything else
imports pymupdf4llm** -- which is why every caller imports it lazily rather than
at module scope. A module-level ``import pymupdf4llm`` anywhere in the app's
import graph re-breaks the memory guarantee for the whole process tree, because
``anyio.to_process`` re-imports the parent's ``__main__`` inside every parse
worker before the worker function runs.

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
