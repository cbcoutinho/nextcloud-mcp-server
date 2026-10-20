"""Verify-on-read access checks for semantic search results (ADR-019).

The vector index is a recall layer; Nextcloud is the source of truth for
access. This module filters search results by checking each unique document
against Nextcloud at query time, dropping any that the user can no longer
access (deleted, unshared, etc.) and lazily evicting them from the index.

Per-doc_type verifiers are registered in ``_VERIFIERS``. Each takes the
authenticated client, the (deduplicated) list of ``SearchResult``s for that
doc_type, and a shared concurrency semaphore. They return the subset of
``doc_id`` values that are currently visible to the user. Verifiers read
whatever metadata they need (e.g. deck card board/stack ids) directly from the
SearchResult — these fields are populated at index-time and propagated by
the algorithm layer (see ``search/bm25_hybrid.py`` and ``search/semantic.py``)
so verification adds zero extra Qdrant round-trips. The file verifier is the
exception: it gates results on current index-tag membership (direct, or via a
tagged folder) using one tag REPORT per index tag plus one batched
SEARCH-by-fileid, so it does not read per-result metadata.

Concurrency is bounded by a shared semaphore (default 20) so a large search
result page (or a multi-doc_type query) cannot exhaust the httpx connection
pool or trigger Nextcloud rate limiting. The 20-slot default matches the
context-expansion convention in ``server/semantic.py``.

Failure policy (notes / deck_card / news_item — the per-access verifiers):

- Definitive 403/404 from Nextcloud → drop the result and schedule eviction.
- Transient errors (5xx, network blips, unexpected exceptions) → keep the
  result and log a warning. We never silently shrink result sets due to
  flakes; the next query will re-verify.
- Unsupported doc_type (no registered verifier) → keep the result and log a
  warning. Verification is opt-in per type; a missing verifier is a soft
  failure, not a search failure.

The ``file`` verifier is the exception to the first rule: it gates on current
index-tag membership (direct or via a tagged folder), so a file is
dropped+evicted when it is not tagged, not in the user's tree (deleted or
unshared), or under an ``EXCLUDED_TAGS`` folder — not on a per-file 403/404. A failed tag
fetch still fails open. See ``_verify_files`` for the full contract.
"""

import logging
from collections.abc import Awaitable, Callable

import anyio
from anyio.abc import TaskGroup
from httpx import HTTPStatusError

from nextcloud_mcp_server.config import Settings, get_settings
from nextcloud_mcp_server.search.algorithms import (
    NextcloudClientProtocol,
    SearchResult,
)
from nextcloud_mcp_server.utils.validation import is_valid_nextcloud_doc_id
from nextcloud_mcp_server.vector.eviction import delete_document_points
from nextcloud_mcp_server.vector.mail_content import (
    list_index_window,
    mail_index_filter,
)

logger = logging.getLogger(__name__)


BatchVerifier = Callable[
    [NextcloudClientProtocol, list[SearchResult], anyio.Semaphore],
    Awaitable[set[str]],
]
"""(client, results, semaphore) -> set of doc_ids accessible to the user."""


# ---------------------------------------------------------------------------
# Per-doc-type verifiers
# ---------------------------------------------------------------------------


def _is_definitive_404_or_403(exc: BaseException) -> bool:
    """Return True if exc indicates the document is definitively inaccessible.

    401 is intentionally excluded — it usually signals expired credentials
    rather than permanent denial, so it is treated as transient (keep the
    result; the next query will re-verify after the client refreshes).
    """
    if isinstance(exc, HTTPStatusError):
        return exc.response.status_code in (403, 404)
    return False


