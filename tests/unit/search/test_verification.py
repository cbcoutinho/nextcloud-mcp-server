"""Unit tests for verify-on-read (ADR-019)."""

from types import SimpleNamespace

import anyio
import httpx
import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.search import verification
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.verification import (
    _verify_deck_cards,
    _verify_files,
    _verify_mail_messages,
    _verify_news_items,
    _verify_notes,
    get_supported_doc_types,
    verify_search_results,
)
from nextcloud_mcp_server.vector.mail_content import MAIL_SCAN_MAX_PER_MAILBOX
from nextcloud_mcp_server.vector.scanner import INDEXED_DOC_TYPES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sem(slots: int = 20) -> anyio.Semaphore:
    return anyio.Semaphore(slots)


def _make_result(
    doc_id: int | str,
    doc_type: str = "note",
    chunk_index: int = 0,
    score: float = 0.9,
    metadata: dict | None = None,
) -> SearchResult:
    # Mirror the producer-side stringification (scanner writes str(note["id"])
    # etc. into Qdrant payloads). Tests pass int literals for readability;
    # the SearchResult contract is ``id: str``.
    return SearchResult(
        id=str(doc_id),
        doc_type=doc_type,
        title=f"{doc_type}_{doc_id}",
        excerpt="...",
        score=score,
        chunk_index=chunk_index,
        metadata=metadata,
    )


def _http_error(status_code: int) -> HTTPStatusError:
    request = httpx.Request("GET", "http://test.local/x")
    response = httpx.Response(status_code=status_code, request=request)
    return HTTPStatusError(f"{status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_supported_doc_types_covers_indexed_types():
    """ADR-019 CI guard: every doc_type indexed by the scanner has a verifier.

    `INDEXED_DOC_TYPES` is the single source of truth in `vector/scanner.py`;
    this test fails if a new indexed type is added without a registered
    verifier in `search/verification.py`.
    """
    assert get_supported_doc_types() >= INDEXED_DOC_TYPES


# ---------------------------------------------------------------------------
# Note verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_notes_200_keeps_all(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(return_value={"id": 1, "content": "x"})
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(
        client, [_make_result(1), _make_result(2), _make_result(3)], _sem()
    )

    assert result == {"1", "2", "3"}
    assert notes_client.get_note.await_count == 3


@pytest.mark.unit
async def test_verify_notes_404_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_403_drops(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == set()


@pytest.mark.unit
async def test_verify_notes_transient_5xx_keeps(mocker):
    """Transient errors must NOT silently shrink results."""
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(503))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == {"42"}


@pytest.mark.unit
async def test_verify_notes_429_keeps_as_transient(mocker):
    """HTTP 429 (rate-limit) is transient, NOT a definitive 403/404 drop.

    Locks in that ``_is_definitive_404_or_403`` returns False for 429 so a
    future refactor cannot accidentally treat rate-limit responses as
    permanent revocations and shrink result pages on every Nextcloud hiccup.
    """
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=_http_error(429))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(42)], _sem())

    assert result == {"42"}


@pytest.mark.unit
async def test_verify_notes_unexpected_exception_keeps(mocker):
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=RuntimeError("boom"))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result(7)], _sem())

    assert result == {"7"}


@pytest.mark.unit
async def test_verify_notes_non_numeric_id_keeps(mocker):
    """Non-numeric note id must not surface as a generic 'unexpected error'.

    The defensive int() guard runs before the network call and produces a
    type-specific log line; result is kept (fail-open).
    """
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result("not-a-number")], _sem())

    assert result == {"not-a-number"}
    notes_client.get_note.assert_not_awaited()


@pytest.mark.unit
async def test_verify_notes_mixed_outcomes(mocker):
    """Mix of accessible, deleted, and transient — only deleted is dropped."""

    async def side_effect(note_id):
        if note_id == 1:
            return {"id": 1}
        if note_id == 2:
            raise _http_error(404)  # deleted
        if note_id == 3:
            raise _http_error(500)  # transient → keep
        raise AssertionError(f"unexpected id {note_id}")

    notes_client = SimpleNamespace(get_note=mocker.AsyncMock(side_effect=side_effect))
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(
        client, [_make_result(1), _make_result(2), _make_result(3)], _sem()
    )

    assert result == {"1", "3"}


