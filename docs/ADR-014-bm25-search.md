# ADR-014: Replace Custom Keyword Search with BM25 Hybrid Search via Qdrant

**Date:** 2025-11-16

**Status:** Implemented

---

### 1. Context

Our RAG application currently employs two separate retrieval mechanisms:
1.  **Dense (Semantic) Search:** Using vector embeddings stored in our Qdrant database to find semantically similar context.
2.  **Keyword Search:** A custom-built fuzzy/character-based search to match-specific keywords, acronyms, and product codes that semantic search often misses.

This dual-system approach has several drawbacks:
* **Poor Relevance:** Our current keyword search is basic (e.g., `LIKE` queries or simple fuzzy matching). It is not as effective as modern full-text search algorithms like BM25.
* **Clunky Fusion:** We lack a robust, principled method to combine the results from the two systems. This leads to disjointed logic in the application layer and suboptimal context being passed to the LLM.
* **Architectural Complexity:** We must maintain two separate search pathways (one to Qdrant, one to the keyword search mechanism), increasing code complexity and maintenance overhead.

Our vector database, **Qdrant**, natively supports **hybrid search** by combining dense vectors with BM25-based **sparse vectors** in a single collection.

### 2. Decision

We will **deprecate and remove** the existing custom keyword/fuzzy search functionality.

We will **replace it by implementing native hybrid search within Qdrant**. This involves:
1.  **Modifying the Qdrant Collection:** Updating our collection to support a named sparse vector index configured for BM25.
2.  **Updating the Ingestion Pipeline:** For every document chunk, we will generate and upsert *both*:
    * Its **dense vector** (from our existing embedding model).
    * Its **sparse vector** (generated using a BM25-compatible model, e.g., `Qdrant/bm25` from `fastembed`).
3.  **Refactoring Retrieval Logic:** All retrieval calls will be consolidated into a single Qdrant query using the `query_points` endpoint. This query will use the `prefetch` parameter to execute both dense and sparse searches, and Qdrant's built-in **Reciprocal Rank Fusion (RRF)** to automatically merge the results into a single, relevance-ranked list.
4.  **Backfilling:** A one-time migration script will be created to generate and add sparse vectors for all existing documents in the Qdrant collection.

---

### 3. Considered Options

#### Option 1: Native Qdrant Hybrid Search (Chosen)
* Use Qdrant's built-in sparse vector and RRF capabilities.
* **Pros:**
    * **Consolidated Architecture:** Manages both dense and sparse indexes in one database.
    * **No Data Sync Issues:** Updates are atomic. A single `upsert` updates both representations.
    * **Built-in Fusion:** RRF is handled natively and efficiently by the database.
    * **Superior Relevance:** Replaces our brittle custom search with the industry-standard BM25.
* **Cons:**
    * Requires a one-time data backfill which may be time-consuming.
    * Adds a new step (sparse vector generation) to the ingestion pipeline.

#### Option 2: External Full-Text Search (e.g., Elasticsearch)
* Keep Qdrant for dense search and add a separate Elasticsearch/OpenSearch cluster for BM25.
* **Pros:**
    * Provides a very powerful, dedicated full-text search engine.
* **Cons:**
    * **High Complexity:** Introduces a new, stateful service to deploy, manage, and scale.
    * **Data Sync Nightmare:** We would be responsible for ensuring that the document IDs and content in Qdrant and Elasticsearch are always perfectly synchronized. This is a major source of bugs.
    * **Manual Fusion:** The application would have to query both systems and perform RRF manually.

#### Option 3: Keep Current System
* Make no changes.
* **Pros:**
    * No engineering effort required.
* **Cons:**
    * Fails to address the known relevance and architectural problems.
    * Our RAG application's performance will remain suboptimal, especially for keyword-sensitive queries.

---

### 4. Rationale

**Option 1 is the clear winner.** It directly solves our primary problem (poor keyword matching) by adopting the industry-standard BM25.

Critically, it achieves this while **simplifying** our overall architecture, not complicating it. By leveraging features already present in our existing database (Qdrant), we avoid the massive operational and synchronization overhead of adding a second search system (Option 2).

This decision consolidates our retrieval logic, eliminates the data consistency problem, and moves the complex fusion logic (RRF) from the application layer into the database, where it can be performed more efficiently.

### 5. Consequences