async def _verify_notes(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    semaphore: anyio.Semaphore,
) -> set[str]:
    # safe: cooperative concurrency, no lock needed (see verify_search_results)
    accessible: set[str] = set()

    async def check(result: SearchResult) -> None:
        doc_id = result.id
        # Parse defensively before the network call so a malformed payload
        # produces a specific log line, not a generic "unexpected error"
        # from the catch-all ``except Exception`` below. ``_verify_notes``
        # is the canonical shape; ``_verify_deck_cards`` and
        # ``_verify_news_items`` mirror this hoisted-cast pattern.
        try:
            note_id_int = int(doc_id)
        except (TypeError, ValueError) as e:
            logger.warning(
                "Non-numeric note id %r: %s; keeping result",
                doc_id,
                e,
            )
            accessible.add(doc_id)
            return

        async with semaphore:
            try:
                await client.notes.get_note(note_id_int)
                accessible.add(doc_id)
            except HTTPStatusError as e:
                if _is_definitive_404_or_403(e):
                    return
                logger.warning(
                    "Transient error verifying note %s: %s %s; keeping result",
                    doc_id,
                    e.response.status_code,
                    e,
                )
                accessible.add(doc_id)
            except Exception as e:
                logger.warning(
                    "Unexpected error verifying note %s: %s; keeping result",
                    doc_id,
                    e,
                )
                accessible.add(doc_id)

    async with anyio.create_task_group() as tg:
        for r in results:
            tg.start_soon(check, r)

    return accessible


async def _tagged_index_items(
    client: NextcloudClientProtocol, settings: Settings
) -> tuple[dict[str, str], set[str]]:
    """Items carrying an index tag *directly*, from one REPORT per tag.

    Returns ``(files, folders)``: indexable directly-tagged files as
    ``{file_id: path}``, and the paths of tagged folders (whose descendants
    the scanner indexes). Unlike the scanner's discovery this never walks the
    folders, so its cost does not grow with the corpus. Both index tags count
    (``vector-index`` → hybrid, ``keyword-index`` → keyword): membership is all
    that matters here, not the mode.
    """
    indexable = settings.indexable_mime_types
    files: dict[str, str] = {}
    folders: set[str] = set()
    if not indexable:
        # Empty allowlist = index nothing (see _discover_tagged_files).
        return files, folders
    for tag_name in (settings.vector_sync_tag, settings.vector_sync_keyword_tag):
        if not tag_name:
            continue
        tag = await client.webdav.get_tag_by_name(tag_name)
        if not tag:
            continue
        for item in await client.webdav.get_files_by_tag(tag["id"]):
            path = item.get("path", "").strip("/")
            if item.get("is_directory"):
                folders.add(path)
            elif item.get("content_type", "").startswith(indexable):
                files[str(item["id"])] = path
    return files, folders