@pytest.mark.unit
async def test_verify_notes_string_doc_id_matches_production(mocker):
    """Notes are stored with string doc_ids in production (scanner.py:241).

    The verifier must parse the string to int for the API call but
    preserve the original string in the accessible set so eviction
    receives the same type that was indexed in Qdrant. Without this
    contract, a `MatchValue(value=42)` eviction filter would not match
    a payload stored as `"42"`.
    """
    notes_client = SimpleNamespace(
        get_note=mocker.AsyncMock(return_value={"id": 42, "content": "x"})
    )
    client = SimpleNamespace(notes=notes_client, username="alice")

    result = await _verify_notes(client, [_make_result("42", doc_type="note")], _sem())

    # Original string id is preserved (not coerced to int 42).
    assert result == {"42"}
    # The API call still uses the int form internally.
    notes_client.get_note.assert_awaited_once_with(42)


# ---------------------------------------------------------------------------
# Mail verifier (per-id, mirrors the note verifier)
# ---------------------------------------------------------------------------


def _mail_result(doc_id, mailbox_id=10):
    """A mail_message SearchResult carrying mailbox_id in its metadata."""
    return _make_result(
        doc_id, doc_type="mail_message", metadata={"mailbox_id": mailbox_id}
    )


@pytest.mark.unit
async def test_verify_mail_batches_one_list_per_mailbox(mocker):
    """The verifier lists each mailbox once (DB cache, not per-message IMAP)."""
    list_messages = mocker.AsyncMock(
        return_value=[{"databaseId": 10}, {"databaseId": 30}]
    )
    mail_client = SimpleNamespace(list_messages=list_messages)
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(
        client,
        [_mail_result(10), _mail_result(20), _mail_result(30)],  # all mailbox 10
        _sem(),
    )
    # 10 and 30 present; 20 absent (deleted/aged out) -> dropped.
    assert result == {"10", "30"}
    # One DB-cached list call for the single mailbox, not one per result.
    list_messages.assert_awaited_once_with(10, limit=MAIL_SCAN_MAX_PER_MAILBOX)


