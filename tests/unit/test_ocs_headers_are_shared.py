"""Guard: the OCS+JSON header pairing lives in one constant, not at each call.

Omitting ``OCS-APIRequest: true`` does not produce a 4xx. Nextcloud answers
``200`` with ``meta.statuscode: 997``, which reads as a server fault and sends
whoever is debugging it hunting for one. ``client/ocs.py`` exists to make that
failure legible; the literal being re-typed at every new call site is what
keeps re-creating it.

What is pinned here is the specific pairing the constant owns --
``OCS-APIRequest`` *together with* ``Accept: application/json``, the shape every
JSON-returning ``/ocs/v2.php`` call needs. Header dicts that send
``OCS-APIRequest`` on its own are a different set with their own reasons (DAV
verbs answered in XML, binary attachment downloads, Deck's own REST API) and
are deliberately not folded in.

The scan is over the AST rather than over lines, so a pairing split across two
lines by the formatter still counts.
"""

import ast
from pathlib import Path

import pytest

import nextcloud_mcp_server.client as client_pkg

pytestmark = pytest.mark.unit

#: The module that defines the constant is the one place allowed to spell it.
DEFINING_MODULE = "ocs.py"

#: The keys whose co-occurrence in one dict literal makes it a copy of
#: ``OCS_REQUEST_HEADERS``.
OWNED_PAIRING = {"OCS-APIRequest", "Accept"}


def _client_sources() -> list[Path]:
    return sorted(Path(client_pkg.__file__).parent.rglob("*.py"))


def _copied_pairings(path: Path) -> list[int]:
    """Line numbers of dict literals carrying the constant's whole pairing."""
    tree = ast.parse(path.read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and OWNED_PAIRING <= {k.value for k in node.keys if isinstance(k, ast.Constant)}
    ]


def test_scan_finds_the_client_package():
    """Guard the guard: a bad glob would make the scan below vacuously pass."""
    names = {p.name for p in _client_sources()}
    assert {"ocs.py", "sharing.py", "deck.py", "webdav.py"} <= names


def test_the_constant_is_still_the_pairing_being_guarded():
    """Guard the guard: if OCS_REQUEST_HEADERS changes shape, so must this test.

    Without it, renaming a key in ``ocs.py`` would leave every assertion below
    scanning for a pairing that no longer exists — passing while the sprawl it
    was written to catch quietly returns.
    """
    from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

    assert set(OCS_REQUEST_HEADERS) == OWNED_PAIRING


def test_no_client_retypes_the_ocs_header_pairing():
    """Every JSON OCS call site must reach the header through the constant."""
    offenders = [
        f"{path.name}:{lineno}"
        for path in _client_sources()
        if path.name != DEFINING_MODULE
        for lineno in _copied_pairings(path)
    ]
    assert not offenders, (
        "these call sites re-type the OCS+JSON header pairing instead of using "
        f"client.ocs.OCS_REQUEST_HEADERS: {offenders}"
    )


def test_every_ocs_client_imports_the_constant():
    """The clients that call /ocs/v2.php must import the constant they need.

    Checked separately from the literal scan because a client can pass that one
    simply by sending no header at all -- which is the 997 this whole module
    exists to prevent.
    """
    expected = {
        "sharing.py",
        "collectives.py",
        "mail.py",
        "deck.py",
        "groups.py",
        "tables.py",
        "users.py",
        "talk.py",
        "__init__.py",
    }
    missing = {
        path.name
        for path in _client_sources()
        if path.name in expected and "OCS_REQUEST_HEADERS" not in path.read_text()
    }
    assert not missing, f"OCS clients not using the shared header: {sorted(missing)}"
