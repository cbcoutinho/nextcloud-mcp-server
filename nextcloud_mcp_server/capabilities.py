"""Reads what a Nextcloud instance advertises on its OCS capabilities endpoint.

Two consumers, one cached lookup of ``/ocs/v2.php/cloud/capabilities``:

**Admin-approved searchable sources.** The Astrolabe Nextcloud app advertises,
per user, which content sources an admin has approved for semantic search, under
``capabilities.astrolabe.semantic_search.enabled_doc_types``. This is the single
source of truth for admin consent: the search layer filters results to these
doc types, and the indexing layer (scanner + webhook ingest) skips everything
else (the hard data-at-rest guarantee is the eager purge Astrolabe triggers on
disable; see ``vector.purge``).

**Per-tool capability gates.** ``@require_capability(app, min_version=...)``
marks an MCP tool as needing an upstream app to be present (and new enough) —
Nextcloud apps advertise themselves as ``capabilities.<app>.version``. A gated
tool is hidden from ``tools/list`` and refused by ``tools/call`` on instances
that cannot serve it, instead of the model discovering a 404 the hard way.

Fail-open throughout: if a capability block is absent (an older app that
predates the feature) or the OCS call fails, ``allowed_doc_types`` returns
``None`` meaning "no restriction" and a gate is treated as satisfied, so nothing
regresses on an instance we cannot interrogate. For ``allowed_doc_types``,
``None`` is distinct from an empty set, which means "the admin disabled every
source".
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError
from packaging.version import InvalidVersion, Version

from nextcloud_mcp_server.config import cfg_bool
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.request_context import current_context

logger = logging.getLogger(__name__)

# Short-lived per-user cache for the OCS capabilities lookup. Admin consent and
# the installed app set change rarely, but search/scan paths and every
# ``tools/list`` consult them, so trade a little staleness for keeping the OCS
# round-trip off the hot path. Mirrors the list_accessible_owners cache in
# search/access_filter.py.
#
# The TTL is also what makes gating self-healing: enabling or upgrading an app
# surfaces its tools within one window, with no server restart.
#
# Keyed by user_id even though most of the payload is instance-wide: the OCS
# call is authenticated per-user (``installed`` resolves per-user on the
# Astrolabe side, and Talk omits its whole block for a user it is disabled for),
# so we cache per-user for correctness. The redundancy is bounded by
# _CACHE_MAXSIZE; on an admin change all entries reconverge within one TTL
# window.
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAXSIZE = 1024
# user_id -> (monotonic_ts, raw OCS payload as returned by client.capabilities())
_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()


class _CapabilitiesClientProtocol(Protocol):
    async def capabilities(self) -> Any: ...


def _capabilities_block(payload: Any) -> dict | None:
    """The ``ocs.data.capabilities`` mapping, or ``None`` if unreadable."""
    if not isinstance(payload, dict):
        return None
    try:
        caps = payload["ocs"]["data"]["capabilities"]
    except (KeyError, TypeError):
        return None
    return caps if isinstance(caps, dict) else None


def _parse_enabled_doc_types(payload: Any) -> frozenset[str] | None:
    """Extract ``enabled_doc_types`` from an OCS capabilities payload.

    Returns ``None`` when the ``astrolabe.semantic_search`` block is absent or
    malformed (treated as "no restriction"). Returns a frozenset (possibly
    empty) when the block is present and well-formed; an empty set means the
    admin disabled every source.
    """
    caps = _capabilities_block(payload)
    if caps is None:
        return None
    block = caps.get("astrolabe")
    if not isinstance(block, dict):
        return None
    semantic = block.get("semantic_search")
    if not isinstance(semantic, dict):
        return None
    raw = semantic.get("enabled_doc_types")
    if not isinstance(raw, list):
        return None
    return frozenset(dt for dt in raw if isinstance(dt, str) and dt)


async def _capabilities(
    client: _CapabilitiesClientProtocol, user_id: str
) -> Any | None:
    """The OCS capabilities payload for ``user_id``, or ``None`` if unavailable.

    Cached per user with a short TTL (+ LRU eviction). Failures are not cached so
    a transient OCS hiccup retries on the next call.
    """
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        _cache.move_to_end(user_id)  # mark recently used (LRU)
        return cached[1]

    try:
        payload = await client.capabilities()
    except Exception as exc:  # noqa: BLE001 — degrade gracefully (fail-open)
        logger.warning(
            "Nextcloud capabilities unavailable for user %s (%s)", user_id, exc
        )
        return None  # don't cache failures — retry next call

    _cache[user_id] = (now, payload)
    # Needed only for an existing (expired) key: __setitem__ updates it in place,
    # keeping its old position, so move it to the end to preserve LRU order. For
    # a brand-new key __setitem__ already appends, so this is a harmless no-op.
    _cache.move_to_end(user_id)
    while len(_cache) > _CACHE_MAXSIZE:
        _cache.popitem(last=False)  # evict least-recently-used
    return payload


async def allowed_doc_types(
    client: _CapabilitiesClientProtocol, user_id: str
) -> frozenset[str] | None:
    """Admin-approved doc types for ``user_id``, or ``None`` for "no restriction".

    Fail-open: a missing capability block or a failed lookup yields ``None`` so
    search remains available.
    """
    payload = await _capabilities(client, user_id)
    if payload is None:
        logger.warning(
            "Not restricting doc types for user %s this cycle "
            "(capabilities unavailable)",
            user_id,
        )
        return None
    return _parse_enabled_doc_types(payload)


def is_doc_type_allowed(doc_type: str, allowed: frozenset[str] | None) -> bool:
    """Whether ``doc_type`` may be indexed/searched given an allow-set.

    ``allowed=None`` means "no restriction" (fail-open / older Astrolabe), so
    everything is permitted.
    """
    return allowed is None or doc_type in allowed


def clear_cache() -> None:
    """Test hook: drop all cached entries."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Per-tool capability gates