@pytest.mark.unit
async def test_verify_mail_404_drops_mailbox(mocker):
    mail_client = SimpleNamespace(
        list_messages=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(client, [_mail_result(42)], _sem())
    assert result == set()


@pytest.mark.unit
async def test_verify_mail_403_drops_mailbox(mocker):
    mail_client = SimpleNamespace(
        list_messages=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(client, [_mail_result(42)], _sem())
    assert result == set()


@pytest.mark.unit
async def test_verify_mail_transient_5xx_keeps(mocker):
    mail_client = SimpleNamespace(
        list_messages=mocker.AsyncMock(side_effect=_http_error(503))
    )
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(client, [_mail_result(42)], _sem())
    assert result == {"42"}


@pytest.mark.unit
async def test_verify_mail_missing_mailbox_id_keeps(mocker):
    """A result without a usable mailbox_id is kept without any network call."""
    list_messages = mocker.AsyncMock()
    mail_client = SimpleNamespace(list_messages=list_messages)
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(
        client, [_make_result(42, doc_type="mail_message")], _sem()
    )
    assert result == {"42"}
    list_messages.assert_not_awaited()


@pytest.mark.unit
async def test_verify_mail_non_numeric_mailbox_id_keeps(mocker):
    """A non-numeric mailbox_id in metadata is kept without a list call."""
    list_messages = mocker.AsyncMock()
    mail_client = SimpleNamespace(list_messages=list_messages)
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(
        client,
        [_make_result(42, doc_type="mail_message", metadata={"mailbox_id": "bad"})],
        _sem(),
    )
    assert result == {"42"}
    list_messages.assert_not_awaited()


@pytest.mark.unit
async def test_verify_mail_non_numeric_id_kept_when_mailbox_listed(mocker):
    """A malformed doc_id can't match the numeric listing, so it's kept."""
    mail_client = SimpleNamespace(
        list_messages=mocker.AsyncMock(return_value=[{"databaseId": 99}])
    )
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(client, [_mail_result("not-a-number")], _sem())
    assert result == {"not-a-number"}


@pytest.mark.unit
async def test_verify_mail_partitions_distinct_mailboxes(mocker):
    """Results in different mailboxes each get their own list call."""

    async def list_messages(mailbox_id, *, limit):
        return {10: [{"databaseId": 1}], 20: [{"databaseId": 2}]}[mailbox_id]

    mail_client = SimpleNamespace(
        list_messages=mocker.AsyncMock(side_effect=list_messages)
    )
    client = SimpleNamespace(mail=mail_client, username="alice")

    result = await _verify_mail_messages(
        client,
        [_mail_result(1, mailbox_id=10), _mail_result(2, mailbox_id=20)],
        _sem(),
    )
    assert result == {"1", "2"}
    assert mail_client.list_messages.await_count == 2


# ---------------------------------------------------------------------------
# News batch verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_news_items_intersects_with_fetched_set(mocker):
    """News verifier does ONE fetch and intersects, regardless of how many ids."""
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(return_value=[{"id": 10}, {"id": 20}, {"id": 30}])
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(10, doc_type="news_item"),
            _make_result(20, doc_type="news_item"),
            _make_result(99, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {"10", "20"}
    assert news_client.get_items.await_count == 1


@pytest.mark.unit
async def test_verify_news_items_api_404_drops_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_news_items_api_403_drops_all(mocker):
    """News API 403 (e.g. user lost access to the app) drops all items."""
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_news_items_transient_keeps_all(mocker):
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(502))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {"1", "2", "3"}


@pytest.mark.unit
async def test_verify_news_items_429_keeps_as_transient(mocker):
    """HTTP 429 from get_items must NOT collapse the batch (transient)."""
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=_http_error(429))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
            _make_result(3, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {"1", "2", "3"}


@pytest.mark.unit
async def test_verify_news_items_unexpected_exception_keeps_all(mocker):
    """A non-HTTP exception from get_items must keep all results (fail open).

    Covers the catch-all ``except Exception`` branch that exists so a bug
    in the News client (or an httpx connection error) cannot silently shrink
    the result set.
    """
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(side_effect=RuntimeError("news client boom"))
    )
    client = SimpleNamespace(news=news_client, username="alice")

    result = await _verify_news_items(
        client,
        [
            _make_result(1, doc_type="news_item"),
            _make_result(2, doc_type="news_item"),
        ],
        _sem(),
    )

    assert result == {"1", "2"}


@pytest.mark.unit
async def test_verify_news_items_non_numeric_id_keeps_only_bad_item(mocker):
    """A non-numeric doc_id is fail-open per item, not per batch.

    The intersection logic in `_verify_news_items` tries `int(d)` for each
    incoming doc_id; a single non-numeric value (e.g. ``"abc"``) is now
    caught per-item — only that one id is preserved unverified, while
    valid numeric ids are still checked against the API response. Mirrors
    the per-item shape of the notes/files/deck verifiers (one bad id does
    not poison adjacent verifications).
    """
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(return_value=[{"id": 10}, {"id": 20}])
    )
    client = SimpleNamespace(news=news_client, username="alice")

    doc_ids: list[int | str] = [10, 20, "abc"]
    result = await _verify_news_items(
        client,
        [_make_result(d, doc_type="news_item") for d in doc_ids],
        _sem(),
    )

    # 10 and 20 are verified present; "abc" is unverifiable so kept fail-open.
    assert result == {"10", "20", "abc"}


@pytest.mark.unit
async def test_verify_news_items_drops_missing_when_other_id_is_non_numeric(
    mocker,
):
    """A non-numeric doc_id no longer rescues a definitively-missing id.

    Regression for the per-item fail-open: previously a single non-numeric
    doc_id triggered batch-wide fail-open, so a definitively-missing id
    (20 below) escaped eviction. With per-item handling, only "abc" is
    kept; 20 is correctly dropped.
    """
    news_client = SimpleNamespace(get_items=mocker.AsyncMock(return_value=[{"id": 10}]))
    client = SimpleNamespace(news=news_client, username="alice")

    doc_ids: list[int | str] = [10, 20, "abc"]
    result = await _verify_news_items(
        client,
        [_make_result(d, doc_type="news_item") for d in doc_ids],
        _sem(),
    )

    # 10 verified present, 20 verified missing (dropped), "abc" unverifiable.
    assert result == {"10", "abc"}


