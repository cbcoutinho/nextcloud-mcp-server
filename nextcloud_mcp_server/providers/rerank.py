"""Cross-encoder reranking against a Cohere-protocol ``/rerank`` endpoint.

One wire format covers every backend we care about — Cohere itself
(``POST /v2/rerank``), a self-hosted `Infinity <https://github.com/michaelfeil/infinity>`_
(``POST /rerank``), a vLLM server (``POST /v1/rerank``), and the Astrolabe
embedding gateway (``POST /v1/rerank``): request ``{model, query, documents,
top_n}``, response ``{"results": [{"index", "relevance_score"}]}``. So this is a
single client and *which* reranker you use is configuration
(:func:`nextcloud_mcp_server.search.rerank.rerank_endpoint`), not a code path.

A plain-httpx client rather than a :class:`~.base.Provider`: the ``Provider`` ABC
is an embedding contract (``embed``/``embed_batch``/``get_dimension``) and a
reranker satisfies none of it. Mirrors :mod:`.gateway_batch`, the existing
precedent for a non-embedding upstream surface.
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
#      from the proxy, not as anything the reranker ever sees.
#   2. Cross-encoders truncate their input near the model's sequence limit
#      anyway, so text beyond roughly this length is not scored — trimming it
#      costs no ranking quality and buys proportional latency.
_MAX_DOCUMENT_CHARS = 2000

# The query shares the cross-encoder's sequence budget with each document, so an
# unbounded query would crowd out the text being scored. The HTTP search surface
# permits a 10,000-character query.
_MAX_QUERY_CHARS = 1000


class RerankError(Exception):
    """The reranker could not be used. Callers degrade to retrieval order rather
    than failing the search, so this is a signal to fall back, not to retry."""


@dataclass(frozen=True)
class RerankedIndex:
    """One reranked candidate: its position in the submitted list, and score."""

    index: int
    score: float


def _parse_entry(item: object, sent: int, seen: set[int]) -> RerankedIndex | None:
    """One ``results`` entry, or ``None`` if it cannot be trusted.

    Split out of :meth:`RerankClient._parse` to keep each piece simple
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


class RerankClient:
    """Scores query/document pairs with a cross-encoder over HTTP."""

    def __init__(
        self,
        url: str,
        model: str,
        token_provider: GatewayTokenProvider | None = None,
        *,
        api_key: str | None = None,
        timeout_seconds: float = _RERANK_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            url: The FULL rerank endpoint, e.g. ``http://infinity:7997/rerank``.
                Deliberately not a base URL to normalise: the path differs per
                backend (``/rerank``, ``/v1/rerank``, ``/v2/rerank``) and
                guessing wrong degrades silently to retrieval order rather than
                erroring. Callers derive it in one place — see
                :func:`nextcloud_mcp_server.search.rerank.rerank_endpoint`.
            model: Model id as the endpoint expects it. The Astrolabe gateway
                needs a ``<provider>/`` prefix; a direct Infinity/vLLM/Cohere
                endpoint wants the bare id.
            token_provider: OIDC client-credentials source, for the gateway.
            api_key: Static bearer (a Cohere key, an Infinity ``--api-key``).
                Takes precedence over ``token_provider`` when both are given.
        """
        self._url = url
        self._model = model
        self._token_provider = token_provider
        self._api_key = api_key
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    async def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
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
            # Clamp the connect timeout to the overall budget. SEARCH_RERANK_
            # TIMEOUT_SECONDS is validated only as > 0, so an operator setting
            # it to 1s would otherwise still spend up to 5s connecting — five
            # times the budget they configured — before the read budget even
            # starts. Immaterial at the 30s default; this only bites at
            # deliberately aggressive settings, which is exactly when a caller
            # is relying on the number they set.
            connect_timeout = min(_RERANK_CONNECT_TIMEOUT_SECONDS, self._timeout)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=connect_timeout)
            ) as client:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers=await self._headers(),
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            raise RerankError(
                f"rerank endpoint returned HTTP {e.response.status_code}"
            ) from e
        except Exception as e:  # transport, JSON decode, timeout
            raise RerankError(f"rerank request failed: {e}") from e

        return self._parse(body, len(documents))

    @staticmethod
    def _parse(body: object, sent: int) -> list[RerankedIndex]:
        """Turn a rerank response into a clean ranking over the submitted list.

        Deliberately defensive about the index set. A rerank backend
        may cap results, and a malformed or partial response must not silently
        delete candidates — dropping an entry here would look like a ranking
        change while actually being lost recall. Out-of-range and duplicate
        indices are discarded; anything the response omits is the CALLER's
        problem to re-append, and :meth:`rerank` guarantees only that every
        index returned is valid and unique.
        """
        if not isinstance(body, dict):
            raise RerankError(
                f"rerank endpoint returned {type(body).__name__}, not an object"
            )
        raw = body.get("results")
        if not isinstance(raw, list):
            raise RerankError("rerank response has no 'results' list")

        ranked: list[RerankedIndex] = []
        seen: set[int] = set()
        for item in raw:
            entry = _parse_entry(item, sent, seen)
            if entry is None:
                continue
            seen.add(entry.index)
            ranked.append(entry)

        if not ranked:
            raise RerankError("rerank endpoint returned no usable results")
        if len(ranked) < sent:
            # Not an error — the caller re-appends the remainder in retrieval
            # order — but it means part of the pool went unscored, which is
            # worth knowing when reranking appears to under-deliver.
            logger.debug(
                "rerank scored %d of %d submitted documents", len(ranked), sent
            )
        return ranked
