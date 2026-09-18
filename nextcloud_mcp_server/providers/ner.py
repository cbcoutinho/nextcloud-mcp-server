"""Person-name detection against the Astrolabe embedding gateway's ``/v1/ner``.

Wire format: request ``{model, texts, labels, threshold}``, response
``{"results": [{"index", "entities": [{"start", "end", "text", "label",
"score"}]}]}``. Offsets are character offsets into the submitted text.

A plain-httpx client, like :mod:`.rerank`: NER satisfies none of the embedding
``Provider`` contract. The client returns only what redaction needs — the person
surface forms found in each text — and it takes each form from the SUBMITTED
text via the offsets rather than trusting the echoed ``text`` field, so a
provider that normalises or truncates its echo cannot make us redact the wrong
string.

Unlike reranking, NER failure is never degraded around: a redacted read that
cannot detect names must return nothing rather than the raw text. So every
failure raises :class:`NerError` and callers fail closed.
"""

import httpx

from .gateway import GatewayTokenProvider

_NER_CONNECT_TIMEOUT_SECONDS = 5.0

# Longest text sent in one slot. Token-classification models see a few hundred
# tokens at a time, so the gateway windows internally anyway; this only bounds
# the request body. Longer inputs are split by :func:`windows` with an overlap,
# so a name straddling a cut is still seen whole in one window.
MAX_TEXT_CHARS = 2000
_WINDOW_OVERLAP_CHARS = 200

# Texts per request, to keep one body well under a typical 1 MB ingress limit.
_MAX_TEXTS_PER_REQUEST = 32

_PERSON_LABEL = "person"


class NerError(Exception):
    """Name detection failed. Callers must fail closed, never fall back to the
    unredacted text."""


def windows(text: str) -> list[str]:
    """Split ``text`` into overlapping slices of at most ``MAX_TEXT_CHARS``."""
    if len(text) <= MAX_TEXT_CHARS:
        return [text]
    step = MAX_TEXT_CHARS - _WINDOW_OVERLAP_CHARS
    return [text[i : i + MAX_TEXT_CHARS] for i in range(0, len(text), step)]


def _entity_text(entity: object, text: str) -> str | None:
    """The person surface form one entity names in ``text``, or ``None``."""
    if not isinstance(entity, dict) or entity.get("label") != _PERSON_LABEL:
        return None
    start, end = entity.get("start"), entity.get("end")
    # bool is an int subclass, so True would otherwise read as offset 1.
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not 0 <= start < end <= len(text):
        return None
    return text[start:end].strip() or None


class NerClient:
    """Detects person names in text over HTTP."""

    def __init__(
        self,
        url: str,
        model: str,
        token_provider: GatewayTokenProvider | None = None,
        *,
        threshold: float = 0.5,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._model = model
        self._token_provider = token_provider
        self._threshold = threshold
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    async def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def detect(self, texts: list[str]) -> list[set[str]]:
        """Person surface forms found in each of ``texts``, positionally.

        Each text must be at most ``MAX_TEXT_CHARS``; split longer ones with
        :func:`windows` first.

        Raises:
            NerError: transport failure, non-2xx, or a response that does not
                account for every submitted text.
        """
        found: list[set[str]] = []
        for i in range(0, len(texts), _MAX_TEXTS_PER_REQUEST):
            found.extend(
                await self._detect_batch(texts[i : i + _MAX_TEXTS_PER_REQUEST])
            )
        return found

    async def _detect_batch(self, texts: list[str]) -> list[set[str]]:
        if any(len(t) > MAX_TEXT_CHARS for t in texts):
            raise NerError(f"NER input over {MAX_TEXT_CHARS} chars; window it first")
        payload = {
            "model": self._model,
            "texts": texts,
            "labels": [_PERSON_LABEL],
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
        return self._parse(body, texts)

    @staticmethod
    def _parse(body: object, texts: list[str]) -> list[set[str]]:
        """Map a response onto the submitted texts.

        Strict where rerank is lenient: a text the response does not account
        for would be served as if it contained no names, i.e. unredacted. So a
        missing, duplicate or out-of-range index is an error, not a skip.
        """
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise NerError("NER response has no 'results' list")
        found: list[set[str] | None] = [None] * len(texts)
        for item in results:
            idx = item.get("index") if isinstance(item, dict) else None
            entities = item.get("entities") if isinstance(item, dict) else None
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or not 0 <= idx < len(texts)
                or found[idx] is not None
                or not isinstance(entities, list)
            ):
                raise NerError(f"NER response has an unusable result: {item!r}")
            found[idx] = {
                name for e in entities if (name := _entity_text(e, texts[idx]))
            }
        if any(f is None for f in found):
            raise NerError(
                f"NER response covered {sum(f is not None for f in found)} of "
                f"{len(texts)} texts"
            )
        return [f for f in found if f is not None]
