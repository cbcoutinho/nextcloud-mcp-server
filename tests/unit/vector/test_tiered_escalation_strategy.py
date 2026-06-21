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
    BatchPending,
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
        # The two pre-consolidation split OCR queues resolve to the single ``ocr``
        # tier so in-flight OCR jobs from an older deploy keep OCR'ing during rollout.
        assert pq.tier_for_queue(pq.LEGACY_INGEST_QUEUE_OCR_INCLUSTER) == "ocr"
        assert pq.tier_for_queue(pq.LEGACY_INGEST_QUEUE_OCR_UPSTREAM) == "ocr"


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
        # procrastinate sets retry_at = utcnow() + wait at call time. Bracketing
        # the call with before/after makes the assertion exact and independent of
        # runner load: with before <= call_now <= after, we have
        #   (retry_at - after) <= wait <= (retry_at - before).
        strat = self._strategy(max_transient=100)
        for attempts, expected in [(1, 4), (2, 8), (3, 16), (4, 32), (20, 300)]:
            before = datetime.now(timezone.utc)
            decision = strat.get_retry_decision(
                exception=httpx.ConnectError("x"), job=_job(attempts=attempts)
            )
            after = datetime.now(timezone.utc)
            assert decision is not None and decision.retry_at is not None
            lo = (decision.retry_at - after).total_seconds()
            hi = (decision.retry_at - before).total_seconds()
            assert lo <= expected <= hi, (
                f"attempts={attempts}: expected={expected}s not in [{lo:.3f}, {hi:.3f}]"
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

    def test_batch_pending_defers_same_queue(self):
        # Batch OCR re-poll (Deck #332): same-queue deferral after retry_in.
        before = datetime.now(timezone.utc)
        decision = self._strategy().get_retry_decision(
            exception=BatchPending(retry_in=120),
            job=_job(queue=pq.INGEST_QUEUE_OCR),
        )
        after = datetime.now(timezone.utc)
        assert decision is not None
        assert decision.queue is None  # stays on its own tier queue
        assert decision.retry_at is not None
        lo = (decision.retry_at - after).total_seconds()
        hi = (decision.retry_at - before).total_seconds()
        assert lo <= 120 <= hi

    def test_batch_pending_exempt_from_transient_cap(self):
        # A batch can take hours -> many polls; the transient cap must NOT stop it
        # (the OCR processor's own deadline terminates a stuck job instead).
        decision = self._strategy(max_transient=5).get_retry_decision(
            exception=BatchPending(retry_in=60), job=_job(attempts=999)
        )
        assert decision is not None and decision.retry_at is not None

    def test_batch_pending_unwraps_exception_group(self):
        group = ExceptionGroup("wrapped", [BatchPending(retry_in=60)])
        decision = self._strategy().get_retry_decision(exception=group, job=_job())
        assert decision is not None and decision.retry_at is not None
