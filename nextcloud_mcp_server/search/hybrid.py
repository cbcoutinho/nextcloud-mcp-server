"""Hybrid search algorithm using Reciprocal Rank Fusion (RRF)."""

import asyncio
import logging
from collections import defaultdict
from typing import Any

from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult
from nextcloud_mcp_server.search.fuzzy import FuzzySearchAlgorithm
from nextcloud_mcp_server.search.keyword import KeywordSearchAlgorithm
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

logger = logging.getLogger(__name__)


class HybridSearchAlgorithm(SearchAlgorithm):
    """Hybrid search combining multiple algorithms using Reciprocal Rank Fusion.

    Implements RRF from ADR-003 to combine results from:
    - Semantic search (vector similarity)
    - Keyword search (token matching)
    - Fuzzy search (character overlap)

    RRF formula: score = weight / (k + rank)
    where k=60 (standard value) and rank is 1-indexed position.
    """

    DEFAULT_RRF_K = 60  # Standard RRF constant

    def __init__(
        self,
        semantic_weight: float = 0.5,
        keyword_weight: float = 0.3,
        fuzzy_weight: float = 0.2,
        rrf_k: int = DEFAULT_RRF_K,
    ):
        """Initialize hybrid search with algorithm weights.

        Args:
            semantic_weight: Weight for semantic results (default: 0.5)
            keyword_weight: Weight for keyword results (default: 0.3)
            fuzzy_weight: Weight for fuzzy results (default: 0.2)
            rrf_k: RRF constant for rank decay (default: 60)

        Raises:
            ValueError: If weights are invalid
        """
        # Validate weights
        if semantic_weight < 0 or keyword_weight < 0 or fuzzy_weight < 0:
            raise ValueError("Weights must be non-negative")

        total_weight = semantic_weight + keyword_weight + fuzzy_weight
        if total_weight > 1.0:
            raise ValueError(f"Weights sum to {total_weight:.2f}, must be ≤1.0")

        if total_weight == 0.0:
            raise ValueError("At least one weight must be > 0")

        self.semantic_weight = semantic_weight
        self.keyword_weight = keyword_weight
        self.fuzzy_weight = fuzzy_weight
        self.rrf_k = rrf_k
        self.total_weight = total_weight

        # Initialize sub-algorithms
        self.semantic = SemanticSearchAlgorithm()
        self.keyword = KeywordSearchAlgorithm()
        self.fuzzy = FuzzySearchAlgorithm()

    @property
    def name(self) -> str:
        return "hybrid"

    @property
    def requires_vector_db(self) -> bool:
        # Requires vector DB if semantic search has non-zero weight
        return self.semantic_weight > 0

    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        doc_type: str | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute hybrid search using RRF to combine algorithms.

        Returns unverified results from combined algorithms. Access verification
        should be performed separately at the final output stage.

        Args:
            query: Search query
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter
            **kwargs: Additional parameters passed to sub-algorithms

        Returns:
            List of unverified SearchResult objects ranked by RRF combined score
        """
        logger.info(
            f"Hybrid search: query='{query}', user={user_id}, limit={limit}, "
            f"weights=(semantic={self.semantic_weight}, keyword={self.keyword_weight}, "
            f"fuzzy={self.fuzzy_weight})"
        )

        # Run algorithms in parallel
        tasks = []
        algo_names = []

        if self.semantic_weight > 0:
            tasks.append(
                self.semantic.search(query, user_id, limit * 2, doc_type, **kwargs)
            )
            algo_names.append("semantic")

        if self.keyword_weight > 0:
            tasks.append(
                self.keyword.search(query, user_id, limit * 2, doc_type, **kwargs)
            )
            algo_names.append("keyword")

        if self.fuzzy_weight > 0:
            tasks.append(
                self.fuzzy.search(query, user_id, limit * 2, doc_type, **kwargs)
            )
            algo_names.append("fuzzy")

        # Execute searches in parallel
        results_list = await asyncio.gather(*tasks)

        # Build results dict
        algo_results = {}
        for algo_name, results in zip(algo_names, results_list):
            algo_results[algo_name] = results
            logger.debug(f"{algo_name} returned {len(results)} results")

        # Combine using RRF
        combined_results = self._reciprocal_rank_fusion(
            algo_results,
            {
                "semantic": self.semantic_weight,
                "keyword": self.keyword_weight,
                "fuzzy": self.fuzzy_weight,
            },
            limit,
        )

        logger.info(f"Hybrid search returned {len(combined_results)} combined results")
        if combined_results:
            result_details = [
                f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in combined_results[:5]
            ]
            logger.debug(f"Top hybrid results: {', '.join(result_details)}")

        return combined_results

    def _reciprocal_rank_fusion(
        self,
        algo_results: dict[str, list[SearchResult]],
        weights: dict[str, float],
        limit: int,
    ) -> list[SearchResult]:
        """Combine multiple ranked result lists using RRF.

        Args:
            algo_results: Dict of algorithm_name -> ranked results
            weights: Dict of algorithm_name -> weight (0-1)
            limit: Maximum results to return

        Returns:
            Combined and re-ranked results
        """
        # Track RRF scores per document
        rrf_scores: dict[tuple[int, str], float] = defaultdict(float)
        # Track best result object for each document
        best_results: dict[tuple[int, str], SearchResult] = {}

        for algo_name, results in algo_results.items():
            weight = weights.get(algo_name, 0.0)
            if weight == 0:
                continue

            for rank, result in enumerate(results, start=1):
                doc_key = (result.id, result.doc_type)

                # RRF formula: weight / (k + rank)
                rrf_score = weight / (self.rrf_k + rank)
                rrf_scores[doc_key] += rrf_score

                # Track best result object (prefer higher original scores)
                if doc_key not in best_results:
                    best_results[doc_key] = result
                elif result.score > best_results[doc_key].score:
                    best_results[doc_key] = result

        # Sort by combined RRF score
        sorted_docs = sorted(
            rrf_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:limit]

        # Calculate normalization factor to scale RRF scores to 0-1 range
        # Theoretical max RRF score = total_weight / (rrf_k + 1)
        # Normalization factor = (rrf_k + 1) / total_weight
        normalization_factor = (self.rrf_k + 1) / self.total_weight

        # Build final results with normalized RRF scores
        final_results = []
        for doc_key, rrf_score in sorted_docs:
            result = best_results[doc_key]

            # Normalize RRF score to 0-1 range for better user comprehension
            normalized_score = rrf_score * normalization_factor

            # Create new result with normalized score
            # Keep original metadata but add RRF details
            metadata = result.metadata or {}
            metadata["rrf_score_raw"] = rrf_score  # Original RRF score
            metadata["original_score"] = result.score  # Original algorithm score
            metadata["normalization_factor"] = normalization_factor

            final_results.append(
                SearchResult(
                    id=result.id,
                    doc_type=result.doc_type,
                    title=result.title,
                    excerpt=result.excerpt,
                    score=normalized_score,  # Use normalized score (0-1 range)
                    metadata=metadata,
                )
            )

        return final_results
