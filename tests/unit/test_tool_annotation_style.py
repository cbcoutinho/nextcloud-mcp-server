"""Guard against silently discarded ToolAnnotations hints.

``ToolAnnotations`` is a pydantic model with snake_case fields. A camelCase
kwarg (``readOnlyHint=True``) is not a validation error — pydantic drops it —
so the tool ships with no annotation at all and nothing fails at runtime. Only
``ty`` notices, which is how five of them reached master (#1393). This pins the
spelling at the source level so a reintroduction fails a plain unit run.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CAMEL_HINT = re.compile(r"\b(readOnly|openWorld|destructive|idempotent)Hint\s*=")
_SERVER_DIR = Path(__file__).resolve().parents[2] / "nextcloud_mcp_server" / "server"


def test_tool_annotations_use_snake_case_hints():
    offenders = [
        f"{path.relative_to(_SERVER_DIR.parents[1])}:{lineno}: {line.strip()}"
        for path in sorted(_SERVER_DIR.rglob("*.py"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _CAMEL_HINT.search(line)
    ]
    assert not offenders, (
        "ToolAnnotations hints must be snake_case (read_only_hint, "
        "open_world_hint, destructive_hint, idempotent_hint) — pydantic "
        "discards camelCase kwargs:\n" + "\n".join(offenders)
    )