# ---------------------------------------------------------------------------

#: Where the gate is stashed on a tool function:
#: ``(app, min_version | None, feature | None)``.
#: Mirrors ``_required_scopes`` in auth/scope_authorization.py — metadata on the
#: function, read back at ``tools/list`` / ``tools/call`` time. ``functools.wraps``
#: copies ``__dict__``, so this survives (and composes with) ``@require_scopes``
#: in either decorator order.
_GATE_ATTR = "_required_capability"


def require_capability(
    app: str, min_version: str | None = None, feature: str | None = None
) -> Callable:
    """Gate an MCP tool on what the upstream Nextcloud app advertises.

    ``app`` is the OCS capability key (the app id for most apps — note Talk's is
    ``spreed``). With ``min_version`` the app must also advertise a
    ``version`` at least that high. With ``feature`` it must list that string
    in its advertised ``features``.

    Prefer ``feature`` where the app publishes one: it states what the tool
    actually needs, and it is checked against what the instance says about
    itself rather than against a version floor someone has to look up and keep
    correct.

    Only use this for apps that actually publish a capability block: absence of
    the key is what closes the gate, so gating an app that advertises nothing
    would hide working tools. See ``APP_CAPABILITY_KEY`` in
    ``nextcloud_mcp_server/server/__init__.py`` for the verified set.

    Example:
        ```python
        @mcp.tool()
        @require_capability("deck", min_version="1.18.0")
        async def deck_assign_dependent_card(ctx: Context, ...): ...
        ```
    """
    if min_version is not None:
        Version(min_version)  # fail at import on a typo'd floor, not at runtime

    def decorator(func: Callable) -> Callable:
        setattr(func, _GATE_ATTR, (app, min_version, feature))
        return func

    return decorator


def get_required_capability(
    func: Callable,
) -> tuple[str, str | None, str | None] | None:
    """The ``(app, min_version, feature)`` gate declared on ``func``, if any."""
    return getattr(func, _GATE_ATTR, None)


def stamp_required_capability(func: Callable, app: str) -> None:
    """Apply an app-presence gate to ``func`` unless it declares its own.

    Used for whole-module gating; a per-tool ``@require_capability`` (which may
    carry a stricter version floor) always wins.
    """
    if get_required_capability(func) is None:
        setattr(func, _GATE_ATTR, (app, None, None))


