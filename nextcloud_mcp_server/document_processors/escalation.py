"""Tier-escalation ladder + signal for the per-tier ingest fleet (Deck #323).

The escalation ladder is the cheapest-first ordering of extraction tiers:

    fast  ->  structured  ->  ocr   ( ->  llm, reserved)

It mirrors the ``tier`` vocabulary documented on
:meth:`DocumentProcessor.tier <.base.DocumentProcessor.tier>` and the
observability label set. On the *external* (procrastinate) ingest path each tier
runs on its own queue + worker fleet; a document that a tier cannot parse well is
**requeued onto the next tier's queue** rather than escalated inline. The
mechanism is a raised :class:`EscalateError` that the procrastinate retry
strategy turns into a native ``RetryDecision(queue=<next-tier queue>)`` queue-hop
(see ``vector/queue/procrastinate.py``).

This module is deliberately free of any queue/transport dependency: it only
knows the *tier* vocabulary and the escalation signal. The tier -> queue-name
mapping lives in the queue layer, which imports :class:`EscalateError` from here
(document_processors never imports vector.queue, so there is no import cycle).
"""

from __future__ import annotations

# Cheapest-first. ``llm`` is reserved (see base.DocumentProcessor.tier) and not
# wired yet, so it is intentionally absent from the live ladder.
TIER_LADDER: tuple[str, ...] = ("fast", "structured", "ocr")


def next_tier(current: str) -> str | None:
    """The next tier above ``current`` in the ladder, or ``None`` if terminal.

    Pure ordering only -- it does not consider whether the next tier is
    *available* (a processor registered / OCR enabled). Callers that need
    availability resolve it against the registry + settings (see
    ``ProcessorRegistry.next_available_tier``); a tier with no escalation target
    is terminal and its result is indexed as-is.
    """
    try:
        idx = TIER_LADDER.index(current)
    except ValueError:
        return None
    nxt = idx + 1
    return TIER_LADDER[nxt] if nxt < len(TIER_LADDER) else None


class EscalateError(Exception):
    """Raised when a tier's parse is too poor to index and a higher tier exists.

    Carries the tiers + reason so the procrastinate retry strategy can hop the
    job to the next tier's queue and record
    ``astrolabe_document_escalation_total{from_tier,to_tier,reason}``. It is a
    control-flow signal, NOT a failure: it must propagate *before* chunk/embed so
    the junk text is never indexed, and it must never be swallowed by a broad
    ``except Exception`` on the indexing path.

    ``reason`` uses the existing escalation label vocabulary:
    ``empty_text`` | ``low_confidence`` | ``unsupported`` | ``forced``.
    """

    def __init__(self, *, from_tier: str, to_tier: str, reason: str) -> None:
        self.from_tier = from_tier
        self.to_tier = to_tier
        self.reason = reason
        super().__init__(
            f"escalate {from_tier}->{to_tier} (reason={reason})",
        )