@pytest.mark.unit
async def test_verify_news_items_malformed_api_response_keeps_all(mocker):
    """A malformed API response (non-numeric server id) fails open per batch.

    Distinct from a non-numeric *stored* doc_id: when the News API itself
    returns garbage, we cannot build present_ids at all, so every requested
    doc_id is preserved (transient — eviction will retry on next query).
    """
    news_client = SimpleNamespace(
        get_items=mocker.AsyncMock(return_value=[{"id": "not-an-int"}, {"id": 20}])
    )
    client = SimpleNamespace(news=news_client, username="alice")

    doc_ids: list[int | str] = [10, 20]
    result = await _verify_news_items(
        client,
        [_make_result(d, doc_type="news_item") for d in doc_ids],
        _sem(),
    )

    # Batch fail-open: API broken, every requested id preserved.
    assert result == {"10", "20"}


# ---------------------------------------------------------------------------
# File verifier
# ---------------------------------------------------------------------------


def _patch_excluded(mocker, paths: set[str] | None = None, *, side_effect=None):
    """Patch the lazily-imported EXCLUDED_TAGS lookup used by _verify_files."""
    if side_effect is not None:
        mock = mocker.AsyncMock(side_effect=side_effect)
    else:
        mock = mocker.AsyncMock(return_value=paths if paths is not None else set())
    return mocker.patch(
        "nextcloud_mcp_server.server.tag_exclusion.get_excluded_file_paths", mock
    )


def _file_client(mocker, *, tagged=None, find_side_effect=None, username="alice"):
    """Build a client whose find_files_by_tag returns the given tagged files."""
    if find_side_effect is not None:
        find = mocker.AsyncMock(side_effect=find_side_effect)
    else:
        find = mocker.AsyncMock(return_value=tagged if tagged is not None else [])
    return SimpleNamespace(
        find_files_by_tag=find,
        webdav=SimpleNamespace(),
        username=username,
    )


@pytest.mark.unit
async def test_verify_files_tagged_is_kept(mocker):
    """A file currently carrying an index tag is kept, and the tagged set is
    fetched with one batch call per tag (not one per result). Both tags are
    queried by default (keyword-index is on by default)."""
    _patch_excluded(mocker)
    client = _file_client(mocker, tagged=[{"id": 100, "path": "/Documents/foo.pdf"}])

    result = await _verify_files(client, [_make_result(100, doc_type="file")], _sem())

    assert result == {"100"}
    client.find_files_by_tag.assert_any_await(
        "vector-index", mime_type_filter="application/pdf"
    )


@pytest.mark.unit
async def test_verify_files_untagged_drops(mocker):
    """A file removed from the vector-index tag (absent from the tagged set) is
    dropped even though it may still exist and be readable by the user."""
    _patch_excluded(mocker)
    client = _file_client(mocker, tagged=[{"id": 100, "path": "/Documents/foo.pdf"}])

    result = await _verify_files(
        client,
        [_make_result(100, doc_type="file"), _make_result(200, doc_type="file")],
        _sem(),
    )

    # 100 is still tagged → kept; 200 was untagged → dropped.
    assert result == {"100"}


@pytest.mark.unit
async def test_verify_files_deleted_drops(mocker):
    """A deleted file is absent from the tagged set → dropped (caller evicts)."""
    _patch_excluded(mocker)
    client = _file_client(mocker, tagged=[])

    result = await _verify_files(client, [_make_result(123, doc_type="file")], _sem())

    assert result == set()


@pytest.mark.unit
async def test_verify_files_empty_tag_set_skips_exclusion_lookup(mocker):
    """When the tag REPORT returns no files, the EXCLUDED_TAGS lookup is skipped
    entirely: an empty tagged set drops every valid-id result regardless of
    exclusions, so the lookup's 2xN WebDAV fan-out is wasted work. Malformed
    doc_ids are still kept (fail-open), exactly as on the non-empty path."""
    excluded = _patch_excluded(mocker, {"Secret"})
    client = _file_client(mocker, tagged=[])

    result = await _verify_files(
        client,
        [
            _make_result(123, doc_type="file"),
            _make_result("not-a-file-id", doc_type="file"),
        ],
        _sem(),
    )

    # Valid id absent from the (empty) tagged set → dropped; malformed id kept.
    assert result == {"not-a-file-id"}
    # The optimization: no exclusion fan-out when there is nothing to filter.
    excluded.assert_not_awaited()


