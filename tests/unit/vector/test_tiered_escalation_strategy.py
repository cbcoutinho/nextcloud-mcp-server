"""Unit tests for the per-tier escalation primitives (Deck #323).

Covers the tier-ladder helpers + EscalateError (document_processors.escalation)
and the procrastinate TieredEscalationStrategy that turns a raised exception
into a queue-hop / same-tier retry / give-up decision.
"""

from datetime import datetime, timezone

import httpx
import pytest
from procrastinate.jobs import Job

import nextcloud_mcp_server.vector.queue.procrastinate as pq
from nextcloud_mcp_server.document_processors.escalation import (
    TIER_LADDER,
    EscalateError,
    next_tier,
)

pytestmark = pytest.mark.unit


def _job(queue: str = pq.INGEST_QUEUE_FAST, attempts: int = 1) -> Job:
    return Job(
        id=1,
        queue=queue,
        task_name=pq.INGEST_TASK_NAME,
        lock=None,
        queueing_lock=None,
        attempts=attempts,
    )


class TestLadder:
    def test_next_tier_ordering(self):
        assert next_tier("fast") == "structured"
        assert next_tier("structured") == "ocr"
        assert next_tier("ocr") is None  # terminal
        assert next_tier("unknown") is None

    def test_ladder_is_cheapest_first(self):
        assert TIER_LADDER == ("fast", "structured", "ocr")

    def test_tier_for_queue(self):
        assert pq.tier_for_queue(pq.INGEST_QUEUE_OCR) == "ocr"
        assert pq.tier_for_queue(pq.INGEST_QUEUE_STRUCTURED) == "structured"
        # Legacy / unknown / None all fall back to the cheapest tier.
        assert pq.tier_for_queue(pq.LEGACY_INGEST_QUEUE) == "fast"
        assert pq.tier_for_queue(None) == "fast"


class TestTieredEscalationStrategy:
    def _strategy(self, max_transient: int = 5):
        return pq.TieredEscalationStrategy(max_transient_attempts=max_transient)

    def test_escalate_hops_to_target_queue(self):
        exc = EscalateError(from_tier="fast", to_tier="ocr", reason="empty_text")
        decision = self._strategy().get_retry_decision(exception=exc, job=_job())
        assert decision is not None
        assert decision.queue == pq.INGEST_QUEUE_OCR

    def test_escalate_to_structured(self):
        exc = EscalateError(
            from_tier="fast", to_tier="structured", reason="low_confidence"
        )
        decision = self._strategy().get_retry_decision(exception=exc, job=_job())
        assert decision is not None
        assert decision.queue == pq.INGEST_QUEUE_STRUCTURED

    def test_escalate_unknown_tier_gives_up(self):
        exc = EscalateError(from_tier="ocr", to_tier="bogus", reason="low_confidence")
        decision = self._strategy().get_retry_decision(exception=exc, job=_job())
        assert decision is None

    def test_escalate_unwraps_exception_group(self):
        exc = EscalateError(from_tier="fast", to_tier="ocr", reason="empty_text")
        group = ExceptionGroup("wrapped", [exc])
        decision = self._strategy().get_retry_decision(exception=group, job=_job())
        assert decision is not None
        assert decision.queue == pq.INGEST_QUEUE_OCR

    def test_transient_retries_same_queue_under_cap(self):
        decision = self._strategy(max_transient=5).get_retry_decision(
            exception=httpx.ConnectError("refused"), job=_job(attempts=1)
        )
        assert decision is not None
        # Same-tier retry: no queue override (stays on its current queue).
        assert decision.queue is None
        assert decision.retry_at is not None

    def test_transient_backoff_progression(self):
        # min(4 * 2**(attempts-1), 300): 4, 8, 16, ... capped at 300s.
        strat = self._strategy(max_transient=100)
        for attempts, expected in [(1, 4), (2, 8), (3, 16), (4, 32), (20, 300)]:
            decision = strat.get_retry_decision(
                exception=httpx.ConnectError("x"), job=_job(attempts=attempts)
            )
            assert decision is not None and decision.retry_at is not None
            delta = (decision.retry_at - datetime.now(timezone.utc)).total_seconds()
            # retry_at = now + wait; allow a small window for execution time.
            assert expected - 2 <= delta <= expected + 1, (
                f"attempts={attempts}: delta={delta:.2f}s, expected≈{expected}s"
            )

    def test_transient_gives_up_over_cap(self):
        decision = self._strategy(max_transient=5).get_retry_decision(
            exception=httpx.ConnectError("refused"), job=_job(attempts=5)
        )
        assert decision is None

    def test_non_transient_error_gives_up(self):
        decision = self._strategy().get_retry_decision(
            exception=ValueError("permanent"), job=_job(attempts=1)
        )
        assert decision is None
