"""Tier-escalation ladder + signal for the per-tier ingest fleet (Deck #323).

The escalation ladder is the cheapest-first ordering of extraction tiers:

    fast  ->  structured  ->  ocr   ( ->  llm, reserved)

It mirrors the ``tier`` vocabulary documented on
:meth:`DocumentProcessor.tier <.base.DocumentProcessor.tier>` and the
observability label set. On the *external* (procrastinate) ingest path each tier
runs on its own queue + worker fleet; a document that a tier cannot parse well --
or cannot parse at all (a hard timeout/OOM failure, #399) -- is **requeued onto
the next tier's queue** rather than dropped or escalated inline. The
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

# Cheapest-first. ``ocr`` is the single configurable OCR tier: the backend (direct
# Mistral vs the embedding gateway, which can route Mistral, surya, etc.) and the
# model are chosen by ``document_ocr_provider`` + ``document_ocr_model``. ``llm`` is
# reserved (see base.DocumentProcessor.tier) and not wired yet.
TIER_LADDER: tuple[str, ...] = ("fast", "structured", "ocr")


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
    (``document_ocr_enabled`` — the single OCR-tier toggle) and the tier-1 engine
    pin (``document_tier1_engine``). Enabling OCR changes the signature, so the
    pathological-but-OCR-recoverable documents dead-lettered while OCR was off are
    re-attempted automatically.

    ``document_max_pdf_size_mb`` is included for the same reason: an oversize PDF
    is always-terminal (``vector/processor.py`` never escalates ``oversize``), so
    without the cap here a document stays dead-lettered until its etag changes
    even after an operator allows bigger files -- and for an archive of scanned
    documents the etag never changes. Formatted with ``:g`` so ``50`` and ``50.0``
    fingerprint identically and a float-repr change cannot spuriously invalidate
    every dead letter.

    Note the blast radius: changing the cap invalidates ALL dead letters, not just
    oversize ones, so genuinely corrupt documents are re-attempted once too. That
    is the intended trade -- the alternative is a bespoke backfill path -- but it
    means a cap change is a thundering herd on a large tenant and should be rolled
    out one tenant at a time.

    ``document_markdown_max_pages`` is included for the same reason (Deck #399).
    A structured-tier ``timeout`` is terminal, and lowering the page ceiling makes
    a previously-timing-out document take the raw-text path and succeed -- so
    without the ceiling here it would stay dead-lettered until its etag changed,
    which for an archive of scanned documents never happens. Any change to the
    value invalidates dead letters, in either direction: raising it can also turn
    a text-only document back into a markdown parse. Formatted with ``:g`` for the
    same reason as the cap: a settings source that yields ``150.0`` instead of
    ``150`` must not change the fingerprint (and ``:d`` would raise on a float,
    breaking the dead-letter key for the whole tenant).

    TODO: when a future setting can make a previously-terminal document parseable,
    fold it in here so raising it auto-retries existing dead-letters. Known
    remaining candidate: a new escalation tier becoming toggleable (e.g. the
    reserved ``llm`` rung in ``TIER_LADDER``).
    """
    return (
        f"ocr={int(bool(settings.document_ocr_enabled))};"
        f"t1={settings.document_tier1_engine};"
        f"maxmb={settings.document_max_pdf_size_mb:g};"
        f"mdpages={settings.document_markdown_max_pages:g}"
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

    ``reason`` uses the post-parse quality vocabulary: ``empty_text``
    (scanned / no text layer), ``low_confidence`` (junk text layer), and
    ``corrupt_glyphs`` (a usable-looking layer whose extractor leaked raw glyph
    codes -- the broken-/ToUnicode case -- recovered by a different in-cluster
    extractor); ``unsupported`` and ``forced`` are reserved for future callers.

    A second caller (``vector/processor._index_document``, #399) reuses this
    signal to escalate a *hard parse failure* -- an isolated-worker timeout/OOM
    on a pathological PDF -- up the ladder instead of dropping the document, so
    ``reason`` may also carry a ``parse_failed_reason`` value (``timeout``,
    ``oom``, ``error``). The ``from_tier``/``to_tier`` hop is identical; only the
    reason label widens, which surfaces on
    ``astrolabe_document_escalation_total{reason}``.
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
    (``ocr``) tier queue and is simply deferred (batch mode routes through the
    embedding gateway's async Batch OCR job).
    """

    def __init__(self, *, retry_in: int) -> None:
        self.retry_in = retry_in
        super().__init__(f"batch OCR pending (retry_in={retry_in}s)")