@pytest.mark.unit
async def test_verify_files_excluded_path_drops(mocker):
    """A tagged file under an EXCLUDED_TAGS folder must not surface — exclusion
    wins, parity with the scanner's defense-in-depth filter."""
    # get_excluded_file_paths returns slash-stripped (normalised) paths.
    _patch_excluded(mocker, {"Secret"})
    client = _file_client(
        mocker,
        tagged=[
            {"id": 100, "path": "/Documents/foo.pdf"},
            {"id": 200, "path": "/Secret/bar.pdf"},
        ],
    )

    result = await _verify_files(
        client,
        [_make_result(100, doc_type="file"), _make_result(200, doc_type="file")],
        _sem(),
    )

    assert result == {"100"}


@pytest.mark.unit
async def test_verify_files_tag_fetch_failure_keeps_all(mocker):
    """If the tag REPORT itself fails, keep every file result (fail-open) —
    never silently shrink results on a backend blip.

    Unlike the per-access verifiers (notes/deck/news), where a definitive
    403/404 is the DROP signal, the file verifier fails open on *every* HTTP
    error — including 403/404. The whole result set hinges on one batch REPORT,
    so a disabled systemtags endpoint (commonly 403) must not nuke all file
    results; the next query re-verifies. 403 and 404 are pinned here alongside
    the transient 503/429 to lock that contract against regression.
    """
    _patch_excluded(mocker)
    for exc in (
        _http_error(403),
        _http_error(404),
        _http_error(503),
        _http_error(429),
        RuntimeError("dav blew up"),
    ):
        client = _file_client(mocker, find_side_effect=exc)
        result = await _verify_files(
            client,
            [_make_result(7, doc_type="file"), _make_result(8, doc_type="file")],
            _sem(),
        )
        assert result == {"7", "8"}, f"{exc!r} on the tag fetch must keep all results"


@pytest.mark.unit
async def test_verify_files_exclusion_lookup_failure_proceeds(mocker):
    """If the EXCLUDED_TAGS lookup fails, proceed without the exclusion filter
    rather than dropping legitimate tagged hits."""
    _patch_excluded(mocker, side_effect=RuntimeError("ocs down"))
    client = _file_client(mocker, tagged=[{"id": 100, "path": "/Documents/foo.pdf"}])

    result = await _verify_files(client, [_make_result(100, doc_type="file")], _sem())

    assert result == {"100"}


@pytest.mark.unit
async def test_verify_files_non_numeric_id_keeps(mocker):
    """A malformed (non-numeric) doc_id cannot be matched against the numeric
    tag REPORT, so it is kept (defense-in-depth, false-positive preferred)."""
    _patch_excluded(mocker)
    client = _file_client(mocker, tagged=[])

    result = await _verify_files(
        client, [_make_result("not-a-file-id", doc_type="file")], _sem()
    )

    assert result == {"not-a-file-id"}


# ---------------------------------------------------------------------------
# Deck card verifier
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_deck_cards_uses_metadata_fast_path(mocker):
    """Deck verifier reads board_id+stack_id from metadata, no Qdrant round-trip."""
    deck_client = SimpleNamespace(get_card=mocker.AsyncMock(return_value=object()))
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == {"42"}
    deck_client.get_card.assert_awaited_once_with(board_id=1, stack_id=2, card_id=42)


@pytest.mark.unit
async def test_verify_deck_cards_403_drops(mocker):
    """Board unshared with user → 403 from get_card → drop."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(403))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_deck_cards_404_drops(mocker):
    """Card deleted from the board → 404 from get_card → drop."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(404))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == set()


@pytest.mark.unit
async def test_verify_deck_cards_transient_5xx_keeps(mocker):
    """Transient 5xx from get_card must NOT silently shrink results."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(502))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == {"42"}


@pytest.mark.unit
async def test_verify_deck_cards_429_keeps_as_transient(mocker):
    """HTTP 429 from get_card must NOT silently shrink result pages."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=_http_error(429))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == {"42"}