async def _verify_files(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    semaphore: anyio.Semaphore,
) -> set[str]:
    """Return the doc_ids of file results this user may currently see.

    A file is included iff it is in the user's tree (owned or shared), it
    *currently* carries an index tag — directly, or through a tagged ancestor
    folder — and it is not under an ``EXCLUDED_TAGS`` folder: parity with the
    scanner's indexing rules.

    This is the verify-on-read fix for stale tags (ADR-019): a file removed from
    both index tags (``vector-index``/``keyword-index``), moved out of a tagged
    folder, unshared or deleted is dropped and scheduled for eviction by the
    caller immediately, rather than lingering until the scanner's grace-period
    sweep reconciles it.

    Only the result ids are checked, so the cost is bounded by the result page,
    not the corpus: one REPORT per index tag lists the directly-tagged items,
    and one batched SEARCH-by-fileid (``find_files_by_ids``) locates the
    results that are not directly tagged, which are then matched against the
    tagged folders by path. (This used to re-walk every tagged folder on every
    search — tens of seconds on a large tenant, and past Astrolabe's timeout.)

    Failure policy mirrors ``_verify_news_items``: if a tag or file lookup
    fails we keep every file result (fail-open, the next query re-verifies),
    and malformed/non-numeric doc_ids are kept (defense-in-depth — producer-
    side validation is the real boundary, so false-positive is preferred over
    false-negative).
    """
    # Lazy import to break an import cycle: ``server/__init__`` imports
    # ``server.semantic`` which imports this module, so importing
    # ``server.tag_exclusion`` at module load time would re-enter a
    # partially-initialised ``server`` package depending on import order.
    from nextcloud_mcp_server.server.tag_exclusion import (  # noqa: PLC0415
        get_excluded_file_paths,
        is_path_excluded,
    )

    settings = get_settings()
    valid_ids = {r.id for r in results if is_valid_nextcloud_doc_id(r.id)}
    # Malformed ids cannot be looked up by fileid; keep them (see docstring).
    accessible = {r.id for r in results} - valid_ids
    for doc_id in accessible:
        logger.warning(
            "Malformed file doc_id %r in verifier; keeping to avoid dropping a "
            "potentially legitimate result (cannot look it up by fileid)",
            doc_id,
        )

    # One semaphore slot for the whole verification, as in _verify_news_items:
    # it caps how many *searches* verify files concurrently. The EXCLUDED_TAGS
    # lookup fans out 2×len(EXCLUDED_TAGS) WebDAV calls within it — see
    # configuration.md → "Files caveat".
    async with semaphore:
        try:
            tagged_files, tagged_folders = await _tagged_index_items(client, settings)
            # {doc_id: path} of the results the user can reach. Directly-tagged
            # files already carry their path, so only the rest — candidates for
            # folder inheritance — need the SEARCH, and none do without folders.
            visible = {i: tagged_files[i] for i in valid_ids if i in tagged_files}
            pending = sorted(int(i) for i in valid_ids - visible.keys())
            if tagged_folders and pending:
                for row in await client.webdav.find_files_by_ids(pending):
                    path = row.get("path", "")
                    if row.get("content_type", "").startswith(
                        settings.indexable_mime_types
                    ) and is_path_excluded(path, tagged_folders):
                        # is_path_excluded is the generic "path is at or under
                        # one of these folders" test; here the folders are the
                        # *tagged* ones.
                        visible[str(row["file_id"])] = path
        except Exception as e:
            logger.warning(
                "Error checking index tags for file verification: %s; keeping "
                "all file results",
                e,
            )
            return {r.id for r in results}

        # Exclusion wins: a tagged file under an EXCLUDED_TAGS folder must not
        # surface, matching the scanner's defense-in-depth filter. A failure
        # here degrades to "no exclusion" rather than dropping legitimate hits.
        # Skipped when nothing survived: it could not change the outcome.
        excluded_paths: set[str] = set()
        if visible:
            try:
                excluded_paths = await get_excluded_file_paths(client.webdav)
            except Exception as e:
                logger.warning(
                    "EXCLUDED_TAGS lookup failed during verification (%s); "
                    "proceeding without exclusion filter",
                    e,
                )

    # A valid id absent from `visible` is untagged/deleted/unshared — drop it
    # and let the caller schedule eviction.
    accessible.update(
        doc_id
        for doc_id, path in visible.items()
        if not is_path_excluded(path, excluded_paths)
    )
    return accessible


async def _verify_deck_cards(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    semaphore: anyio.Semaphore,
) -> set[str]:
    # safe: cooperative concurrency, no lock needed (see verify_search_results)
    accessible: set[str] = set()

    async def check(result: SearchResult) -> None:
        doc_id = result.id
        # board_id and stack_id are propagated from the Qdrant payload by the
        # algorithm layer. No extra Qdrant round-trip.
        meta = result.metadata or {}
        board_id = meta.get("board_id")
        stack_id = meta.get("stack_id")
        if board_id is None or stack_id is None:
            # Without metadata we cannot run the cheap fast-path. Per ADR-019
            # we deliberately do NOT fall back to O(boards × stacks) iteration
            # in the search hot path; treat as accessible.
            logger.warning(
                "Incomplete deck metadata for card %s (board_id=%s, stack_id=%s); "
                "keeping result (verification skipped, legacy data)",
                doc_id,
                board_id,
                stack_id,
            )
            accessible.add(doc_id)
            return

        # Parse defensively before the network call so a malformed payload
        # produces a specific log line, not a generic "unexpected error"
        # from the catch-all ``except Exception`` below. Mirrors the
        # canonical hoisted-cast pattern in ``_verify_notes``.
        try:
            board_id_int = int(board_id)
            stack_id_int = int(stack_id)
            card_id_int = int(doc_id)
        except (TypeError, ValueError) as e:
            logger.warning(
                "Non-numeric deck metadata for card %s "
                "(board_id=%r, stack_id=%r): %s; keeping result",
                doc_id,
                board_id,
                stack_id,
                e,
            )
            accessible.add(doc_id)
            return

        async with semaphore:
            try:
                await client.deck.get_card(
                    board_id=board_id_int,
                    stack_id=stack_id_int,
                    card_id=card_id_int,
                )
                accessible.add(doc_id)
            except HTTPStatusError as e:
                if _is_definitive_404_or_403(e):
                    return
                logger.warning(
                    "Transient error verifying deck card %s: %s %s; keeping result",
                    doc_id,
                    e.response.status_code,
                    e,
                )
                accessible.add(doc_id)
            except Exception as e:
                logger.warning(
                    "Unexpected error verifying deck card %s: %s; keeping result",
                    doc_id,
                    e,
                )
                accessible.add(doc_id)

    async with anyio.create_task_group() as tg:
        for r in results:
            tg.start_soon(check, r)

    return accessible


