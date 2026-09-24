"""Thin async clients for the eval harness.

The embedding gateway and Ollama are reached directly over the tailnet with the
OpenAI-compatible wire format the repo's providers use — but *without* the M2M
OIDC handshake that ``GatewayProvider`` enforces, because the dev gateway is
open on the tailnet (no bearer). This keeps the harness runnable without gateway
client-credentials. Production still uses ``GatewayProvider`` + M2M; the dense
vectors are byte-identical because the ``/v1/embeddings`` contract is the same.

Rate limits (mistral-embed: 12 req/s, 20M tok/min) are respected by bounding
concurrency with an ``anyio.CapacityLimiter`` supplied by the caller.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from fastembed import SparseTextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

logger = logging.getLogger(__name__)

# Gateway/Ollama calls are flaky at scale (a sweep makes thousands): transient
# 502/503/504, read timeouts, and — under high embed concurrency on an aged
# connection pool — raw ``ssl.SSLError`` ("passed invalid argument") that httpx
# does NOT wrap as a TransportError. Retry all of them so one blip doesn't abort
# a whole task group; the harness runs unattended for many minutes.
_RETRY_STATUS = {500, 502, 503, 504, 429}
_RETRY_EXC = (httpx.TransportError, ssl.SSLError)
_MAX_ATTEMPTS = 5


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    """POST JSON with bounded exponential backoff on transient failures."""
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
        except (*_RETRY_EXC, httpx.HTTPStatusError) as exc:
            # Only retry transient transport/SSL errors or retryable status codes.
            if isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code not in _RETRY_STATUS
            ):
                raise
            last = exc
            if attempt < _MAX_ATTEMPTS - 1:
                await anyio.sleep(1.5 * (2**attempt))
                logger.warning("retry %s %s (attempt %d)", url, exc, attempt + 1)
    assert last is not None
    raise last


@dataclass
class EmbedderSpec:
    """A dense embedding model under test."""

    name: str  # human label, e.g. "mistral-embed"
    kind: str  # "gateway" | "ollama"
    model: str  # provider model id, e.g. "mistral/mistral-embed"
    dim: int  # vector dimension (drives Qdrant density / €/GiB)

    @property
    def slug(self) -> str:
        """Filesystem/collection-safe identifier."""
        return self.name.replace("/", "_").replace(":", "-").replace(".", "-")


class GatewayEmbedder:
    """Dense embeddings via the gateway ``POST /v1/embeddings`` (OpenAI format)."""

    def __init__(
        self, client: httpx.AsyncClient, model: str, limiter: anyio.CapacityLimiter
    ):
        self._client = client
        self._model = model
        self._limiter = limiter

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        async with self._limiter:
            data = await _post_json(
                self._client, "/v1/embeddings", {"model": self._model, "input": texts}
            )
        vectors = [row["embedding"] for row in data["data"]]
        tokens = int(data.get("usage", {}).get("total_tokens", 0))
        return vectors, tokens


class OllamaEmbedder:
    """Dense embeddings via a local Ollama ``POST /api/embed`` (self-hosted)."""

    def __init__(
        self, client: httpx.AsyncClient, model: str, limiter: anyio.CapacityLimiter
    ):
        self._client = client
        self._model = model
        self._limiter = limiter

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        async with self._limiter:
            data = await _post_json(
                self._client, "/api/embed", {"model": self._model, "input": texts}
            )
        vectors = data["embeddings"]
        tokens = int(data.get("prompt_eval_count", 0))
        return vectors, tokens


class GatewayReranker:
    """Cross-encoder reranking via the gateway ``POST /v1/rerank`` (Cohere shape)."""

    def __init__(
        self, client: httpx.AsyncClient, model: str, limiter: anyio.CapacityLimiter
    ):
        self._client = client
        self._model = model
        self._limiter = limiter
        self.label = model

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """Return ``(original_index, relevance_score)`` pairs, best first."""
        async with self._limiter:
            data = await _post_json(
                self._client,
                "/v1/rerank",
                {
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                },
            )
        return [(int(r["index"]), float(r["relevance_score"])) for r in data["results"]]


class LocalCrossEncoderReranker:
    """Cheap CPU cross-encoder via fastembed (the note-390460 control).

    Default model ``Xenova/ms-marco-MiniLM-L-6-v2`` — the exact reranker that
    *hurt* finance tables, kept as an A/B control against the strong gateway one.
    """

    def __init__(self, model: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
        self._encoder = TextCrossEncoder(model_name=model)
        self.label = model

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        scores = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            lambda: list(self._encoder.rerank(query, documents))
        )
        ranked = sorted(enumerate(scores), key=lambda t: t[1], reverse=True)
        return [(idx, float(score)) for idx, score in ranked[:top_n]]


class Bm25Encoder:
    """FastEmbed BM25 sparse vectors — the SAME model production uses (Qdrant/bm25)."""

    def __init__(self, model_name: str = "Qdrant/bm25"):
        self._model = SparseTextEmbedding(model_name=model_name)

    async def encode(self, texts: list[str]) -> list[dict[str, Any]]:
        """Return ``[{"indices": list[int], "values": list[float]}, ...]``."""
        embeddings = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            lambda: list(self._model.embed(texts))
        )
        return [
            {"indices": e.indices.tolist(), "values": e.values.tolist()}
            for e in embeddings
        ]


class GatewayChat:
    """Text generation via the gateway ``POST /v1/chat/completions``."""

    def __init__(
        self, client: httpx.AsyncClient, model: str, limiter: anyio.CapacityLimiter
    ):
        self._client = client
        self._model = model
        self._limiter = limiter

    async def generate(self, prompt: str, *, max_tokens: int = 128) -> str:
        async with self._limiter:
            data = await _post_json(
                self._client,
                "/v1/chat/completions",
                {
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                },
            )
        return data["choices"][0]["message"]["content"]


def make_embedder(
    spec: EmbedderSpec,
    *,
    gateway: httpx.AsyncClient,
    ollama: httpx.AsyncClient,
    limiter: anyio.CapacityLimiter,
) -> GatewayEmbedder | OllamaEmbedder:
    if spec.kind == "gateway":
        return GatewayEmbedder(gateway, spec.model, limiter)
    if spec.kind == "ollama":
        return OllamaEmbedder(ollama, spec.model, limiter)
    raise ValueError(f"unknown embedder kind: {spec.kind!r}")


def resolve_dim(spec: EmbedderSpec, vectors: list[list[Any]]) -> int:
    """Trust the live vector length over the declared dim (defends against drift)."""
    if vectors and vectors[0]:
        return len(vectors[0])
    return spec.dim
