"""Shared visualization utilities for PCA coordinate computation.

Extracts the PCA coordinate computation logic used by both:
- viz_routes.py (session-based auth)
- management.py (OAuth bearer token auth)

Both endpoints need to compute 3D PCA coordinates for search results,
so this module provides the shared implementation.
"""

import logging
from typing import Any

import anyio.to_thread
import numpy as np

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.pca import PCA
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def compute_pca_coordinates(
    search_results: list[Any],
    query_embedding: np.ndarray | list[float],
) -> dict[str, Any]:
    """Compute PCA 3D coordinates for search results visualization.

    This is the shared implementation used by both viz_routes.py and
    the management API. It retrieves vectors from Qdrant and applies
    PCA dimensionality reduction.

    Args:
        search_results: List of SearchResult objects with point_id
        query_embedding: The query embedding vector

    Returns:
        Dict with:
            - coordinates_3d: List of [x, y, z] for each result
            - query_coords: [x, y, z] for the query point
            - pca_variance: Dict with pc1, pc2, pc3 explained variance ratios
    """
    settings = get_settings()

    # Collect point IDs from search results for batch retrieval
    point_ids = [r.point_id for r in search_results if r.point_id]

    if len(point_ids) < 2:
        return {"coordinates_3d": [], "query_coords": []}

    qdrant_client = await get_qdrant_client()

    # Batch retrieve vectors from Qdrant
    points_response = await qdrant_client.retrieve(
        collection_name=settings.get_collection_name(),
        ids=point_ids,
        with_vectors=["dense"],
        with_payload=["doc_id", "chunk_start_offset", "chunk_end_offset"],
    )

    # Build chunk_vectors_map from batch response
    chunk_vectors_map: dict[tuple[Any, Any, Any], Any] = {}
    for point in points_response:
        if point.vector is not None:
            # Extract dense vector (handle both named and unnamed vectors)
            if isinstance(point.vector, dict):
                vector = point.vector.get("dense")
            else:
                vector = point.vector

            if vector is not None and point.payload:
                doc_id = point.payload.get("doc_id")
                chunk_start = point.payload.get("chunk_start_offset")
                chunk_end = point.payload.get("chunk_end_offset")
                chunk_key = (doc_id, chunk_start, chunk_end)
                chunk_vectors_map[chunk_key] = vector

    if len(chunk_vectors_map) < 2:
        return {"coordinates_3d": [], "query_coords": []}

    # Detect embedding dimension
    embedding_dim = None
    for vector in chunk_vectors_map.values():
        if vector is not None:
            embedding_dim = len(vector)
            break

    if embedding_dim is None:
        return {"coordinates_3d": [], "query_coords": []}

    logger.info(f"Detected embedding dimension: {embedding_dim}")

    # Build chunk vectors array in search_results order (1:1 mapping)
    chunk_vectors = []
    for result in search_results:
        chunk_key = (result.id, result.chunk_start_offset, result.chunk_end_offset)
        if chunk_key in chunk_vectors_map:
            chunk_vectors.append(chunk_vectors_map[chunk_key])
        else:
            # Chunk not found in vectors (shouldn't happen)
            logger.warning(
                f"Chunk {chunk_key} not found in fetched vectors, using zero vector"
            )
            chunk_vectors.append(np.zeros(embedding_dim))

    chunk_vectors = np.array(chunk_vectors)

    # Ensure query_embedding is a numpy array
    if not isinstance(query_embedding, np.ndarray):
        query_embedding = np.array(query_embedding)

    # Combine query vector with chunk vectors for PCA
    # Query will be the last point in the array
    all_vectors = np.vstack([chunk_vectors, np.array([query_embedding])])

    # Normalize vectors to unit length (L2 normalization)
    # This is critical because Qdrant uses COSINE distance, which only measures
    # vector direction (angle), not magnitude. PCA uses Euclidean distance which
    # considers both direction and magnitude. By normalizing to unit length,
    # Euclidean distances in PCA space will match cosine distances.
    norms = np.linalg.norm(all_vectors, axis=1, keepdims=True)

    # Check for zero-norm vectors (can happen with empty/corrupted embeddings)
    zero_norm_mask = norms[:, 0] < 1e-10
    if zero_norm_mask.any():
        zero_indices = np.where(zero_norm_mask)[0]
        logger.warning(
            f"Found {zero_norm_mask.sum()} zero-norm vectors at indices "
            f"{zero_indices.tolist()}. Replacing with small epsilon to avoid "
            "division by zero."
        )
        # Replace zero norms with small epsilon to avoid NaN
        norms[zero_norm_mask] = 1e-10

    all_vectors_normalized = all_vectors / norms
    logger.info(
        f"Normalized vectors: query_norm={norms[-1][0]:.3f}, "
        f"doc_norm_range=[{norms[:-1].min():.3f}, {norms[:-1].max():.3f}]"
    )

    # Apply PCA dimensionality reduction (768-dim → 3D)
    # Run in thread pool to avoid blocking the event loop (CPU-bound)
    def _compute_pca(vectors: np.ndarray) -> tuple[np.ndarray, PCA]:
        pca = PCA(n_components=3)
        coords = pca.fit_transform(vectors)
        return coords, pca

    coords_3d, pca = await anyio.to_thread.run_sync(
        lambda: _compute_pca(all_vectors_normalized)
    )

    # After fit, these attributes are guaranteed to be set
    assert pca.explained_variance_ratio_ is not None

    # Check for NaN values in PCA output (numerical instability)
    nan_mask = np.isnan(coords_3d)
    if nan_mask.any():
        nan_rows = np.where(nan_mask.any(axis=1))[0]
        logger.error(
            f"Found NaN values in PCA output at {len(nan_rows)} points: "
            f"{nan_rows.tolist()[:10]}. Replacing NaN with 0.0 to prevent "
            "JSON serialization error."
        )
        # Replace NaN with 0 to allow JSON serialization
        coords_3d = np.nan_to_num(coords_3d, nan=0.0)

    # Split query coords from chunk coords
    # Round to 2 decimal places for cleaner display
    query_coords_3d = [round(float(x), 2) for x in coords_3d[-1]]  # Last point is query
    chunk_coords_3d = coords_3d[:-1]  # All but last are chunks

    logger.info(
        f"PCA explained variance: PC1={pca.explained_variance_ratio_[0]:.3f}, "
        f"PC2={pca.explained_variance_ratio_[1]:.3f}, "
        f"PC3={pca.explained_variance_ratio_[2]:.3f}"
    )

    # Coordinates already match search_results order (1:1 mapping)
    result_coords = [[round(float(x), 2) for x in coord] for coord in chunk_coords_3d]

    return {
        "coordinates_3d": result_coords,
        "query_coords": query_coords_3d,
        "pca_variance": {
            "pc1": float(pca.explained_variance_ratio_[0]),
            "pc2": float(pca.explained_variance_ratio_[1]),
            "pc3": float(pca.explained_variance_ratio_[2]),
        },
    }
