"""Cross-encoder reranking against the embedding gateway's ``/v1/rerank``.

A plain-httpx sub-client rather than a :class:`~.base.Provider`: the ``Provider``
ABC is an embedding contract (``embed``/``embed_batch``/``get_dimension``) and a
reranker satisfies none of it. Mirrors :mod:`.gateway_batch`, which is the
existing precedent for a gateway surface that is not an embedding provider.

The gateway takes a namespaced model id, so choosing a self-hosted versus a
hosted reranker is configuration rather than a code path here.
"""

import logging
from dataclasses import dataclass

import httpx

from .gateway import GatewayTokenProvider

logger = logging.getLogger(__name__)

# Reranking a deep pool is a single forward pass per document, so the request
# timeout is sized for the pool rather than for a control-plane round-trip. The
# caller layers its own configured timeout on top and degrades on expiry; this
# is the transport-level backstop.
_RERANK_CONNECT_TIMEOUT_SECONDS = 5.0
_RERANK_REQUEST_TIMEOUT_SECONDS = 120.0

# Per-document character budget for the request body.
#
# Two independent reasons, both real:
#   1. ``document_chunk_size`` is operator-configurable with no upper bound, so
#      "pool size x chunk size" is an unbounded request body. At a large chunk
#      size a full pool exceeds a typical 1 MB ingress limit and fails as a 413
#      from the proxy, not as anything the gateway ever sees.
#   2. Cross-encoders truncate their input near the model's sequence limit
#      anyway, so text beyond roughly this length is not scored — trimming it
#      costs no ranking quality and buys proportional latency.
_MAX_DOCUMENT_CHARS = 2000

# The query shares the cross-encoder's sequence budget with each document, so an
# unbounded query would crowd out the text being scored. The HTTP search surface
# permits a 10,000-character query.
_MAX_QUERY_CHARS = 1000


class RerankError(Exception):
    """The gateway could not rerank. Callers degrade to retrieval order rather
    than failing the search, so this is a signal to fall back, not to retry."""


@dataclass(frozen=True)
class RerankedIndex:
    """One reranked candidate: its position in the submitted list, and score."""

    index: int
    score: float


def _parse_entry(item: object, sent: int, seen: set[int]) -> RerankedIndex | None:
    """One ``results`` entry, or ``None`` if it cannot be trusted.

    Split out of :meth:`GatewayRerankClient._parse` to keep each piece simple
    enough to read as a single rule (and under the project's cognitive-complexity
    gate). Every rejection here is a case a provider has been observed to
    produce or could plausibly produce:

    * ``bool`` is an ``int`` subclass in Python, so ``True`` would otherwise be
      accepted as index 1 — and silently reorder a result;
    * an out-of-range index would address past the caller's list;
    * a duplicate would let one candidate occupy two result slots.
    """
    if not isinstance(item, dict):
        return None
    idx = item.get("index")
    if not isinstance(idx, int) or isinstance(idx, bool):
        return None
    if not 0 <= idx < sent or idx in seen:
        return None
    score = item.get("relevance_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    return RerankedIndex(index=idx, score=float(score))


class GatewayRerankClient:
    """Scores query/document pairs with a cross-encoder via the gateway."""

    def __init__(
        self,
        base_url: str,
        model: str,
        token_provider: GatewayTokenProvider | None = None,
        *,
        timeout_seconds: float = _RERANK_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        # EMBEDDING_GATEWAY_URL is a bare origin; rerank lives under /v1 like the
        # rest of the gateway API. Idempotent if already /v1-suffixed.
        base = base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self._base = base
        self._model = model
        self._token_provider = token_provider
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    async def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def rerank(self, query: str, documents: list[str]) -> list[RerankedIndex]:
        """Rank ``documents`` against ``query``, best first.

        Returns a ranking over the SUBMITTED list, expressed as indices into it.
        The caller maps those back onto its own objects, so this never has to
        know about search results.

        Raises:
            RerankError: transport failure, non-2xx, or an unusable body. The
                caller falls back to retrieval order.
        """
        if not documents:
            return []

        # Truncation is silent to the caller, so say something here: a long
        # query or long chunks produce a ranking computed on less text than the
        # caller thinks it submitted, and without this there is no signal to
        # explain a surprising order.
        over_len = sum(1 for d in documents if len(d) > _MAX_DOCUMENT_CHARS)
        if over_len or len(query) > _MAX_QUERY_CHARS:
            logger.debug(
                "rerank input truncated: query %d->%d chars, %d/%d documents "
                "over %d chars",
                len(query),
                min(len(query), _MAX_QUERY_CHARS),
                over_len,
                len(documents),
                _MAX_DOCUMENT_CHARS,
            )

        payload = {
            "model": self._model,
            "query": query[:_MAX_QUERY_CHARS],
            "documents": [d[:_MAX_DOCUMENT_CHARS] for d in documents],
            # Ask for a full ranking: the caller decides how deep to cut, and a
            # provider-side top_n would silently drop the tail we still need to
            # re-append in retrieval order.
            "top_n": len(documents),
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._timeout, connect=_RERANK_CONNECT_TIMEOUT_SECONDS
                )
            ) as client:
                resp = await client.post(
                    f"{self._base}/rerank",
                    json=payload,
                    headers=await self._headers(),
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            raise RerankError(
                f"gateway rerank returned HTTP {e.response.status_code}"
            ) from e
        except Exception as e:  # transport, JSON decode, timeout
            raise RerankError(f"gateway rerank failed: {e}") from e

        return self._parse(body, len(documents))

    @staticmethod
    def _parse(body: object, sent: int) -> list[RerankedIndex]:
        """Turn a rerank response into a clean ranking over the submitted list.

        Deliberately defensive about the index set. Providers behind the gateway
        may cap results, and a malformed or partial response must not silently
        delete candidates — dropping an entry here would look like a ranking
        change while actually being lost recall. Out-of-range and duplicate
        indices are discarded; anything the response omits is the CALLER's
        problem to re-append, and :meth:`rerank` guarantees only that every
        index returned is valid and unique.
        """
        if not isinstance(body, dict):
            raise RerankError(
                f"gateway rerank returned {type(body).__name__}, not an object"
            )
        raw = body.get("results")
        if not isinstance(raw, list):
            raise RerankError("gateway rerank response has no 'results' list")

        ranked: list[RerankedIndex] = []
        seen: set[int] = set()
        for item in raw:
            entry = _parse_entry(item, sent, seen)
            if entry is None:
                continue
            seen.add(entry.index)
            ranked.append(entry)

        if not ranked:
            raise RerankError("gateway rerank returned no usable results")
        if len(ranked) < sent:
            # Not an error — the caller re-appends the remainder in retrieval
            # order — but it means part of the pool went unscored, which is
            # worth knowing when reranking appears to under-deliver.
            logger.debug(
                "rerank scored %d of %d submitted documents", len(ranked), sent
            )
        return ranked
