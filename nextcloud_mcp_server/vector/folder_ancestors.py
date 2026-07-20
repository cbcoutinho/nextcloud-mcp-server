"""Resolve the ancestor folder file-IDs of a file's path (ADR-033 Phase 3).

The folder-scope search filter (Deck #740) matches on ``folder_ancestors`` — the
list of ancestor folder fileids stamped on every file point — via
``MatchAny(folder_ancestors, [folder_id])``. A shared folder keeps ONE canonical
Nextcloud fileid across every user who mounts it, so the ancestor chain is
user-agnostic for the shared portion: the same ``folder_id`` matches for the
owner and every reader, which is what makes the filter both correct for all
readers and flat in corpus size (unlike a per-user path string).

Resolution is a Depth-0 ``PROPFIND`` for ``oc:fileid`` per ancestor folder
(``WebDAVClient.get_fileid``), memoised via a caller-supplied ``cache`` so
repeated ancestors collapse to one lookup each. It is best-effort: a folder that
404s or errors is simply omitted from the returned list (the search then falls
back to the ``file_path`` MatchText branch for that scope), so a resolution
hiccup never aborts an index.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Protocol

logger = logging.getLogger(__name__)


class FileIdResolver(Protocol):
    """The single WebDAV method this module needs (see WebDAVClient.get_fileid)."""

    async def get_fileid(self, path: str) -> str | None: ...


def ancestor_dir_paths(file_path: str) -> list[str]:
    """Ancestor directory paths of a file, immediate-parent-first, root excluded.

    ``"/A/B/doc.pdf"`` → ``["/A/B", "/A"]``. The user's files root (``"/"``) is
    omitted: it is a per-user endpoint (each user's own home), never a shared
    scoping folder, and trivially contains everything — indexing it would bloat
    every point's ancestor list for no filtering value.
    """
    if not file_path:
        return []
    parents = [
        str(parent)
        for parent in PurePosixPath(file_path).parents
        if str(parent) not in ("/", ".", "")
    ]
    return parents


async def resolve_folder_ancestors(
    webdav: FileIdResolver,
    file_path: str,
    *,
    cache: dict[str, str | None] | None = None,
) -> list[str]:
    """Return the ancestor folder fileids of ``file_path`` (immediate-parent-first).

    Best-effort: an ancestor whose fileid cannot be resolved (404, transport
    error) is skipped, so the returned list may be shorter than the directory
    depth. ``cache`` (path → fileid|None) memoises lookups within a caller's
    scope; pass a shared dict across a batch to collapse common ancestors to one
    PROPFIND each.
    """
    if not file_path:
        return []
    if cache is None:
        cache = {}
    ancestors: list[str] = []
    for dir_path in ancestor_dir_paths(file_path):
        if dir_path in cache:
            fileid = cache[dir_path]
        else:
            try:
                fileid = await webdav.get_fileid(dir_path)
            except Exception as exc:  # noqa: BLE001 — best-effort; skip this folder
                logger.debug(
                    "Ancestor fileid resolve failed for %r (%s); skipping",
                    dir_path,
                    exc,
                )
                fileid = None
            cache[dir_path] = fileid
        if fileid:
            ancestors.append(fileid)
    return ancestors