async def unmet_capability(
    client: _CapabilitiesClientProtocol,
    user_id: str,
    app: str,
    min_version: str | None,
    feature: str | None = None,
) -> str | None:
    """Why this instance cannot serve a tool gated on ``app``, else ``None``.

    ``None`` means "allowed", including every case where the answer is unknown
    (capabilities unavailable, payload unreadable, version string unparseable) —
    the gate never hides a tool on a hunch.
    """
    payload = await _capabilities(client, user_id)
    caps = _capabilities_block(payload)
    if caps is None:
        return None  # fail open — cannot tell, so do not gate

    block = caps.get(app)
    if not isinstance(block, dict):
        return (
            f"the Nextcloud '{app}' app is not installed, or is not enabled "
            "for this account"
        )
    if feature is not None:
        advertised_features = block.get("features")
        if isinstance(advertised_features, list) and feature not in advertised_features:
            return (
                f"the Nextcloud '{app}' app here does not advertise the "
                f"'{feature}' feature"
            )
        # A missing or non-list ``features`` key says nothing, so it does not
        # gate -- same fail-open rule the version check follows.

    if min_version is None:
        return None

    advertised = block.get("version")
    if not isinstance(advertised, str) or not advertised:
        return None  # app is there but says nothing about its version — fail open
    try:
        # PEP 440 ordering, so a pre-release ("1.18.0-beta.3") sorts BELOW the
        # release it precedes and stays gated out. Deliberate: betas are where a
        # feature is still moving.
        satisfied = Version(advertised) >= Version(min_version)
    except InvalidVersion:
        logger.debug("Unparseable version %r for app %s; not gating", advertised, app)
        return None
    if satisfied:
        return None
    return (
        f"it needs the Nextcloud '{app}' app >= {min_version}, "
        f"but this instance has {advertised}"
    )


def _gating_disabled() -> bool:
    return cfg_bool("MCP_DISABLE_CAPABILITY_GATING")


def _tool_gate(mcp: Any, name: str) -> tuple[str, str | None, str | None] | None:
    """The gate declared by the tool registered as ``name``, if any."""
    tool = mcp._tool_manager.get_tool(name)
    return get_required_capability(tool.fn) if tool is not None else None


async def _gate_client(mcp: Any) -> tuple[_CapabilitiesClientProtocol, str]:
    """An authenticated client (+ user id) for the request being served."""
    client = await get_client(current_context(mcp))
    return client, client.username


async def filter_by_capability(mcp: Any, tools: list) -> list:
    """Drop the tools this instance/user cannot serve (fail-open).

    Costs nothing when no listed tool is gated — the common case — and one
    (cached) OCS round-trip otherwise.
    """
    if _gating_disabled():
        return tools
    gated = [
        (tool, gate)
        for tool in tools
        if (gate := _tool_gate(mcp, tool.name)) is not None
    ]
    if not gated:
        return tools

    try:
        client, user_id = await _gate_client(mcp)
        hidden = {
            tool.name
            for tool, (app, min_version, feature) in gated
            if await unmet_capability(client, user_id, app, min_version, feature)
        }
    except Exception as exc:  # noqa: BLE001 — availability beats accuracy here
        logger.warning("Capability gating skipped (%s); listing every tool", exc)
        return tools

    if hidden:
        logger.info(
            "Hiding %d tool(s) this Nextcloud instance cannot serve: %s",
            len(hidden),
            ", ".join(sorted(hidden)),
        )
    return [tool for tool in tools if tool.name not in hidden]


async def enforce_capability(mcp: Any, name: str) -> None:
    """Refuse a gated tool the instance cannot serve.

    ``filter_by_capability`` already hides it, but a client may hold a stale tool
    list — an explicit reason beats whatever Nextcloud returns for a route its
    app version doesn't have.
    """
    if _gating_disabled():
        return
    gate = _tool_gate(mcp, name)
    if gate is None:
        return
    try:
        client, user_id = await _gate_client(mcp)
        reason = await unmet_capability(client, user_id, *gate)
    except Exception as exc:  # noqa: BLE001 — fail open, let the call proceed
        logger.warning("Capability check for %s skipped (%s)", name, exc)
        return
    if reason:
        raise ToolError(f"Tool {name} is unavailable here: {reason}.")