@pytest.mark.unit
async def test_verify_deck_cards_unexpected_exception_keeps(mocker):
    """Non-HTTP exception from get_card → fail-open, keep result."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=RuntimeError("deck client boom"))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )

    assert result == {"42"}


@pytest.mark.unit
async def test_verify_deck_cards_non_numeric_metadata_keeps(mocker):
    """Non-numeric board_id/stack_id/card_id must fail open before the API call.

    The hoisted ``int()`` casts in ``_verify_deck_cards`` produce a
    type-specific log line; the catch-all path must never run.
    """
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    # Non-numeric board_id
    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                42,
                doc_type="deck_card",
                metadata={"board_id": "not-a-number", "stack_id": 2},
            )
        ],
        _sem(),
    )
    assert result == {"42"}

    # Non-numeric stack_id
    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                43,
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": "bad"},
            )
        ],
        _sem(),
    )
    assert result == {"43"}

    # Non-numeric card_id (doc_id itself)
    result = await _verify_deck_cards(
        client,
        [
            _make_result(
                "card-uuid",
                doc_type="deck_card",
                metadata={"board_id": 1, "stack_id": 2},
            )
        ],
        _sem(),
    )
    assert result == {"card-uuid"}

    deck_client.get_card.assert_not_awaited()


@pytest.mark.unit
async def test_verify_deck_cards_missing_metadata_keeps_unverified(mocker):
    """Legacy data without board_id/stack_id → keep, do NOT iterate or call API."""
    deck_client = SimpleNamespace(
        get_card=mocker.AsyncMock(side_effect=AssertionError("must not be called"))
    )
    client = SimpleNamespace(deck=deck_client, username="alice")

    # No metadata at all
    result = await _verify_deck_cards(
        client, [_make_result(42, doc_type="deck_card")], _sem()
    )
    assert result == {"42"}

    # Only board_id (stack_id missing)
    result = await _verify_deck_cards(
        client,
        [_make_result(43, doc_type="deck_card", metadata={"board_id": 1})],
        _sem(),
    )
    assert result == {"43"}

    # Only stack_id (board_id missing)
    result = await _verify_deck_cards(
        client,
        [_make_result(44, doc_type="deck_card", metadata={"stack_id": 2})],
        _sem(),
    )
    assert result == {"44"}

    deck_client.get_card.assert_not_awaited()


# ---------------------------------------------------------------------------
# Top-level verify_search_results
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_search_results_empty_input_passthrough():
    client = SimpleNamespace(username="alice")
    assert await verify_search_results(client, []) == ([], 0)


@pytest.mark.unit
async def test_verify_search_results_dedupes_chunks_per_document(mocker):
    """Two chunks of the same note → ONE call to the underlying verifier."""
    spy = mocker.AsyncMock(return_value={"1"})
    mocker.patch.dict(verification._VERIFIERS, {"note": spy}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note", chunk_index=0),
        _make_result(1, doc_type="note", chunk_index=1),
        _make_result(1, doc_type="note", chunk_index=2),
    ]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert len(kept) == 3  # all kept, all reference the same accessible doc
    assert dropped_count == 0
    spy.assert_awaited_once()
    # Verifier received exactly one SearchResult (the deduplicated representative)
    args, _kwargs = spy.call_args
    assert len(args[1]) == 1
    assert args[1][0].id == "1"
    # And a semaphore as the third arg
    assert isinstance(args[2], anyio.Semaphore)


@pytest.mark.unit
async def test_verify_search_results_drops_inaccessible_and_evicts(mocker):
    """Inline-fallback path (no eviction_task_group): evict completes before return.

    Notes are stored with string doc_ids in production (scanner.py:241
    ``doc_id = str(note["id"])``), so this test uses string ids end-to-end
    to exercise the actual production type — ``SearchResult.id``,
    ``_VERIFIERS["note"]`` return-set members, and
    ``delete_document_points`` arguments all stay as ``str``.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Verifier reports note "1" accessible, note "99" not
    note_verifier = mocker.AsyncMock(return_value={"1"})
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [
        _make_result("1", doc_type="note"),
        _make_result("99", doc_type="note"),
    ]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert [r.id for r in kept] == ["1"]
    assert dropped_count == 1
    spy_evict.assert_awaited_once_with("99", "note", "alice")


