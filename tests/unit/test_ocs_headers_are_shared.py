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

import nextcloud_mcp_server as root_pkg

pytestmark = pytest.mark.unit

#: The module that defines the constant is the one place allowed to spell it.
DEFINING_MODULE = "client/ocs.py"

#: The keys whose co-occurrence in one dict literal makes it a copy of
#: ``OCS_REQUEST_HEADERS``.
OWNED_PAIRING = {"OCS-APIRequest", "Accept"}


def _package_root() -> Path:
    return Path(root_pkg.__file__).parent


def _rel(path: Path) -> str:
    """Package-relative POSIX path, e.g. ``client/sharing.py``.

    Bare filenames are not identifiers here: the package holds a ``deck.py``, a
    ``talk.py`` and a ``sharing.py`` under each of ``client/``, ``models/`` and
    ``server/``, plus an ``__init__.py`` in every subpackage. Matching on
    ``path.name`` silently conflated all of them the moment this scan grew
    beyond ``client/``.
    """
    return path.relative_to(_package_root()).as_posix()


def _package_sources() -> list[Path]:
    """Every module in the distribution, not just ``client/``.

    Scoping this to the client package is what let two call sites outside it
    keep retyping the pairing -- ``api/apps.py`` and ``auth/storage.py`` both
    build their own ``httpx.AsyncClient`` for an ``/ocs/v2.php`` route. A guard
    that only watches the place the problem was already fixed is a guard that
    reports success.
    """
    return sorted(_package_root().rglob("*.py"))


def _copied_pairings(path: Path) -> list[int]:
    """Line numbers of dict literals carrying the constant's whole pairing."""
    tree = ast.parse(path.read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and OWNED_PAIRING <= {k.value for k in node.keys if isinstance(k, ast.Constant)}
    ]


def test_scan_reaches_beyond_the_client_package():
    """Guard the guard: a bad glob would make the scan below vacuously pass.

    Named modules from three different packages, so narrowing the scan back to
    ``client/`` fails here rather than silently shrinking what is protected.
    """
    found = {_rel(p) for p in _package_sources()}
    assert {
        "client/ocs.py",
        "client/sharing.py",
        "client/deck.py",
        "client/webdav.py",
    } <= found
    assert {"api/apps.py", "auth/storage.py"} <= found


def test_the_constant_is_still_the_pairing_being_guarded():
    """Guard the guard: if OCS_REQUEST_HEADERS changes shape, so must this test.

    Without it, renaming a key in ``ocs.py`` would leave every assertion below
    scanning for a pairing that no longer exists — passing while the sprawl it
    was written to catch quietly returns.
    """
    from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

    assert set(OCS_REQUEST_HEADERS) == OWNED_PAIRING


def test_no_client_retypes_the_ocs_header_pairing():
    """Every JSON OCS call site must reach the header through the constant.

    Not only the app clients: anything talking to ``/ocs/v2.php`` needs the
    pairing, including modules that build a one-off ``httpx.AsyncClient``.
    """
    offenders = [
        f"{_rel(path)}:{lineno}"
        for path in _package_sources()
        if _rel(path) != DEFINING_MODULE
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
        "client/sharing.py",
        "client/collectives.py",
        "client/mail.py",
        "client/deck.py",
        "client/groups.py",
        "client/tables.py",
        "client/users.py",
        "client/talk.py",
        "client/__init__.py",
        "api/apps.py",
        "auth/storage.py",
    }
    present = {_rel(p) for p in _package_sources()}
    assert expected <= present, f"expected modules are gone: {expected - present}"
    missing = {
        rel
        for path in _package_sources()
        if (rel := _rel(path)) in expected
        and "OCS_REQUEST_HEADERS" not in path.read_text()
    }
    assert not missing, f"OCS clients not using the shared header: {sorted(missing)}"


def test_the_constant_cannot_be_mutated():
    """Nine clients share this one object; an in-place edit must not be possible.

    The per-client ``dict(...)`` copies exist because of that sharing. Making
    the source read-only turns the mistake those copies guard against into a
    ``TypeError`` at the point it is made, rather than a cross-client bug
    discovered later in whichever client happened to run next.
    """
    from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

    with pytest.raises(TypeError):
        OCS_REQUEST_HEADERS["Accept"] = "text/xml"  # ty: ignore[unsupported-operation]