**New Work:**
* **Ingestion:** The data ingestion pipeline must be updated to add the `fastembed` library (or similar), generate sparse vectors, and upsert them to the new named vector field in Qdrant.
* **Retrieval:** The application's retrieval service must be refactored to use the `query_points` endpoint with `prefetch` and `fusion=models.Fusion.RRF`.
* **Migration:** A one-time backfill script must be written and executed to add sparse vectors for all existing documents.
* **Infrastructure:** The Qdrant collection schema must be updated (or re-created) to add the `sparse_vectors_config`.

**Positive:**
* **Improved Accuracy:** Retrieval will be significantly more accurate, handling both semantic and keyword queries robustly.
* **Simplified Code:** The application's retrieval logic will be cleaner and simpler, with one endpoint instead of two.
* **Reduced Maintenance:** We will remove the custom fuzzy-search code, which is brittle and difficult to maintain.

**Negative:**
* The data backfill process will require careful management to avoid downtime.
* Ingestion time will slightly increase due to the extra step of sparse vector generation. This is considered a negligible trade-off for the gains in relevance.

---

### 6. Implementation Notes

**Implementation completed on 2025-11-16**

**Key Changes:**

1. **Dependencies** (pyproject.toml:25):
   - Added `fastembed>=0.4.2` for BM25 sparse vector embeddings
   - Adjusted `pillow` version constraint to be compatible with fastembed

2. **Qdrant Collection Schema** (nextcloud_mcp_server/vector/qdrant_client.py:113-128):
   - Updated to named vectors: `{"dense": VectorParams(...), "sparse": SparseVectorParams(...)}`
   - Added sparse vector configuration with BM25 index
   - Maintains backward compatibility with existing collections (detects legacy schema)

3. **BM25 Embedding Provider** (nextcloud_mcp_server/embedding/bm25_provider.py):
   - Created `BM25SparseEmbeddingProvider` using FastEmbed's `Qdrant/bm25` model
   - Implements `encode()` and `encode_batch()` methods
   - Returns sparse vectors as `{indices: list[int], values: list[float]}` format

4. **Document Indexing Pipeline** (nextcloud_mcp_server/vector/processor.py:229-255):
   - Generates both dense (semantic) and sparse (BM25) embeddings for each document chunk
   - Updates `PointStruct` to use named vectors: `vector={"dense": ..., "sparse": ...}`
   - Maintains same chunking strategy (512 words, 50-word overlap)

5. **BM25 Hybrid Search Algorithm** (nextcloud_mcp_server/search/bm25_hybrid.py):
   - Implements `BM25HybridSearchAlgorithm` using Qdrant's native RRF fusion
   - Uses `prefetch` parameter for parallel dense + sparse search
   - Applies `fusion=models.Fusion.RRF` for automatic result merging
   - Maintains same deduplication and filtering logic as semantic search

6. **MCP Tool Updates** (nextcloud_mcp_server/server/semantic.py:39-68):
   - Simplified `nc_semantic_search()` to use BM25 hybrid only
   - Removed `algorithm`, `semantic_weight`, `keyword_weight`, `fuzzy_weight` parameters
   - Updated default `score_threshold=0.0` for RRF scoring
   - Returns `search_method="bm25_hybrid"` in responses

7. **Legacy Algorithm Removal**:
   - Deleted `nextcloud_mcp_server/search/keyword.py` (278 lines)
   - Deleted `nextcloud_mcp_server/search/fuzzy.py` (220 lines)
   - Deleted `nextcloud_mcp_server/search/hybrid.py` (238 lines - custom RRF)
   - Updated `nextcloud_mcp_server/search/__init__.py` to export only BM25 hybrid

**Migration Strategy:**
- No migration required (vector sync feature is experimental)
- New documents automatically indexed with both dense + sparse vectors
- Collection re-creation on first startup with updated schema

**Test Results:**
- All unit tests passing (118 passed)
- All integration tests passing (7 semantic search tests)
- Code formatting verified with ruff

**Benefits Realized:**
- ✅ Consolidated architecture (single Qdrant database for both dense + sparse)
- ✅ Native RRF fusion (database-level, more efficient)
- ✅ Industry-standard BM25 (replaces custom keyword search)
- ✅ Simplified codebase (removed 736 lines of legacy code)
- ✅ Better relevance (handles both semantic and keyword queries)
