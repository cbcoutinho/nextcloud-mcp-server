"""Access verification for search results.

This module provides centralized verification of Nextcloud access permissions
for search results. Verification happens at the final output stage (MCP tool/viz endpoint)
rather than within individual search algorithms, preventing redundant API calls.

Key benefits:
- Deduplication: Each document verified exactly once (even in hybrid mode)
- Parallel execution: All verifications run concurrently via anyio task groups
- Separation of concerns: Algorithms handle scoring, this module handles security
"""

import logging
from dataclasses import replace
from typing import Protocol

import anyio

from nextcloud_mcp_server.search.algorithms import SearchResult

logger = logging.getLogger(__name__)


class NextcloudClientProtocol(Protocol):
    """Protocol for Nextcloud client with app-specific access."""

    @property
    def notes(self):
        """Notes client for accessing notes API."""
        ...


async def verify_search_results(
    results: list[SearchResult],
    nextcloud_client: NextcloudClientProtocol,
) -> list[SearchResult]:
    """
    Verify Nextcloud access for search results.

    Deduplicates by (doc_id, doc_type), verifies in parallel using anyio task groups,
    and filters out inaccessible documents. Maintains original result ordering.

    Args:
        results: Unverified search results from Qdrant
        nextcloud_client: Nextcloud client for access checks

    Returns:
        Verified and accessible results (same order as input)

    Example:
        >>> unverified = await search_algo.search(query="test", limit=10)
        >>> verified = await verify_search_results(unverified, client)
        >>> # verified contains only documents user can access
    """
    # Deduplicate by (doc_id, doc_type) while preserving order
    # This is critical for hybrid search where same doc may appear in multiple algorithm results
    seen = set()
    unique_results = []
    for result in results:
        key = (result.id, result.doc_type)
        if key not in seen:
            seen.add(key)
            unique_results.append(result)

    if not unique_results:
        return []

    logger.debug(
        f"Verifying access for {len(unique_results)} unique documents "
        f"(from {len(results)} total results)"
    )

    # Verify all unique documents in parallel using anyio task group
    # Use list to maintain order (index-based storage)
    verified_results = [None] * len(unique_results)

    async def verify_one(index: int, result: SearchResult):
        """
        Verify a single document and store result at index.

        Args:
            index: Position in verified_results list
            result: Search result to verify
        """
        try:
            if result.doc_type == "note":
                # Fetch note to verify access and get fresh metadata
                note = await nextcloud_client.notes.get_note(result.id)
                # Update metadata with fresh data from Nextcloud
                updated_metadata = {**(result.metadata or {}), **note}
                verified_results[index] = replace(result, metadata=updated_metadata)
            # TODO: Add verification for other doc types (calendar, deck, file, etc.)
            else:
                # For now, assume other types are accessible
                # In production, add proper verification for each type
                logger.debug(
                    f"No verification implemented for doc_type={result.doc_type}, "
                    "assuming accessible"
                )
                verified_results[index] = result

        except Exception as e:
            # Document is inaccessible (403, 404, or other error)
            # Log at debug level since this is expected for filtered results
            logger.debug(f"Document {result.doc_type}/{result.id} not accessible: {e}")
            verified_results[index] = None

    # Run all verifications in parallel using anyio task group
    # This provides structured concurrency with automatic cancellation on errors
    async with anyio.create_task_group() as tg:
        for idx, result in enumerate(unique_results):
            tg.start_soon(verify_one, idx, result)

    # Filter out None (inaccessible) and return verified results
    accessible = [r for r in verified_results if r is not None]

    logger.debug(
        f"Verification complete: {len(accessible)} accessible, "
        f"{len(unique_results) - len(accessible)} filtered out"
    )

    return accessible