@pytest.mark.unit
async def test_verify_evicts_cross_user_file_under_querying_user_id(mocker):
    """A shared file the recipient can no longer access is evicted under the
    QUERYING user's id, never the owner's.

    This guards the cross-user eviction no-op: a point owned by alice
    (user_id=alice) surfaced to bob via accessible_owners and then found
    inaccessible must be evicted with user_id=bob — which deletes nothing of
    alice's (her points carry user_id=alice). So a recipient's revoked access
    can never delete the owner's index entries; bob's view self-heals via
    list_accessible_owners instead. A future change that evicted under the
    owner's id would corrupt the owner's index, and this test would catch it.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)
    _patch_excluded(mocker)

    # The shared file is no longer in bob's tagged set (share revoked → absent
    # from his vector-index tag REPORT), so the file verifier drops it.
    client = _file_client(mocker, tagged=[], username="bob")

    kept, dropped_count = await verify_search_results(
        client,
        [_make_result(777, doc_type="file", metadata={"path": "shared.txt"})],
    )

    assert kept == []
    assert dropped_count == 1
    spy_evict.assert_awaited_once_with("777", "file", "bob")


@pytest.mark.unit
async def test_verify_search_results_fire_and_forget_eviction(mocker):
    """When eviction_task_group is provided, eviction does not block the response.

    Validates the ADR-019 design: spawn evict() on the lifespan-owned task
    group via start_soon so the search response returns immediately. The
    eviction still runs (verified after the task group exits).
    """
    eviction_started = anyio.Event()
    eviction_may_complete = anyio.Event()
    eviction_completed = anyio.Event()

    async def slow_delete(doc_id, doc_type, user_id):
        eviction_started.set()
        await eviction_may_complete.wait()
        eviction_completed.set()

    mocker.patch.object(
        verification,
        "delete_document_points",
        mocker.AsyncMock(side_effect=slow_delete),
    )

    note_verifier = mocker.AsyncMock(return_value=set())  # both inaccessible
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [_make_result(99, doc_type="note")]
    client = SimpleNamespace(username="alice")

    async with anyio.create_task_group() as tg:
        kept, dropped_count = await verify_search_results(
            client, results, eviction_task_group=tg
        )
        # 1. Search response was returned …
        assert kept == []
        assert dropped_count == 1
        # 2. … even though eviction has started but not finished.
        await eviction_started.wait()
        assert not eviction_completed.is_set()
        # 3. Now allow eviction to complete; the task group exit awaits it.
        eviction_may_complete.set()

    # After the task group exits, the eviction must have run.
    assert eviction_completed.is_set()


@pytest.mark.unit
async def test_verify_search_results_eviction_task_group_closed_is_ignored(
    mocker,
):
    """A closed eviction task group must not surface as a search error.

    Guards the race documented in `verification.py`: the lifespan task
    group can exit between the ``getattr()`` capture in
    ``server/semantic.py`` and the ``start_soon`` call here. Calling
    ``start_soon`` on a closed group raises ``RuntimeError``; the
    verifier must catch and log it (eviction is best-effort, the next
    query re-verifies). Without this guard the search response would
    fail.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    note_verifier = mocker.AsyncMock(return_value=set())  # 99 inaccessible
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    class ClosedTaskGroup:
        """Stand-in for an exited anyio.TaskGroup."""

        def __init__(self):
            self.start_soon_calls = 0

        def start_soon(self, *_args, **_kwargs):
            self.start_soon_calls += 1
            raise RuntimeError("This task group is not active")

    closed_tg = ClosedTaskGroup()

    results = [_make_result(99, doc_type="note")]
    client = SimpleNamespace(username="alice")

    # Must NOT raise even though start_soon raises RuntimeError.
    kept, dropped_count = await verify_search_results(
        client, results, eviction_task_group=closed_tg
    )

    assert kept == []
    assert dropped_count == 1
    assert closed_tg.start_soon_calls == 1
    # Inline fallback must NOT run when a (closed) task group was provided —
    # the guard is fire-and-forget, eviction is dropped on the floor.
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_no_eviction_when_disabled(mocker):
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    note_verifier = mocker.AsyncMock(return_value=set())  # all inaccessible
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [_make_result(7, doc_type="note")]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(
        client, results, evict_on_missing=False
    )

    assert kept == []
    assert dropped_count == 1
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_unknown_doc_type_passes_through(mocker, caplog):
    """No verifier registered for doc_type → keep, log a warning."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)
    # Ensure no verifier for "calendar"
    mocker.patch.dict(
        verification._VERIFIERS,
        {k: v for k, v in verification._VERIFIERS.items() if k != "calendar"},
        clear=True,
    )

    results = [_make_result(1, doc_type="calendar")]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert len(kept) == 1
    assert dropped_count == 0
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_verifier_blowup_keeps_all(mocker):
    """A verifier raising an unexpected exception must not silently drop results."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)
    note_verifier = mocker.AsyncMock(side_effect=RuntimeError("qdrant down"))
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    results = [
        _make_result(1, doc_type="note"),
        _make_result(2, doc_type="note"),
    ]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert [r.id for r in kept] == ["1", "2"]
    assert dropped_count == 0  # fail-open: nothing dropped
    spy_evict.assert_not_awaited()


