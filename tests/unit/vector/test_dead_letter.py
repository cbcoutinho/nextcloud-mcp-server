"""Unit tests for content-addressed dead-letter markers.

Covers vector/dead_letter.py: the durable, user-agnostic terminal-failure marker
that stops a multi-user shared file (whose single placeholder's user_id is
overwritten by the last scanner) from being re-queued forever. A marker is keyed
by content (``etag``) + escalation config (``tiers_sig``); a scan skips only while
both still match, so a content change or a newly-available tier (e.g. OCR enabled)
makes the document retryable again.

Qdrant is reached via ``get_qdrant_client``/``get_settings``/``get_embedding_service``,
all monkeypatched here so the logic runs without a live Qdrant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector import dead_letter as dl

pytestmark = pytest.mark.unit

_COLLECTION = "test_collection"


class _Settings:
    # The dense slot is always sized from the embedding provider (no keyword
    # branch anymore); the embedding service stub in the fixture returns dim=4.
    def get_collection_name(self) -> str:
        return _COLLECTION


def _point(payload: dict) -> SimpleNamespace:
    """Stand-in for a qdrant_client Record (only id/payload are read)."""
    return SimpleNamespace(id="pt", payload=payload)


@pytest.fixture
def client(monkeypatch) -> AsyncMock:
    """An AsyncMock Qdrant client wired into dead_letter, plus stub deps.

    ``scroll`` defaults to "no points"; individual tests override
    ``client.scroll.return_value``/``side_effect``.
    """
    qc = AsyncMock()
    qc.scroll.return_value = ([], None)
    monkeypatch.setattr(dl, "get_qdrant_client", AsyncMock(return_value=qc))
    monkeypatch.setattr(dl, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        dl, "get_embedding_service", lambda: SimpleNamespace(get_dimension=lambda: 4)
    )
    return qc


def _must_conditions(flt) -> dict:
    """Map FieldCondition key -> matched value for a Filter's ``must`` clause."""
    out = {}
    for c in flt.must or []:
        key = getattr(c, "key", None)
        match = getattr(c, "match", None)
        out[key] = getattr(match, "value", None)
    return out


class TestDeadLetterId:
    def test_user_agnostic_and_distinct_from_placeholder(self) -> None:
        from nextcloud_mcp_server.vector.placeholder import _generate_placeholder_id

        dl_id = dl._generate_dead_letter_id("file", "520189")
        # Deterministic + user-agnostic (depends only on doc_type:doc_id).
        assert dl_id == dl._generate_dead_letter_id("file", "520189")
        # Never collides with the in-flight placeholder for the same document.
        assert dl_id != _generate_placeholder_id("file", "520189")


class TestMarkDeadLetter:
    async def test_upserts_content_addressed_marker(self, client) -> None:
        await dl.mark_dead_letter(
            "520189",
            "file",
            "etag-1",
            "ocr=0;t1=pypdfium2",
            "timeout",
            file_path="/Plans/big.pdf",
        )
        client.upsert.assert_awaited_once()
        point = client.upsert.await_args.kwargs["points"][0]
        assert point.id == dl._generate_dead_letter_id("file", "520189")
        payload = point.payload
        assert payload["is_placeholder"] is True
        assert payload[dl.DEAD_LETTER_KEY] is True
        assert payload["etag"] == "etag-1"
        assert payload["tiers_sig"] == "ocr=0;t1=pypdfium2"
        assert payload["reason"] == "timeout"
        assert payload["doc_id"] == "520189"
        assert payload["file_path"] == "/Plans/big.pdf"
        # Dense slot is always sized from the embedding provider (dim=4 here) —
        # the keyword/simple-dimension branch is gone (per-document index mode).
        assert len(point.vector["dense"]) == 4

    async def test_failure_is_swallowed(self, client) -> None:
        client.upsert.side_effect = RuntimeError("qdrant down")
        # Best-effort: a write failure must not propagate (would crash the worker).
        await dl.mark_dead_letter("1", "file", "e", "sig", "oom")


class TestIsDeadLettered:
    def _marker(self, etag: str, tiers_sig: str) -> SimpleNamespace:
        return _point(
            {
                "doc_id": "520189",
                dl.DEAD_LETTER_KEY: True,
                "etag": etag,
                "tiers_sig": tiers_sig,
            }
        )

    async def test_true_when_etag_and_sig_match(self, client) -> None:
        client.scroll.return_value = ([self._marker("e1", "sig1")], None)
        assert await dl.is_dead_lettered("520189", "file", "e1", "sig1") is True

    async def test_false_on_etag_change(self, client) -> None:
        client.scroll.return_value = ([self._marker("e1", "sig1")], None)
        # File content changed -> retryable.
        assert await dl.is_dead_lettered("520189", "file", "e2", "sig1") is False

    async def test_false_on_tiers_sig_change(self, client) -> None:
        client.scroll.return_value = ([self._marker("e1", "ocr=0;t1=pypdfium2")], None)
        # OCR just enabled -> a new escalation tier exists -> retryable.
        assert (
            await dl.is_dead_lettered("520189", "file", "e1", "ocr=1;t1=pypdfium2")
            is False
        )

    async def test_false_when_no_marker(self, client) -> None:
        client.scroll.return_value = ([], None)
        assert await dl.is_dead_lettered("520189", "file", "e1", "sig1") is False

    async def test_false_on_empty_etag(self, client) -> None:
        # Cannot content-address without an etag; never dead-lettered.
        assert await dl.is_dead_lettered("520189", "file", "", "sig1") is False
        client.scroll.assert_not_awaited()

    async def test_false_on_qdrant_error(self, client) -> None:
        client.scroll.side_effect = RuntimeError("qdrant down")
        # Degrade to "process normally" rather than aborting the scan.
        assert await dl.is_dead_lettered("520189", "file", "e1", "sig1") is False

    async def test_filter_is_user_agnostic(self, client) -> None:
        client.scroll.return_value = ([self._marker("e1", "sig1")], None)
        await dl.is_dead_lettered("520189", "file", "e1", "sig1")
        flt = client.scroll.await_args.kwargs["scroll_filter"]
        conds = _must_conditions(flt)
        assert conds == {
            "doc_id": "520189",
            "doc_type": "file",
            "is_placeholder": True,
            dl.DEAD_LETTER_KEY: True,
        }
        assert "user_id" not in conds


class TestClearDeadLetter:
    async def test_deletes_by_marker_filter(self, client) -> None:
        await dl.clear_dead_letter("520189", "file")
        client.delete.assert_awaited_once()
        flt = client.delete.await_args.kwargs["points_selector"]
        conds = _must_conditions(flt)
        assert conds[dl.DEAD_LETTER_KEY] is True
        assert conds["doc_id"] == "520189"

    async def test_failure_is_swallowed(self, client) -> None:
        client.delete.side_effect = RuntimeError("qdrant down")
        await dl.clear_dead_letter("520189", "file")
