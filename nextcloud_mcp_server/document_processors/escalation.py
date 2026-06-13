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

from dataclasses import dataclass
from typing import Literal

# Cheapest-first. ``llm`` is reserved (see base.DocumentProcessor.tier) and not
# wired yet, so it is intentionally absent from the live ladder.
TIER_LADDER: tuple[str, ...] = ("fast", "structured", "ocr")


@dataclass(frozen=True)
class EscalationDecision:
    """Outcome of the post-parse quality gate (``ProcessorRegistry.evaluate_escalation``).

    ``kind``:
      * ``"hop"`` — the parse is too poor and a higher tier *can run*; the caller
        raises :class:`EscalateError` to requeue the document onto ``to_tier``.
      * ``"suppressed"`` — the parse would escalate to ``to_tier`` (the *ideal*
        next tier), but that tier is **disabled** (e.g. OCR off). The caller does
        NOT hop — it indexes the current tier's output as terminal — and records
        the would-be escalation so operators see the latent demand ("what-if OCR
        were enabled"). Enabling the tier turns these into real ``"hop"`` events.

    A ``None`` return from ``evaluate_escalation`` (not an instance of this class)
    means "index as-is, nothing to escalate" — good text, or no higher tier
    exists at all (no processor registered for it).
    """

    kind: Literal["hop", "suppressed"]
    to_tier: str
    reason: str  # empty_text | low_confidence


def next_tier(current: str) -> str | None:
    """The next tier above ``current`` in the ladder, or ``None`` if terminal.

    Pure ordering only -- it does not consider whether the next tier is
    *available* (a processor registered / OCR enabled). **Production routing uses
    ``ProcessorRegistry.next_available_tier``**, which layers availability on top
    of this ordering; ``next_tier`` itself is the underlying building block
    (referenced directly by tests). A tier with no escalation target is terminal
    and its result is indexed as-is.
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

    ``reason`` uses the existing escalation label vocabulary. This PR raises
    ``empty_text`` (scanned / no text layer) and ``low_confidence`` (junk text
    layer); ``unsupported`` and ``forced`` are reserved for future callers and
    not raised yet.
    """

    def __init__(self, *, from_tier: str, to_tier: str, reason: str) -> None:
        self.from_tier = from_tier
        self.to_tier = to_tier
        self.reason = reason
        super().__init__(
            f"escalate {from_tier}->{to_tier} (reason={reason})",
        )
