"""Person-name and address detection against the embedding gateway's ``/v1/ner``.

Wire format: request ``{model, texts, labels, threshold}``, response
``{"results": [{"index", "entities": [{"start", "end", "text", "label",
"score"}]}]}``. Offsets are character offsets into the submitted text.

A plain-httpx client, like :mod:`.rerank`: NER satisfies none of the embedding
``Provider`` contract. The client returns only what redaction needs — the
``(label, surface form)`` pairs found in each text — and it takes each form from the SUBMITTED
text via the offsets rather than trusting the echoed ``text`` field, so a
provider that normalises or truncates its echo cannot make us redact the wrong
string.

Unlike reranking, NER failure is never degraded around: a document whose names
cannot be detected must not be exported unredacted. So every failure raises
:class:`NerError` and callers fail closed.
"""

import re

import httpx

from .gateway import GatewayTokenProvider

_NER_CONNECT_TIMEOUT_SECONDS = 5.0

# Longest text sent in one slot. Token-classification models see a few hundred
# tokens at a time, so the gateway windows internally anyway; this only bounds
# the request body. Longer inputs are split by :func:`windows` with an overlap,
# so a name straddling a cut is still seen whole in one window.
MAX_TEXT_CHARS = 2000
_WINDOW_OVERLAP_CHARS = 200
_SPACE_RE = re.compile(r"\s+")

# Default texts per request. Also keeps one body well under a typical 1 MB
# ingress limit; NER_BATCH_SIZE tunes it per backend (small for CPU).
_DEFAULT_BATCH_SIZE = 8

PERSON_LABEL = "person"
ADDRESS_LABEL = "address"


class NerError(Exception):
    """Name detection failed. Callers must fail closed, never fall back to the
    unredacted text."""


def windows(text: str) -> list[str]:
    """Split ``text`` into overlapping slices of at most ``MAX_TEXT_CHARS``.

    Cuts fall on whitespace: a word cut in two ("…for Aca|demic") reads as a
    name to the model, and every detected name is redacted wherever it occurs.
    A run with no whitespace in reach is cut hard.
    """
    if len(text) <= MAX_TEXT_CHARS:
        return [text]
    out: list[str] = []
    start = 0
    while len(text) - start > MAX_TEXT_CHARS:
        end = start + MAX_TEXT_CHARS
        # Back off to the last whitespace, but no further than the overlap, so
        # the next window always starts after this one.
        runs = list(_SPACE_RE.finditer(text, start, end))
        if runs and runs[-1].start() > start + _WINDOW_OVERLAP_CHARS:
            end = runs[-1].start()
        out.append(text[start:end])
        # The next window reaches back an overlap, to the first word start in
        # it; past a word longer than the overlap, to the last one before it
        # (more overlap). It still starts after this window's start.
        back = end - _WINDOW_OVERLAP_CHARS
        starts = [m.end() for m in runs if start < m.end() <= end]
        start = next(
            (s for s in starts if s >= back),
            max((s for s in starts if s < back), default=back),
        )
    out.append(text[start:])
    return out


def _entity(
    entity: object, text: str, labels: tuple[str, ...]
) -> tuple[str, str] | None:
    """``(label, surface form)`` of one entity in ``text``, or ``None``."""
    if not isinstance(entity, dict) or entity.get("label") not in labels:
        return None
    start, end = entity.get("start"), entity.get("end")
    # bool is an int subclass, so True would otherwise read as offset 1. The
    # bool test comes first: after an isinstance(int) narrowing, static
    # analysers (Sonar S2583) wrongly treat a later bool test as always false.
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if not 0 <= start < end <= len(text):
        return None
    surface = text[start:end].strip()
    return (entity["label"], surface) if surface else None


class NerClient:
    """Detects named entities (people, addresses) in text over HTTP."""

    def __init__(
        self,
        url: str,
        model: str,
        token_provider: GatewayTokenProvider | None = None,
        *,
        threshold: float = 0.5,
        timeout_seconds: float = 30.0,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._url = url
        self._model = model
        self._token_provider = token_provider
        self._threshold = threshold
        self._timeout = timeout_seconds
        self._batch_size = max(1, batch_size)

    @property
    def model(self) -> str:
        return self._model

    async def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def detect(
        self, texts: list[str], labels: tuple[str, ...] = (PERSON_LABEL,)
    ) -> list[set[tuple[str, str]]]:
        """``(label, surface form)`` pairs found in each of ``texts``, positionally.

        Each text must be at most ``MAX_TEXT_CHARS``; split longer ones with
        :func:`windows` first.

        Raises:
            NerError: transport failure, non-2xx, or a response that does not
                account for every submitted text.
        """
        found: list[set[tuple[str, str]]] = []
        for i in range(0, len(texts), self._batch_size):
            found.extend(
                await self._detect_batch(texts[i : i + self._batch_size], labels)
            )
        return found

    async def _detect_batch(
        self, texts: list[str], labels: tuple[str, ...]
    ) -> list[set[tuple[str, str]]]:
        if any(len(t) > MAX_TEXT_CHARS for t in texts):
            raise NerError(f"NER input over {MAX_TEXT_CHARS} chars; window it first")
        payload = {
            "model": self._model,
            "texts": texts,
            "labels": list(labels),
            "threshold": self._threshold,
        }
        try:
            connect_timeout = min(_NER_CONNECT_TIMEOUT_SECONDS, self._timeout)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=connect_timeout)
            ) as client:
                resp = await client.post(
                    self._url, json=payload, headers=await self._headers()
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            raise NerError(
                f"NER endpoint returned HTTP {e.response.status_code}"
            ) from e
        except Exception as e:  # transport, JSON decode, timeout
            raise NerError(f"NER request failed: {e}") from e
        return self._parse(body, texts, labels)

    @staticmethod
    def _parse(
        body: object, texts: list[str], labels: tuple[str, ...]
    ) -> list[set[tuple[str, str]]]:
        """Map a response onto the submitted texts.

        Strict where rerank is lenient: a text the response does not account
        for would be served as if it contained no names, i.e. unredacted. So a
        missing, duplicate or out-of-range index is an error, not a skip.
        """
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise NerError("NER response has no 'results' list")
        found: list[set[tuple[str, str]] | None] = [None] * len(texts)
        for item in results:
            idx = item.get("index") if isinstance(item, dict) else None
            entities = item.get("entities") if isinstance(item, dict) else None
            if (
                isinstance(idx, bool)  # before the int test, as in _entity
                or not isinstance(idx, int)
                or not 0 <= idx < len(texts)
                or found[idx] is not None
                or not isinstance(entities, list)
            ):
                # Shape only, never the item: its entities carry the names this
                # client exists to keep out of anything that is logged or shown.
                count = len(entities) if isinstance(entities, list) else "no"
                raise NerError(
                    f"NER response has an unusable result: index {idx!r} "
                    f"with {count} entities"
                )
            found[idx] = {
                pair for e in entities if (pair := _entity(e, texts[idx], labels))
            }
        if any(f is None for f in found):
            raise NerError(
                f"NER response covered {sum(f is not None for f in found)} of "
                f"{len(texts)} texts"
            )
        return [f for f in found if f is not None]
