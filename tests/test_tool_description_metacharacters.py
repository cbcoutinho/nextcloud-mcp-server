"""Guard against shell metacharacters in MCP tool descriptions.

Anti-injection MCP gateways (e.g. IBM mcp-context-forge) refuse to expose a tool
whose description contains shell metacharacters. The tool is silently dropped
from ``tools/list``: the server starts fine, the gateway reports no error, and
the tool simply never reaches the client.

Semicolons used as ordinary English punctuation are enough to trigger this. See
https://github.com/cbcoutinho/nextcloud-mcp-server/issues/1183, where 29 of 162
tools were dropped this way -- including ``nc_webdav_read_file``, without which
the server has little use.

Because docstrings are the descriptions, this is a documentation-style rule and
a static check over the source is enough: it needs no Nextcloud instance.
"""

import ast
import pathlib

import pytest

# The CI job runs ``pytest -m unit``, so an unmarked test is collected and then
# deselected: it never fails, it just silently stops running.
pytestmark = pytest.mark.unit

METACHARACTERS = ("&&", ";", "||", "$(", "> ", "< ")

# Tools whose description legitimately contains a metacharacter inside a quoted
# literal, where removing it would document invalid syntax. These stay dropped
# by strict gateways -- a deliberate trade-off, not an oversight.
ALLOWED_TOOLS = {
    "nc_calendar_create_event",  # RFC 5545: ``DTSTART;TZID=`` and ``FREQ=...;BYDAY=``
    "nc_webdav_write_file",  # MIME: ``'type;base64'``
}

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "nextcloud_mcp_server"


def _iter_tool_docstrings():
    """Yield (tool_name, docstring, path) for every ``@mcp.tool``-decorated function."""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any("tool" in ast.dump(d) for d in node.decorator_list):
                continue
            docstring = ast.get_docstring(node)
            if docstring:
                yield node.name, docstring, path


def test_tool_descriptions_have_no_shell_metacharacters():
    offenders = []
    for name, docstring, path in _iter_tool_docstrings():
        if name in ALLOWED_TOOLS:
            continue
        found = sorted({m for m in METACHARACTERS if m in docstring})
        if found:
            offenders.append(f"{path.name}::{name} contains {found}")

    assert not offenders, (
        "Tool descriptions must not contain shell metacharacters, or "
        "anti-injection gateways will silently drop these tools:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse ordinary punctuation instead (a period or a comma rather than "
        "a semicolon). If the character is required syntax that cannot be "
        "reworded, add the tool to ALLOWED_TOOLS with a comment explaining why."
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted tool that no longer needs it should leave the allowlist."""
    names = {name for name, _, _ in _iter_tool_docstrings()}
    unknown = ALLOWED_TOOLS - names
    assert not unknown, f"ALLOWED_TOOLS references unknown tools: {sorted(unknown)}"

    still_needed = {
        name
        for name, docstring, _ in _iter_tool_docstrings()
        if name in ALLOWED_TOOLS and any(m in docstring for m in METACHARACTERS)
    }
    obsolete = ALLOWED_TOOLS - still_needed
    assert not obsolete, (
        f"These tools no longer contain metacharacters and should be removed "
        f"from ALLOWED_TOOLS: {sorted(obsolete)}"
    )


@pytest.mark.parametrize("metacharacter", METACHARACTERS)
def test_detector_catches_each_metacharacter(metacharacter):
    """Positive control: the rule must actually fire on each metacharacter."""
    docstring = f"Does a thing{metacharacter}then another thing."
    assert any(m in docstring for m in METACHARACTERS)
