"""Tag-based file exclusion for MCP file operations (issue #710).

Resolves the configured ``EXCLUDED_TAGS`` to a set of file paths that
should be hidden from WebDAV MCP tools (list, read, search) and rejected
by mutating tools (write, delete, move, copy).

The flow per call:

1. Parse ``EXCLUDED_TAGS`` (comma-separated tag names) from config.
2. For each tag name, resolve to a tag ID via ``get_tag_by_name``.
3. For each tag ID, fetch all tagged file/folder paths via
   ``get_files_by_tag``.
4. Collect normalised paths into a single ``set[str]``.

Tagging a *folder* excludes the folder itself and every descendant via
prefix match in :func:`is_path_excluded`.

Threat model: this is a defence-in-depth control to prevent accidental
exfiltration via the LLM tool surface. A user controlling the Nextcloud
account whose credentials the server uses can untag files unless the tag
is created with ``user_assignable=false``.
"""

import logging

import anyio

from nextcloud_mcp_server.client.webdav import WebDAVClient
from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)


def get_excluded_tag_names() -> list[str]:
    """Return the configured excluded tag names (empty list if disabled)."""
    raw = get_settings().excluded_tags
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


async def _resolve_one_tag(
    tag_name: str,
    webdav: WebDAVClient,
    results: list[set[str]],
) -> None:
    """Resolve a single tag's paths and append them as a set to *results*.

    Each task appends its own set to the shared list; ``list.append`` is
    atomic between cooperative yields under anyio (single-threaded
    between awaits) so no explicit lock is needed. Swallows its own
    exceptions so a failure for one tag does not abort the surrounding
    task group (preserves fail-open per-tag semantics).
    """
    try:
        tag = await webdav.get_tag_by_name(tag_name)
    except Exception as e:
        logger.warning(
            "Tag exclusion lookup failed for tag %r (%s); "
            "skipping — files tagged with this tag will be visible",
            tag_name,
            e,
        )
        return

    if tag is None:
        logger.debug("Excluded tag %r does not exist — skipping", tag_name)
        return

    if tag.get("id") is None:
        # Malformed PROPFIND response: <oc:systemtag> entry without
        # <oc:id/>. Skip rather than dispatch <oc:systemtag>None</oc:systemtag>.
        logger.debug(
            "Excluded tag %r has no id in PROPFIND response — skipping",
            tag_name,
        )
        return

    try:
        files = await webdav.get_files_by_tag(tag["id"])
    except Exception as e:
        logger.warning(
            "Tag exclusion file enumeration failed for tag %r (%s); "
            "skipping — files tagged with this tag will be visible",
            tag_name,
            e,
        )
        return

    paths: set[str] = set()
    for f in files:
        path = _normalise_path(f["path"])
        paths.add(path)
        if f.get("is_directory"):
            logger.debug(
                "Excluding directory %r (tag %r) — descendants will be hidden",
                path,
                tag_name,
            )
    results.append(paths)


async def get_excluded_file_paths(webdav: WebDAVClient) -> set[str]:
    """Resolve excluded tags to the set of paths they cover.

    Tagged directories are added as their own normalised path; descendants
    are blocked via prefix match in :func:`is_path_excluded`.

    Per-tag resolution is fanned out via ``anyio.create_task_group`` so
    that the 2N network calls (1 PROPFIND + 1 REPORT per tag) run
    concurrently rather than serially. No lock is needed: each task
    appends its own ``set`` to a shared list, and append is atomic
    between awaits under anyio's cooperative single-threaded model.

    **Failure mode is fail-open per tag**: if the systemtags endpoint is
    unreachable or returns an error for a given tag, that tag is skipped
    with a warning rather than propagating the error. Reasoning: the
    threat model is preventing *accidental* exfiltration via the LLM tool
    surface; a Nextcloud-side outage of the systemtags API should not
    take down all WebDAV tools. Operators relying on this for stronger
    guarantees should monitor the warning logs.
    """
    tag_names = get_excluded_tag_names()
    if not tag_names:
        return set()

    results: list[set[str]] = []
    async with anyio.create_task_group() as tg:
        for tag_name in tag_names:
            tg.start_soon(_resolve_one_tag, tag_name, webdav, results)

    excluded: set[str] = set().union(*results)
    if excluded:
        # `len(excluded)` counts directly-tagged entries — descendants of
        # tagged directories are hidden too but resolved at check time.
        logger.info(
            "Tag-based exclusion resolved to %d directly-tagged path(s) "
            "for tags: %s (descendants of tagged directories also hidden)",
            len(excluded),
            ", ".join(tag_names),
        )

    return excluded


def is_path_excluded(path: str, excluded_paths: set[str]) -> bool:
    """Return True if *path* (or any of its parents) is excluded.

    A path is excluded when it matches an entry exactly, or when an
    excluded entry is one of its directory ancestors (prefix match on
    ``<dir>/``).
    """
    if not excluded_paths:
        return False
    normalised = _normalise_path(path)
    if normalised in excluded_paths:
        return True
    for exc in excluded_paths:
        if normalised.startswith(exc + "/"):
            return True
    return False


def _normalise_path(path: str) -> str:
    """Strip leading/trailing slashes for consistent comparison."""
    return path.strip("/")