async def _verify_news_items(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    semaphore: anyio.Semaphore,
) -> set[str]:
    """Batch-verify news items with a single fetch.

    The Nextcloud News API has no per-item endpoint, so ``news.get_item`` is
    implemented as a fetch-all + filter — which would be O(N × all_items) if
    called per id. Instead we fetch once and intersect. Only one slot of
    the shared semaphore is consumed per search (rather than one per id),
    but that slot is held for the full ``get_items`` round-trip; see the
    in-body comment for the backpressure rationale.
    """
    doc_ids = [r.id for r in results]

    # Semaphore lifetime: this slot is held for the duration of ONE
    # deduplicated News fetch (≤1 per search), not per-id. That is the
    # correct backpressure behaviour — a single user's news verification
    # must not hammer Nextcloud with concurrent fetch-all requests, and
    # any other verifiers running in parallel for the same search share
    # the same semaphore. Latency of this fetch is proportional to the
    # user's full news corpus; see the News caveat in
    # docs/configuration.md for production guidance.
    #
    # Multi-user note: the "≤1 per search" bound is per-search, not
    # per-process. If N users simultaneously search news content, all N
    # hold a slot for the duration of their respective fetches, each
    # consuming 1/max_concurrent of the shared verification budget. A
    # single news-heavy user can therefore hold their slot for seconds.
    async with semaphore:
        try:
            # TODO(perf): if profiling shows this fetch dominates query latency
            # for news-heavy users, cache the per-request item set or push for
            # a per-item News API endpoint. The shared semaphore protects
            # against runaway concurrent fetches, but the payload itself can
            # be large (News auto-purge cap is in the thousands of items).
            #
            # NOTE: ``batch_size`` is intentionally unbounded (-1). A numeric
            # ceiling here would silently *break correctness*: any item beyond
            # the cap would be missing from ``present_ids`` and incorrectly
            # dropped from the result set. The fail-open contract requires
            # fetching every item the user has access to. See the news caveat
            # in docs/configuration.md (Verify-on-Read) for the latency
            # tradeoff and follow-up paths.
            news_fetch_start = anyio.current_time()
            items = await client.news.get_items(batch_size=-1, get_read=True)
            logger.debug(
                "News fetch for verification took %.2fs (%d item(s) returned)",
                anyio.current_time() - news_fetch_start,
                len(items),
            )
        except HTTPStatusError as e:
            # If the News API itself is gone (app disabled, user lost access),
            # treat *all* requested items as inaccessible. Eviction will reclaim.
            if _is_definitive_404_or_403(e):
                # News app commonly disabled/uninstalled — debug-level keeps
                # this off operator dashboards; transient errors below stay
                # at warning because they're unexpected.
                logger.debug(
                    "News API returned %s for user %s; treating all %d news_items as inaccessible",
                    e.response.status_code,
                    client.username,
                    len(doc_ids),
                )
                return set()
            logger.warning(
                "Transient error fetching news items for verification: %s %s; keeping all results",
                e.response.status_code,
                e,
            )
            return set(doc_ids)
        except Exception as e:
            logger.warning(
                "Unexpected error fetching news items for verification: %s; keeping all results",
                e,
            )
            return set(doc_ids)

    # Build present_ids from the API response. Granularity is intentionally
    # asymmetric with the per-item loop below:
    #
    #   * Here (structural failure): if the API response itself is corrupt
    #     — even one item with a non-numeric id — we cannot reliably build
    #     `present_ids`, so every requested doc_id fails open. The batch
    #     is the only safe blast radius when the source-of-truth payload
    #     can't be trusted.
    #   * Below (data failure): a single non-numeric *stored* doc_id is a
    #     local data issue. Failing the whole batch open would let one bad
    #     row in Qdrant mask real revocations for every other item, so we
    #     scope the fail-open to that one id.
    try:
        present_ids = {
            int(item.get("id")) for item in items if item.get("id") is not None
        }
    except (TypeError, ValueError) as e:
        logger.warning(
            "Non-numeric id in news API response (sample=%r): %s; keeping all results",
            items[:3] if items else items,
            e,
        )
        return set(doc_ids)

    # Per-item check: a single non-numeric *stored* doc_id is fail-open
    # for THAT item only — not the whole batch. Mirrors the per-item
    # shape of the notes/files/deck verifiers. See the granularity note
    # above for why this is narrower than the API-response failure path.
    accessible: set[str] = set()
    for d in doc_ids:
        # SearchResult.id is always str (Qdrant payload doc_id is keyword-
        # indexed; producers stringify on write). Pass through verbatim.
        if not is_valid_nextcloud_doc_id(d):
            # The news API has no per-item endpoint, so a malformed doc_id
            # cannot be verified against the source of truth. Err toward
            # false-positive (keep in results) over false-negative (drop a
            # potentially legitimate result) — matches the same conservative
            # posture _verify_notes and _verify_deck_cards take for
            # non-numeric IDs. The producer-side validation is the real
            # security boundary; the verifier is defence-in-depth.
            logger.warning(
                "Malformed news_item doc_id %r in verifier; keeping to "
                "avoid dropping a potentially legitimate result (news API "
                "has no per-item endpoint, so cannot verify against source "
                "of truth — false-positive preferred over false-negative)",
                d,
            )
            accessible.add(d)
            continue
        try:
            if int(d) in present_ids:
                accessible.add(d)
        except (TypeError, ValueError):
            logger.debug("Non-numeric news doc_id %r; keeping (cannot verify)", d)
            accessible.add(d)
    return accessible


