"""Tier-escalation ladder + signal for the per-tier ingest fleet (Deck #323).

The escalation ladder is the cheapest-first ordering of extraction tiers:

    fast  ->  structured  ->  ocr-incluster  ->  ocr-upstream   ( ->  llm, reserved)

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
from typing import Any, Literal

# Cheapest-first. OCR is split into two rungs: ``ocr-incluster`` (the on-demand
# burst GPU, e.g. surya, reached via the embedding gateway over the tailnet) tried
# BEFORE ``ocr-upstream`` (paid Mistral). ``llm`` is reserved (see
# base.DocumentProcessor.tier) and not wired yet.
TIER_LADDER: tuple[str, ...] = ("fast", "structured", "ocr-incluster", "ocr-upstream")


def escalation_tiers_signature(settings: Any) -> str:
    """A stable string fingerprint of the runtime escalation-tier configuration.

    Used by the document dead-letter marker (``vector/dead_letter.py``) as part of
    its content key: a document that fails its terminal tier is dead-lettered until
    either its content (etag) OR this signature changes. The signature therefore
    captures every setting that can make a *new* escalation tier become available
    at runtime — flip it and previously dead-lettered documents become retryable.

    Derived purely from settings (not the live ``ProcessorRegistry``) so it is
    identical across the API/scanner and worker roles: the *registered* processor
    set is build-constant, so the only runtime variables are the OCR-enabled gate
    (``document_ocr_enabled`` — the single tier toggle today) and the tier-1 engine
    pin (``document_tier1_engine``). Enabling OCR changes the signature, so the
    pathological-but-OCR-recoverable documents dead-lettered while OCR was off are
    re-attempted automatically.

    TODO: when a future setting can make a previously-terminal document parseable,
    fold it in here so raising it auto-retries existing dead-letters. Two known
    candidates: a new escalation tier becoming toggleable (e.g. the reserved
    ``llm`` rung in ``TIER_LADDER``), and a raised oversize cap (an oversize PDF is
    always-terminal, so without the cap in this signature it stays dead-lettered
    until its etag changes even after an operator allows bigger files).
    """
    return (
        f"ocr={int(bool(settings.document_ocr_enabled))};"
        f"ocric={int(bool(settings.document_ocr_incluster_enabled))};"
        f"t1={settings.document_tier1_engine}"
    )


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
    reason: Literal["empty_text", "low_confidence", "corrupt_glyphs"]


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

    ``reason`` uses the existing escalation label vocabulary: ``empty_text``
    (scanned / no text layer), ``low_confidence`` (junk text layer), and
    ``corrupt_glyphs`` (a usable-looking layer whose extractor leaked raw glyph
    codes -- the broken-/ToUnicode case -- recovered by a different in-cluster
    extractor); ``unsupported`` and ``forced`` are reserved for future callers.
    """

    def __init__(self, *, from_tier: str, to_tier: str, reason: str) -> None:
        self.from_tier = from_tier
        self.to_tier = to_tier
        self.reason = reason
        super().__init__(
            f"escalate {from_tier}->{to_tier} (reason={reason})",
        )


class BatchPending(Exception):
    """Raised when a tier's work is in flight on an async backend and the worker
    should poll again later (Deck #332 — batch OCR).

    Like :class:`EscalateError` it is a **control-flow signal, NOT a failure**:
    the document's batch OCR job is still running on the gateway, so the OCR tier
    submits it (or polls an existing job) and raises this to ask the procrastinate
    retry strategy to re-run the SAME job on the SAME queue after ``retry_in``
    seconds — releasing the worker slot meanwhile so a multi-minute/hour batch
    doesn't pin a worker (and isn't reclaimed as a stalled ``doing`` job).

    It must propagate untouched to the retry strategy: never swallowed by a broad
    ``except Exception`` on the indexing path, never counted as a drop/parse
    error, and never marks the placeholder failed (the doc isn't done yet).
    Unlike ``EscalateError`` it does NOT change queue — the job stays on its own
    (``ocr-upstream``) tier queue and is simply deferred (batch mode is the
    upstream Mistral path only; the in-cluster rung is synchronous).
    """

    def __init__(self, *, retry_in: int) -> None:
        self.retry_in = retry_in
        super().__init__(f"batch OCR pending (retry_in={retry_in}s)")
