"""PCA coordinate computation for search-result visualization.

Used by the OAuth bearer-token search endpoints in ``api/visualization.py``
(``/api/v1/search`` and ``/api/v1/vector-viz/search``).

Note that ``auth/viz_routes.py`` (session-based auth) does *not* call this
module — it carries its own inline copy of the same retrieve/normalize/PCA
sequence. Both go through :class:`~nextcloud_mcp_server.vector.pca.PCA`, so
changes to the projection itself apply to both, but anything changed here
(spans, payload fields, guards) must be mirrored there by hand until the two
are deduplicated.
"""

import logging
from typing import Any

import anyio.to_thread
import numpy as np

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.vector.pca import PCA
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def compute_pca_coordinates(
    search_results: list[Any],
    query_embedding: np.ndarray | list[float],
) -> dict[str, Any]:
    """Compute PCA 3D coordinates for search results visualization.

    Retrieves the result vectors from Qdrant and applies PCA dimensionality
    reduction. Called only by the OAuth bearer-token search endpoints in
    ``api/visualization.py`` — ``auth/viz_routes.py`` runs its own inline copy
    of this sequence rather than calling here (see the module docstring).

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
    with trace_operation(
        "search.pca_retrieve_vectors", {"pca.num_point_ids": len(point_ids)}
    ):
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
                # SearchResult.id is str; coerce payload doc_id to match so the
                # tuple lookup below succeeds even on legacy int-typed payloads.
                raw_doc_id = point.payload.get("doc_id")
                doc_id = None if raw_doc_id is None else str(raw_doc_id)
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

    logger.info("Detected embedding dimension: %s", embedding_dim)

    # Build chunk vectors array in search_results order (1:1 mapping)
    chunk_vectors = []
    for result in search_results:
        chunk_key = (result.id, result.chunk_start_offset, result.chunk_end_offset)
        if chunk_key in chunk_vectors_map:
            chunk_vectors.append(chunk_vectors_map[chunk_key])
        else:
            # No dense vector for this chunk — expected for keyword-only results
            # (``keyword-index`` tag), which carry a sparse vector only and so
            # can't be positioned by PCA. Placed at the origin; hybrid chunks are
            # unaffected.
            logger.debug(
                "Chunk %s has no dense vector (keyword-only?); placing at origin",
                chunk_key,
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
            "Found %s zero-norm vectors at indices %s. Replacing with small epsilon to avoid division by zero.",
            zero_norm_mask.sum(),
            zero_indices.tolist(),
        )
        # Replace zero norms with small epsilon to avoid NaN
        norms[zero_norm_mask] = 1e-10

    all_vectors_normalized = all_vectors / norms
    logger.info(
        "Normalized vectors: query_norm=%s, doc_norm_range=[%s, %s]",
        format(norms[-1][0], ".3f"),
        format(norms[:-1].min(), ".3f"),
        format(norms[:-1].max(), ".3f"),
    )

    # Apply PCA dimensionality reduction (768-dim → 3D)
    # Run in thread pool to avoid blocking the event loop (CPU-bound)
    def _compute_pca(vectors: np.ndarray) -> tuple[np.ndarray, PCA]:
        pca = PCA(n_components=3)
        coords = pca.fit_transform(vectors)
        return coords, pca

    with trace_operation(
        "search.pca_compute",
        {
            "pca.num_samples": int(all_vectors_normalized.shape[0]),
            "pca.num_features": int(all_vectors_normalized.shape[1]),
            "pca.n_components": 3,
        },
    ):
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
            "Found NaN values in PCA output at %s points: %s. Replacing NaN with 0.0 to prevent JSON serialization error.",
            len(nan_rows),
            nan_rows.tolist()[:10],
        )
        # Replace NaN with 0 to allow JSON serialization
        coords_3d = np.nan_to_num(coords_3d, nan=0.0)

    # Split query coords from chunk coords
    # Round to 2 decimal places for cleaner display
    query_coords_3d = [round(float(x), 2) for x in coords_3d[-1]]  # Last point is query
    chunk_coords_3d = coords_3d[:-1]  # All but last are chunks

    logger.info(
        "PCA explained variance: PC1=%s, PC2=%s, PC3=%s",
        format(pca.explained_variance_ratio_[0], ".3f"),
        format(pca.explained_variance_ratio_[1], ".3f"),
        format(pca.explained_variance_ratio_[2], ".3f"),
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