async def _verify_mail_messages(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    semaphore: anyio.Semaphore,
) -> set[str]:
    """Verify mail messages with one DB-cached list per mailbox, then intersect.

    ``mail.get_message`` triggers a server-side IMAP body fetch, so a per-result
    verify would issue one IMAP FETCH per hit — multiple seconds for an active
    inbox. Instead we batch by ``mailbox_id`` (propagated into ``result.metadata``
    from the Qdrant payload) and call ``list_index_window`` once per mailbox,
    which reads the Mail app's DB cache (no IMAP). A message is accessible iff it
    is present in that listing — by construction the *same* window the scanner
    indexes, so anything aged out of the window is being evicted anyway.

    When ``MAIL_INDEX_TAG`` is set the listing carries the same tag filter the
    scanner indexed with, so untagging a message drops it from search on the
    next query (and evicts it) — the mail equivalent of untagging a file.

    Failure policy mirrors ``_verify_news_items``: a definitive 403/404 for a
    mailbox drops all its results (eviction reclaims); transient errors keep
    them (fail-open). Results with no/!numeric ``mailbox_id`` or a non-numeric
    ``doc_id`` are kept (fail-open; verification can't batch them), as are all
    results when the tag filter itself can't be resolved.
    """
    # safe: cooperative concurrency, no lock needed (see verify_search_results)
    accessible: set[str] = set()

    # Partition results by mailbox so each mailbox is listed exactly once.
    by_mailbox: dict[int, list[SearchResult]] = {}
    for r in results:
        mailbox_id = (r.metadata or {}).get("mailbox_id")
        # No usable mailbox_id (legacy payload) — can't batch via the DB cache
        # without an IMAP-triggering per-id fetch, so keep it (fail-open).
        if mailbox_id is None:
            logger.warning(
                "Mail result %s has no mailbox_id; keeping (batch verification "
                "skipped)",
                r.id,
            )
            accessible.add(r.id)
            continue
        try:
            mailbox_int = int(mailbox_id)
        except (TypeError, ValueError):
            logger.warning(
                "Mail result %s has non-numeric mailbox_id %r; keeping "
                "(batch verification skipped)",
                r.id,
                mailbox_id,
            )
            accessible.add(r.id)
            continue
        by_mailbox.setdefault(mailbox_int, []).append(r)

    # Resolve the include-tag filter once, before the fan-out, so every mailbox
    # is checked against the same window the scanner indexed. If it can't be
    # resolved, keep every result unverified rather than listing unfiltered:
    # unfiltered is a *narrower* window for old tagged mail, so falling back
    # would drop and hard-evict messages that are legitimately indexed.
    try:
        index_filter = await mail_index_filter(client.mail)
    except Exception as e:
        logger.warning(
            "Could not resolve the mail index filter (%s); keeping all %d mail "
            "result(s) unverified this query",
            e,
            len(results),
        )
        return {r.id for r in results}

    async def check_mailbox(mailbox_id: int, mb_results: list[SearchResult]) -> None:
        async with semaphore:
            try:
                messages = await list_index_window(
                    client.mail, mailbox_id, index_filter
                )
            except HTTPStatusError as e:
                if _is_definitive_404_or_403(e):
                    # Mailbox/account gone — all its results are inaccessible.
                    return
                logger.warning(
                    "Transient error listing mailbox %s for verification: %s %s; "
                    "keeping its %d result(s)",
                    mailbox_id,
                    e.response.status_code,
                    e,
                    len(mb_results),
                )
                for r in mb_results:
                    accessible.add(r.id)
                return
            except Exception as e:
                logger.warning(
                    "Unexpected error listing mailbox %s for verification: %s; "
                    "keeping its %d result(s)",
                    mailbox_id,
                    e,
                    len(mb_results),
                )
                for r in mb_results:
                    accessible.add(r.id)
                return

        present_ids = {
            str(m.get("databaseId"))
            for m in messages
            if m.get("databaseId") is not None
        }
        for r in mb_results:
            if r.id in present_ids:
                accessible.add(r.id)
            elif not is_valid_nextcloud_doc_id(r.id):
                # Malformed stored id can't match the numeric listing; keep it
                # (fail-open) rather than drop a possibly-legitimate result —
                # mirrors the notes/news posture.
                logger.warning(
                    "Malformed mail_message doc_id %r in verifier; keeping",
                    r.id,
                )
                accessible.add(r.id)
            # else: genuinely absent (deleted or aged out) -> drop + evict.

    async with anyio.create_task_group() as tg:
        for mailbox_id, mb_results in by_mailbox.items():
            tg.start_soon(check_mailbox, mailbox_id, mb_results)

    return accessible