@pytest.mark.unit
async def test_verify_search_results_preserves_order(mocker):
    """Order of original results must be preserved after filtering."""
    note_verifier = mocker.AsyncMock(return_value={"1", "3"})
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note", score=0.9),
        _make_result(2, doc_type="note", score=0.8),
        _make_result(3, doc_type="note", score=0.7),
    ]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert [r.id for r in kept] == ["1", "3"]
    assert dropped_count == 1


@pytest.mark.unit
async def test_verify_search_results_eviction_failure_does_not_propagate(mocker):
    """Eviction failures are logged, never raised — must not break search."""
    mocker.patch.object(
        verification,
        "delete_document_points",
        mocker.AsyncMock(side_effect=RuntimeError("qdrant down")),
    )
    note_verifier = mocker.AsyncMock(return_value=set())
    mocker.patch.dict(verification._VERIFIERS, {"note": note_verifier}, clear=False)

    client = SimpleNamespace(username="alice")
    # Should NOT raise
    kept, dropped_count = await verify_search_results(
        client, [_make_result(1, doc_type="note")]
    )
    assert kept == []
    assert dropped_count == 1


@pytest.mark.unit
async def test_verify_search_results_dispatches_per_doc_type_concurrently(mocker):
    """Mixed doc_types must be routed to their respective verifiers."""
    note_verifier = mocker.AsyncMock(return_value={"1"})
    file_verifier = mocker.AsyncMock(return_value={"500"})
    mocker.patch.dict(
        verification._VERIFIERS,
        {"note": note_verifier, "file": file_verifier},
        clear=False,
    )
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    results = [
        _make_result(1, doc_type="note"),
        _make_result(500, doc_type="file", metadata={"path": "a.txt"}),
        _make_result(999, doc_type="file", metadata={"path": "b.txt"}),  # to be dropped
    ]
    client = SimpleNamespace(username="alice")

    kept, dropped_count = await verify_search_results(client, results)

    assert {(r.id, r.doc_type) for r in kept} == {("1", "note"), ("500", "file")}
    assert dropped_count == 1
    note_verifier.assert_awaited_once()
    file_verifier.assert_awaited_once()


@pytest.mark.unit
async def test_verify_search_results_passes_semaphore_to_verifier(mocker):
    """The dispatcher must construct a Semaphore and pass it to verifiers."""
    captured: dict[str, anyio.Semaphore] = {}

    async def verifier(client, results, semaphore):
        captured["sem"] = semaphore
        return {r.id for r in results}

    mocker.patch.dict(verification._VERIFIERS, {"note": verifier}, clear=False)
    mocker.patch.object(verification, "delete_document_points", mocker.AsyncMock())

    client = SimpleNamespace(username="alice")
    await verify_search_results(client, [_make_result(1)], max_concurrent=5)

    assert isinstance(captured["sem"], anyio.Semaphore)
