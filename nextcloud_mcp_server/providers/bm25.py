"""BM25 sparse embedding provider using FastEmbed."""

import logging
import unicodedata
from collections.abc import Sequence
from typing import Any

import anyio
from fastembed import SparseTextEmbedding
from fastembed.sparse.bm25 import Bm25

# A Rust extension that ships no type stubs, so ty cannot see its members.
from py_rust_stemmers import SnowballStemmer  # ty: ignore[missing-module-member]

from nextcloud_mcp_server.config import BM25_LANGUAGE_SCRIPTS, get_settings

logger = logging.getLogger(__name__)


def _script_of(word: str) -> str | None:
    """The Unicode script of a word's first letter (``"LATIN"``, ``"CYRILLIC"``,
    ...), or None when it has no letters (``"2026"``, ``"__"``)."""
    for ch in word:
        if ch.isalpha():
            return unicodedata.name(ch, "").split(" ", 1)[0] or None
    return None


class ScriptStemmer:
    """Stem each word with the Snowball stemmer of the language its script
    belongs to, for text that mixes languages (``BM25_LANGUAGES=russian,english``).

    FastEmbed's ``Bm25`` takes one stemmer for all tokens, so this dispatches per
    word. A word in a script no configured language uses (a CJK word, or a Latin
    one with only ``russian`` configured) is left unstemmed, as is a word with no
    letters. A word that mixes scripts is stemmed by its first letter's script.

    ``languages`` must already be validated (one per script, see
    ``parse_bm25_languages``).
    """

    def __init__(self, languages: Sequence[str]):
        self.languages = tuple(languages)
        self._stemmers = {
            BM25_LANGUAGE_SCRIPTS[lang]: SnowballStemmer(lang)
            for lang in self.languages
        }

    def __reduce__(self) -> tuple[type["ScriptStemmer"], tuple[tuple[str, ...]]]:
        # SnowballStemmer is a Rust object that cannot be pickled or deep-copied;
        # FastEmbed needs both for parallel embedding, so rebuild from languages.
        return (type(self), (self.languages,))

    def stem_word(self, word: str) -> str:
        stemmer = self._stemmers.get(_script_of(word) or "")
        return stemmer.stem_word(word) if stemmer is not None else word


def _build_model(model_name: str, languages: tuple[str, ...]) -> SparseTextEmbedding:
    """Build the FastEmbed BM25 model for the configured languages.

    One language uses FastEmbed's own ``language`` support unchanged, so the
    default (``english``) produces exactly the vectors it always has. Several
    use a :class:`ScriptStemmer` and the union of each language's stopwords.
    """
    if len(languages) == 1:
        return SparseTextEmbedding(model_name=model_name, language=languages[0])
    # Each language's bundled stopword list, loaded the way FastEmbed loads it
    # (the model files are already cached, so this costs no download).
    stopwords: set[str] = set()
    for lang in languages:
        stopwords |= Bm25(model_name=model_name, language=lang).stopwords
    return SparseTextEmbedding(
        model_name=model_name, stemmer=ScriptStemmer(languages), stopwords=stopwords
    )


class BM25SparseEmbeddingProvider:
    """
    BM25 sparse embedding provider for hybrid search.

    Uses FastEmbed's BM25 model to generate sparse vectors for keyword-based
    retrieval. These sparse vectors are combined with dense semantic vectors
    in Qdrant using Reciprocal Rank Fusion (RRF) for hybrid search.

    Unlike dense embeddings which have fixed dimensions, sparse embeddings
    have variable-length vectors with (index, value) pairs representing
    term frequencies in the BM25 vocabulary.
    """

    def __init__(
        self,
        model_name: str = "Qdrant/bm25",
        languages: Sequence[str] | None = None,
    ):
        """
        Initialize BM25 sparse embedding provider.

        Args:
            model_name: FastEmbed BM25 model name (default: Qdrant/bm25)
            languages: Validated stemming/stopword languages. Defaults to
                BM25_LANGUAGES from settings.
        """
        self.model_name = model_name
        self.languages = (
            tuple(languages)
            if languages is not None
            else get_settings().bm25_language_list
        )
        logger.info(
            "Initializing BM25 sparse embedding provider: %s (languages: %s)",
            model_name,
            ", ".join(self.languages),
        )

        self.model = _build_model(model_name, self.languages)
        logger.info("BM25 sparse embedding model loaded: %s", model_name)

    def encode(self, text: str) -> dict[str, Any]:
        """
        Generate BM25 sparse embedding for a single text (synchronous).

        Note: For async contexts, prefer encode_async() to avoid blocking the event loop.

        Args:
            text: Input text to encode

        Returns:
            Dictionary with 'indices' and 'values' keys for Qdrant sparse vector
        """
        # FastEmbed returns a generator, take first result
        sparse_embedding = next(iter(self.model.embed([text])))

        return {
            "indices": sparse_embedding.indices.tolist(),
            "values": sparse_embedding.values.tolist(),
        }

    async def encode_async(self, text: str) -> dict[str, Any]:
        """
        Generate BM25 sparse embedding for a single text (async).

        Runs CPU-bound BM25 encoding in thread pool to avoid blocking the event loop.

        Args:
            text: Input text to encode

        Returns:
            Dictionary with 'indices' and 'values' keys for Qdrant sparse vector
        """

        # Run CPU-bound BM25 encoding in thread pool
        return await anyio.to_thread.run_sync(lambda: self.encode(text))  # type: ignore[attr-defined]

    async def encode_batch(self, texts: list[str]) -> list[dict[str, Any]]:
        """
        Generate BM25 sparse embeddings for multiple texts (batched).

        Args:
            texts: List of texts to encode

        Returns:
            List of dictionaries with 'indices' and 'values' for each text
        """

        # Run CPU-bound BM25 encoding in thread pool to avoid blocking event loop
        sparse_embeddings = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            lambda: list(self.model.embed(texts))
        )

        return [
            {
                "indices": emb.indices.tolist(),
                "values": emb.values.tolist(),
            }
            for emb in sparse_embeddings
        ]


# BM25 sparse embedding singleton
_bm25_service: BM25SparseEmbeddingProvider | None = None


async def get_bm25_service() -> BM25SparseEmbeddingProvider:
    """
    Get singleton BM25 sparse embedding provider instance.

    Lazily instantiates the singleton off the event loop. The
    ``BM25SparseEmbeddingProvider`` constructor calls
    ``fastembed.SparseTextEmbedding(model_name="Qdrant/bm25")`` which
    downloads ~50 MB of model weights from HuggingFace and loads them
    into memory — observed >5 s wall-clock in production, enough to
    stall the calling thread. The encode methods on the provider
    already offload via ``anyio.to_thread.run_sync``; this routes the
    first-time init through the same path so the event loop stays
    responsive (kubernetes ``/health/live`` httpGet probe in
    particular).

    The singleton is process-wide so the per-pod cost is paid once.
    Subsequent calls hit the warm path and return after a single
    non-blocking await.

    Concurrent first callers race: two coroutines can both observe
    ``_bm25_service is None`` and both enter ``run_sync``. We accept
    the duplicate model load over an ``asyncio.Lock``, which has its
    own cross-loop hazards under anyio TaskGroups (see PR #799 for
    that class of bug). The duplicate is bounded — FastEmbed caches
    the downloaded weights on disk after the first call, so loser(s)
    pick up cheaply.

    Returns:
        Global BM25SparseEmbeddingProvider instance
    """
    global _bm25_service
    if _bm25_service is None:
        _bm25_service = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            BM25SparseEmbeddingProvider
        )
    return _bm25_service
