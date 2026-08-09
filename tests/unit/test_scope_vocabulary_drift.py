"""Guards against drift between the places that list scopes.

``ALL_SUPPORTED_SCOPES`` is the vocabulary; several other lists must cover it.
Each has drifted at least once, and every time the symptom was the same: a tool
that works under BasicAuth and is permanently uncallable in OAuth / Login Flow
v2, because the scope it requires can never be granted. ``mail.send`` went that
way, then ``semantic.read`` (GH #1277).

These are cheap set comparisons — the point is that they fail here rather than
in a deployment.
"""

import re
from pathlib import Path

import pytest

from nextcloud_mcp_server.app import build_dcr_scopes
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES
from tests.conftest import DEFAULT_FULL_SCOPES

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ASTROLABE_OAUTH_HOOK = (
    REPO_ROOT / "app-hooks" / "before-starting" / "26-configure-astrolabe-oauth.sh"
)


def _hook_allowed_scopes() -> set[str]:
    """Parse ALLOWED_SCOPES out of the Astrolabe OIDC client setup hook."""
    match = re.search(
        r'^ALLOWED_SCOPES="([^"]+)"', ASTROLABE_OAUTH_HOOK.read_text(), re.MULTILINE
    )
    assert match, f"ALLOWED_SCOPES not found in {ASTROLABE_OAUTH_HOOK}"
    return set(match.group(1).split())


def test_dcr_advertises_every_supported_scope():
    """The DCR registration must cover the whole vocabulary."""
    advertised = set(
        build_dcr_scopes(vector_sync_enabled=True, offline_access_enabled=True).split()
    )
    assert ALL_SUPPORTED_SCOPES <= advertised, (
        f"not advertised via DCR: {sorted(ALL_SUPPORTED_SCOPES - advertised)}"
    )


def test_dcr_emits_semantic_read_once_when_enabled():
    """semantic.read is a vocabulary member *and* conditionally appended.

    It must not be emitted twice — the subtraction in build_dcr_scopes is what
    prevents that, and nothing else would notice if it were dropped.
    """
    scopes = build_dcr_scopes(
        vector_sync_enabled=True, offline_access_enabled=False
    ).split()
    assert scopes.count("semantic.read") == 1


def test_dcr_omits_semantic_read_when_vector_sync_disabled():
    """Advertising a scope for tools that are not registered would be a lie."""
    scopes = build_dcr_scopes(
        vector_sync_enabled=False, offline_access_enabled=False
    ).split()
    assert "semantic.read" not in scopes


def test_astrolabe_oidc_client_allows_every_supported_scope():
    """The Astrolabe OIDC client is a hard ceiling on the tokens it issues.

    A scope missing from its allowed list cannot reach a token, and since the
    token gates every tool call, those tools silently vanish for Astrolabe
    users. mail.* and talk.* were missing exactly this way.
    """
    missing = ALL_SUPPORTED_SCOPES - _hook_allowed_scopes()
    assert not missing, f"Astrolabe OIDC client cannot grant: {sorted(missing)}"


def test_full_access_test_token_carries_every_supported_scope():
    """The "full access" OAuth fixture must not be quietly narrower than the
    vocabulary, or e2e tests pass while real deployments lose those tools."""
    missing = ALL_SUPPORTED_SCOPES - set(DEFAULT_FULL_SCOPES.split())
    assert not missing, f"DEFAULT_FULL_SCOPES is missing: {sorted(missing)}"