_VERIFIERS: dict[str, BatchVerifier] = {
    "note": _verify_notes,
    "file": _verify_files,
    "deck_card": _verify_deck_cards,
    "news_item": _verify_news_items,
    "mail_message": _verify_mail_messages,
}


def get_supported_doc_types() -> set[str]:
    """Return the set of doc_types that have registered verifiers.

    Used by CI guards and tests to ensure every indexed doc_type has a
    verifier (see ADR-019 implementation checklist).
    """
    return set(_VERIFIERS.keys())


async def _apply_user_display_paths(user_id: str, results: list[SearchResult]) -> None:
    """Override file results' path/title with the querying user's own mount path.

    ADR-033 Phase 2: a shared file's Qdrant scalar ``file_path`` is pinned to the
    owner (Phase 1), so a non-owner reader would otherwise see the *owner's* path.
    Here we look up each returned file's per-user path in the ``document_paths``
    store and substitute it into the result the reader receives.

    Best-effort and non-security: on any store error, or for a file with no stored
    row (legacy / not-yet-scanned-by-this-user), the Qdrant scalar already carried
    on the result stands unchanged. Mutates ``results`` in place (SearchResult is a
    mutable dataclass). Lazy imports keep the vector/DB layers off this module's
    import path.
    """
    file_ids = [r.id for r in results if r.doc_type == "file"]
    if not file_ids:
        return
    try:
        from nextcloud_mcp_server.vector.document_path_store import (  # noqa: PLC0415
            DocumentPathStore,
        )
        from nextcloud_mcp_server.vector.sharing_state import (  # noqa: PLC0415
            file_title_from_path,
        )

        store = await DocumentPathStore.shared()
        paths = await store.get_paths_for_user(user_id, "file", file_ids)
    except Exception as exc:  # noqa: BLE001 — display-only; fall back to the scalar
        logger.debug(
            "Per-user display-path override skipped (%s); using stored scalar", exc
        )
        return
    if not paths:
        return
    for r in results:
        if r.doc_type != "file":
            continue
        path = paths.get(r.id)
        if not path:
            continue
        if r.metadata is None:
            r.metadata = {}
        r.metadata["path"] = path
        r.title = file_title_from_path(path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def verify_search_results(
    client: NextcloudClientProtocol,
    results: list[SearchResult],
    *,
    evict_on_missing: bool = True,
    max_concurrent: int | None = None,
    eviction_task_group: TaskGroup | None = None,
) -> tuple[list[SearchResult], int]:
    """Filter search results to those the user can currently access.

    Deduplicates by ``(doc_id, doc_type)`` before verifying, so multiple
    chunks from the same document cost a single check. Verifiers run
    concurrently per doc_type and concurrently per id within each verifier,
    bounded by a shared semaphore (``max_concurrent``).

    When ``evict_on_missing=True``, points for documents that fail verification
    are deleted from Qdrant. If ``eviction_task_group`` is provided (the
    lifespan-owned task group from ``app.py::VectorSyncState``), eviction is
    fire-and-forget — the search response returns immediately and Qdrant
    deletes happen in the background. If no task group is provided (unit
    tests, modes without vector sync), eviction falls back to running inline
    in a local task group. Eviction failures are logged but never propagated.

    Args:
        client: Authenticated NextcloudClient (must expose ``username``).
        results: SearchResult list from the algorithm layer (may include
            multiple chunks per document).
        evict_on_missing: Schedule lazy eviction for inaccessible docs.
        max_concurrent: Cap on concurrent verification round-trips against
            Nextcloud. When ``None`` (the default), resolved from
            ``Settings.verification_concurrency`` (env var
            ``VERIFICATION_CONCURRENCY``, default 20).
        eviction_task_group: Optional long-lived task group on which to
            spawn fire-and-forget eviction. Pass
            ``ctx.request_context.lifespan_context.eviction_task_group``
            from MCPServer tools.

    Returns:
        Tuple of ``(kept_results, dropped_count)`` where ``kept_results`` is
        the filtered list preserving the original order and ``dropped_count``
        is the number of unique ``(doc_id, doc_type)`` pairs that failed
        verification (ghost records).
    """
    if not results:
        return results, 0

    user_id: str = client.username

    if max_concurrent is None:
        max_concurrent = get_settings().verification_concurrency

    # Group unique (doc_id, doc_type) by doc_type so each verifier sees a
    # deduplicated batch. We pick one SearchResult per (id, doc_type) to carry
    # metadata (path, board_id/stack_id) into the verifier — chunks of the
    # same document share these fields, so any chunk works.
    by_type: dict[str, dict[str, SearchResult]] = {}
    for r in results:
        by_type.setdefault(r.doc_type, {}).setdefault(r.id, r)

    # Shared semaphore bounds total Nextcloud round-trips across all
    # per-id verifiers. Without it, a 50-result mostly-notes page could fan
    # out 50 concurrent get_note calls and exhaust the connection pool.
    semaphore = anyio.Semaphore(max_concurrent)

    # Concurrency note: ``accessible_by_type`` is mutated by multiple
    # ``run_verifier`` tasks running under the task group below. This is
    # safe without an explicit lock because (a) anyio uses cooperative
    # multitasking — a task only yields at ``await`` points, never
    # mid-statement; (b) each task is dispatched once per ``doc_type``
    # by the loop ``for doc_type, ... in by_type.items()`` further down,
    # so two tasks never write to the same key; and (c) Python dict key
    # assignment is not an await point, so two tasks cannot race on the
    # same write. Adding a lock would be dead weight; using ``anyio.Lock``
    # here would force serialization on a path that is intentionally
    # parallel.
    accessible_by_type: dict[str, set[str]] = {}

    async def run_verifier(doc_type: str, unique_results: list[SearchResult]) -> None:
        verifier = _VERIFIERS.get(doc_type)
        if verifier is None:
            logger.warning(
                "No verifier registered for doc_type=%r; keeping %d result(s) unverified",
                doc_type,
                len(unique_results),
            )
            accessible_by_type[doc_type] = {r.id for r in unique_results}
            return
        try:
            accessible_by_type[doc_type] = await verifier(
                client, unique_results, semaphore
            )
        except Exception as e:
            # Verifier itself blew up (not per-id) — fail open.
            logger.error(
                "Verifier for doc_type=%s raised: %s; keeping all %d result(s) unverified",
                doc_type,
                e,
                len(unique_results),
            )
            accessible_by_type[doc_type] = {r.id for r in unique_results}

    async with anyio.create_task_group() as tg:
        for doc_type, id_to_result in by_type.items():
            tg.start_soon(run_verifier, doc_type, list(id_to_result.values()))

    # Compute (doc_id, doc_type) pairs that failed verification
    inaccessible: set[tuple[str, str]] = set()
    for doc_type, id_to_result in by_type.items():
        # The .get() default is defensive only — run_verifier always populates
        # accessible_by_type[doc_type], either with the verifier's result or
        # with all ids on verifier crash (fail-open).
        accessible = accessible_by_type.get(doc_type, set(id_to_result.keys()))
        for doc_id in id_to_result.keys():
            if doc_id not in accessible:
                inaccessible.add((doc_id, doc_type))

    if inaccessible:
        logger.info(
            "Verification dropped %d inaccessible document(s): %s",
            len(inaccessible),
            sorted(inaccessible),
        )

    # Filter results, preserving order. All chunks of an inaccessible document
    # are dropped together (dedup happened before verification, but the result
    # list still contains all chunks).
    kept = [r for r in results if (r.id, r.doc_type) not in inaccessible]

    # Lazy eviction.
    #
    # Preferred path: spawn evict() on the lifespan-owned task group via
    # `start_soon`, which returns immediately — the search response is not
    # blocked on Qdrant deletes. If the server is shutting down, the task
    # group is cleared back to None (see app.py) and we fall through to the
    # inline path. Cancellation mid-eviction is fine: the next query will
    # re-verify and re-attempt (self-healing per ADR-019).
    #
    # Fallback path: when no task group is supplied (unit tests, deployment
    # modes without vector sync), run eviction inline in a local task group.
    # This preserves prior behaviour for tests that rely on eviction being
    # complete by the time `verify_search_results` returns.
    if evict_on_missing and inaccessible:

        async def evict(doc_id: str, doc_type: str) -> None:
            # Eviction is scoped to the QUERYING user's own points
            # (user_id == the searcher). For a cross-user shared document
            # (owner_id=alice surfaced to bob via accessible_owners), bob
            # failing verification evicts with user_id=bob — a deliberate
            # no-op, because alice's points carry user_id=alice and must NOT
            # be deleted just because bob's share was revoked. Bob's view
            # self-heals via list_accessible_owners (alice drops out of his
            # accessible owners once OCS no longer reports the share). See the
            # legacy-user_id semantics note in build_ownership_filter.
            try:
                await delete_document_points(doc_id, doc_type, user_id)
            except Exception as e:
                logger.warning(
                    "Failed to evict %s_%s from Qdrant: %s", doc_type, doc_id, e
                )

        if eviction_task_group is not None:
            for doc_id, doc_type in inaccessible:
                # Guard against the lifespan task group having exited between
                # the getattr() capture in server/semantic.py and this call —
                # start_soon raises RuntimeError on a closed group, which
                # would otherwise surface as a search error. Eviction is
                # best-effort: the next query re-verifies and re-attempts.
                try:
                    eviction_task_group.start_soon(evict, doc_id, doc_type)
                except RuntimeError:
                    logger.debug("Eviction task group closed; will retry on next query")
        else:
            async with anyio.create_task_group() as tg:
                for doc_id, doc_type in inaccessible:
                    tg.start_soon(evict, doc_id, doc_type)

    # ADR-033 Phase 2: substitute each returned file's per-user display path
    # (the owner-pinned Qdrant scalar would otherwise show the owner's path to a
    # non-owner reader). Best-effort — a store miss leaves the scalar in place.
    await _apply_user_display_paths(user_id, kept)

    return kept, len(inaccessible)
