"""Deep links into the Astrolabe Nextcloud app's UI.

The Astrolabe frontend opens its chunk viewer from query parameters on the app
root (``src/App.vue``'s ``handleUrlParameters``), and strips them via
``history.replaceState`` once the viewer is open. This module is the only place
that *builds* such a link; the parser has existed for far longer than any
generator.
"""

import logging
from urllib.parse import urlencode

from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)

# Astrolabe's app root. The chunk viewer is opened by query parameters on it —
# the app has no vue-router, so there is no per-document path to link to.
ASTROLABE_APP_PATH = "/index.php/apps/astrolabe/"


def astrolabe_browser_base() -> str | None:
    """Return the browser-reachable Nextcloud base URL, or None.

    Uses ``nextcloud_browser_url`` (``nextcloud_public_url`` →
    ``nextcloud_public_issuer_url`` → ``nextcloud_host``) so links point at
    Nextcloud even in external-IdP deployments where the OAuth issuer URL is the
    IdP rather than Nextcloud.

    Returns None when nothing is configured, and when the configured base URL
    lacks an http:// or https:// scheme — a bare ``internal:8080`` would
    otherwise yield a non-clickable link, so the misconfiguration is surfaced as
    a warning and callers omit the link instead.
    """
    base = (get_settings().nextcloud_browser_url or "").strip()
    if not base:
        return None
    if not base.startswith(("http://", "https://")):
        logger.warning(
            "Cannot build an Astrolabe URL: configured Nextcloud base URL %r is "
            "missing an http:// or https:// scheme.",
            base,
        )
        return None
    return base.rstrip("/")


def chunk_url(
    base: str | None,
    *,
    doc_type: str,
    doc_id: int | str,
    chunk_start: int | None,
    chunk_end: int | None,
    title: str | None = None,
    path: str | None = None,
    page_number: int | None = None,
    chunk_index: int | None = None,
    total_chunks: int | None = None,
    extra: dict[str, str] | None = None,
) -> str | None:
    """Build a link that opens ``doc_id``'s chunk in the Astrolabe chunk viewer.

    ``base`` comes from :func:`astrolabe_browser_base`; it is a parameter rather
    than an internal call so a search resolves it once instead of once per
    result.

    Returns None when there is no base URL, or when either chunk offset is
    missing — Astrolabe requires ``doc_type``, ``doc_id``, ``chunk_start`` and
    ``chunk_end`` together, and opens nothing if any is absent, so a link
    without them would be a dead end rather than a degraded one.

    ``extra`` carries per-doc_type access-recheck identifiers (``board_id``,
    ``mailbox_id``, ``calendar_uri``) so a stale link gets the same local access
    check as a live search result.
    """
    if not base or chunk_start is None or chunk_end is None:
        return None

    params: dict[str, str] = {
        "doc_type": doc_type,
        "doc_id": str(doc_id),
        "chunk_start": str(chunk_start),
        "chunk_end": str(chunk_end),
    }
    optional = {
        "title": title,
        "path": path,
        "page_number": page_number,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        **(extra or {}),
    }
    # Omit unset values rather than emitting `page_number=None`, which Astrolabe
    # would parseInt into NaN.
    params.update({k: str(v) for k, v in optional.items() if v is not None})

    # urlencode quotes the spaces and slashes that real titles and paths carry.
    return f"{base}{ASTROLABE_APP_PATH}?{urlencode(params)}"
