# ADR-003: Vector Database and Semantic Search Architecture

## Status
Superseded by ADR-007

**Note**: This ADR was never implemented. The core technical decisions (Qdrant, embeddings, hybrid search) remain valid and are incorporated into ADR-007, which adds user-controlled background job management, task queuing, multi-user scheduling, and web UI integration. See [ADR-007: Background Vector Sync with User-Controlled Job Management](./ADR-007-background-vector-sync-job-management.md) for the implemented architecture.

## Context

### Current State

ADR-001 introduced token-based keyword search with relevance ranking, which improved upon simple substring matching. However, this approach still has fundamental limitations:

1. **Lexical Matching Only**: Requires exact word matches (e.g., "automobile" won't match "car")
2. **No Semantic Understanding**: Cannot understand intent or context (e.g., "how to bake bread" won't match "bread recipe")
3. **Language Barriers**: Poor support for synonyms, related terms, or multilingual content
4. **No Cross-Content Search**: Cannot find related content across different apps (notes, files, calendar)
5. **Scaling Issues**: Performance degrades with large content collections

### User Needs

LLM-powered applications (Claude via MCP) benefit significantly from semantic search capabilities:

- **Context Discovery**: Find relevant information based on meaning, not just keywords
- **Knowledge Retrieval**: Retrieve contextually relevant notes/files for task completion
- **Cross-Referencing**: Connect related information across different content types
- **Natural Language Queries**: Support conversational search patterns

### Technical Requirements

1. **Multi-User Environment**: OAuth-based with per-user isolation and permissions
2. **Multi-Tenant**: Single deployment serving multiple users with strict data isolation
3. **Real-Time Search**: Sub-second query latency for good UX
4. **Large Content**: Support for documents, PDFs, images with text extraction
5. **Privacy**: No external API calls for sensitive content (optionally self-hosted)
6. **Hybrid Search**: Combine semantic and keyword search for best results

## Decision

We will implement **semantic search using a vector database** with the following architecture:

### Core Components

1. **Vector Database**: Qdrant as external sidecar service
2. **Embedding Strategy**: Configurable (OpenAI API / local models / self-hosted)
3. **Search Pattern**: Hybrid search (semantic + keyword fusion)
4. **Multi-Tenancy**: Single collection with user_id filtering
5. **Authorization**: Dual-phase (vector search + Nextcloud API verification)
6. **Sync Strategy**: Background worker with incremental updates (see ADR-002)

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    User Request (OAuth)                      │
│                    "find notes about baking"                 │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│               MCP Server (Semantic Search Tool)             │
│                                                              │
│  1. Generate query embedding                                │
│  2. Search vector DB (user_id filter)                       │
│  3. Verify permissions via Nextcloud API                    │
│  4. Return ranked results                                   │
└──────────┬─────────────────────────────┬────────────────────┘
           │                              │
           ▼                              ▼
┌──────────────────────┐      ┌──────────────────────────────┐
│ Embedding Service    │      │ Qdrant Vector Database        │
│ - OpenAI API         │      │                               │
│ - Local Model        │      │ Collection: nextcloud_content │
│ - Self-hosted        │      │ - User-filtered vectors       │
└──────────────────────┘      │ - Metadata for auth          │
                               │ - HNSW index                  │
                               └───────────────────────────────┘
                                          ▲
                                          │
                                          │ Indexing
                                          │
                               ┌──────────┴────────────────────┐
                               │ Background Sync Worker        │
                               │ (see ADR-002 for auth)        │
                               │                               │
                               │ 1. Fetch user content         │
                               │ 2. Generate embeddings        │
                               │ 3. Upsert to Qdrant          │
                               │ 4. Update metadata            │
                               └───────────────────────────────┘
```

## Implementation Details

### 1. Vector Database Selection: Qdrant

After evaluating multiple options, we select **Qdrant** for the following reasons:

**Qdrant Advantages:**
- ✅ Native async Python client (`qdrant-client`)
- ✅ Efficient multi-tenancy via filtered search (no collection-per-user needed)
- ✅ Built-in hybrid search support (dense + sparse vectors)
- ✅ HNSW index with excellent performance
- ✅ Lightweight Docker deployment
- ✅ Persistent storage with snapshots
- ✅ API key authentication
- ✅ Active development and documentation

**Comparison with Alternatives:**

| Feature | Qdrant | Chroma | Weaviate | pgvector |
|---------|--------|--------|----------|----------|
| Async Python | ✅ | ⚠️ Sync | ✅ | ✅ |
| Multi-tenant filtering | ✅ | ⚠️ Limited | ✅ | ✅ |
| Hybrid search | ✅ | ❌ | ✅ | ⚠️ Manual |
| Docker deployment | ✅ Easy | ✅ Easy | ✅ Complex | ⚠️ Postgres |
| Memory usage | ✅ Low | ⚠️ Medium | ⚠️ High | ✅ Low |
| Maturity | ✅ Production | ⚠️ Young | ✅ Production | ✅ Mature |

**Decision**: Qdrant provides the best balance of features, performance, and ease of deployment.

### 2. Embedding Strategy: Tiered Approach

Support multiple embedding backends with automatic fallback:

```python
class EmbeddingService:
    """Unified interface for embedding generation"""

    def __init__(self):
        self.provider = self._detect_provider()

    def _detect_provider(self) -> EmbeddingProvider:
        """Auto-detect available embedding provider"""

        # Tier 1: OpenAI API (best quality, requires API key)
        if os.getenv("OPENAI_API_KEY"):
            return OpenAIEmbedding(
                model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
                api_key=os.getenv("OPENAI_API_KEY")
            )

        # Tier 2: Self-hosted embedding service (good quality, privacy-preserving)
        if os.getenv("EMBEDDING_SERVICE_URL"):
            return HTTPEmbedding(
                url=os.getenv("EMBEDDING_SERVICE_URL"),
                model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
            )

        # Tier 3: Local model (fallback, CPU-only)
        logger.warning("No cloud/hosted embeddings available, using local model")
        return LocalEmbedding(
            model=os.getenv("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        )

    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text"""
        return await self.provider.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts (optimized)"""
        return await self.provider.embed_batch(texts)
```

#### 2.1 OpenAI Embeddings (Tier 1)

```python
class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI embedding API"""

    def __init__(self, model: str, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dimension = 1536 if "3-small" in model else 1536  # Model-dependent

    async def embed(self, text: str) -> list[float]:
        response = await self.client.embeddings.create(
            model=self.model,
            input=text
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # OpenAI supports batch up to 2048 inputs
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts
        )
        return [item.embedding for item in response.data]
```

**Costs**: text-embedding-3-small: $0.02 per 1M tokens (~4M characters)
- 10,000 notes × 500 words avg = ~$0.10 to index
- Searches are extremely cheap (~$0.00002 per query)

#### 2.2 Self-Hosted Embeddings (Tier 2)

```python
class HTTPEmbedding(EmbeddingProvider):
    """Self-hosted embedding service (Infinity, TEI, Ollama)"""

    def __init__(self, url: str, model: str):
        self.client = httpx.AsyncClient()
        self.url = url
        self.model = model
        self.dimension = 384  # Model-dependent (bge-small: 384, bge-base: 768)

    async def embed(self, text: str) -> list[float]:
        response = await self.client.post(
            f"{self.url}/embeddings",
            json={"input": text, "model": self.model}
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]
```

**Self-Hosted Options**:
- **Infinity**: Lightweight, OpenAI-compatible API, GPU support
- **Text Embeddings Inference (TEI)**: HuggingFace official, optimized, Rust-based
- **Ollama**: Easy setup, multi-model support, CPU/GPU

#### 2.3 Local Embeddings (Tier 3)

```python
class LocalEmbedding(EmbeddingProvider):
    """Local embedding using sentence-transformers (CPU fallback)"""

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model)
        self.dimension = self.model.get_sentence_embedding_dimension()

    async def embed(self, text: str) -> list[float]:
        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None,
            self.model.encode,
            text
        )
        return embedding.tolist()
```

**Recommended Local Models**:
- `all-MiniLM-L6-v2`: 384 dims, fast, good quality
- `all-mpnet-base-v2`: 768 dims, slower, better quality
- `paraphrase-multilingual-MiniLM-L12-v2`: Multilingual support

### 3. Vector Database Schema

```python
# Qdrant collection configuration
collection_config = {
    "collection_name": "nextcloud_content",
    "vectors_config": {
        "size": 384,  # Embedding dimension (model-dependent)
        "distance": "Cosine"  # Cosine similarity for semantic search
    },
    "optimizers_config": {
        "indexing_threshold": 10000  # Start indexing after 10k vectors
    },
    "hnsw_config": {
        "m": 16,  # Number of edges per node (balance speed/accuracy)
        "ef_construct": 100  # Quality of index construction
    }
}

# Payload schema (metadata)
payload_schema = {
    "user_id": str,           # Required: owner of content
    "content_type": str,      # "note", "file", "calendar_event"
    "content_id": str,        # Source ID (note_id, file_path, event_id)
    "title": str,             # Searchable title
    "excerpt": str,           # First 200 chars for preview
    "category": str,          # Optional: category/folder
    "mime_type": str,         # Optional: file MIME type
    "shared_with": list[str], # Optional: list of user_ids with access
    "tags": list[str],        # Optional: user tags
    "created_at": int,        # Unix timestamp
    "modified_at": int,       # Unix timestamp
    "indexed_at": int         # Unix timestamp (for sync tracking)
}
```

#### 3.1 Multi-Tenancy via Filtering

```python
# User-specific search with filtering
search_results = await qdrant_client.search(
    collection_name="nextcloud_content",
    query_vector=query_embedding,
    query_filter=models.Filter(
        must=[
            # User owns the content OR it's shared with them
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=current_user_id)
                    ),
                    models.FieldCondition(
                        key="shared_with",
                        match=models.MatchAny(any=[current_user_id])
                    )
                ]
            ),
            # Optional: filter by content type
            models.FieldCondition(
                key="content_type",
                match=models.MatchValue(value="note")
            )
        ]
    ),
    limit=20,
    score_threshold=0.7  # Only return confident matches
)
```

### 4. Hybrid Search Implementation

Combine semantic and keyword search for best results:

```python
@mcp.tool()
@require_scopes("notes:read")
async def nc_notes_hybrid_search(
    query: str,
    ctx: Context,
    limit: int = 10,
    semantic_weight: float = 0.7,
    keyword_weight: float = 0.3
) -> SearchNotesResponse:
    """
    Hybrid search combining semantic understanding with keyword precision.

    Args:
        query: Natural language search query
        limit: Maximum results to return
        semantic_weight: Weight for semantic similarity (0-1)
        keyword_weight: Weight for keyword matching (0-1)
    """

    client = get_client(ctx)
    username = client.username

    # Run searches in parallel
    semantic_task = asyncio.create_task(
        semantic_search(query, username, limit=limit * 2)
    )
    keyword_task = asyncio.create_task(
        keyword_search(query, username, limit=limit * 2)
    )

    semantic_results, keyword_results = await asyncio.gather(
        semantic_task, keyword_task
    )

    # Fusion: Combine and rerank results
    fused_results = reciprocal_rank_fusion(
        semantic_results,
        keyword_results,
        semantic_weight=semantic_weight,
        keyword_weight=keyword_weight
    )

    # Verify permissions via Nextcloud API (dual-phase authorization)
    verified_results = []
    for result in fused_results[:limit * 2]:  # Get extra for filtering
        try:
            note = await client.notes.get_note(result["note_id"])
            verified_results.append({
                "note": note,
                "score": result["score"],
                "match_type": result["match_type"]  # "semantic", "keyword", "both"
            })
            if len(verified_results) >= limit:
                break
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                continue  # User lost access
            raise

    return SearchNotesResponse(
        results=verified_results,
        query=query,
        total_found=len(verified_results),
        search_method="hybrid"
    )

def reciprocal_rank_fusion(
    semantic_results: list[dict],
    keyword_results: list[dict],
    semantic_weight: float = 0.7,
    keyword_weight: float = 0.3,
    k: int = 60  # RRF constant
) -> list[dict]:
    """
    Reciprocal Rank Fusion for combining search results.

    RRF is more robust than score normalization because it only
    depends on ranks, not absolute scores.
    """

    # Build rank maps
    semantic_ranks = {r["note_id"]: i for i, r in enumerate(semantic_results)}
    keyword_ranks = {r["note_id"]: i for i, r in enumerate(keyword_results)}

    # Get all unique note IDs
    all_note_ids = set(semantic_ranks.keys()) | set(keyword_ranks.keys())

    # Calculate fused scores
    fused = []
    for note_id in all_note_ids:
        # RRF formula: score = sum(weight_i / (k + rank_i))
        semantic_score = 0
        keyword_score = 0
        match_type = []

        if note_id in semantic_ranks:
            semantic_score = semantic_weight / (k + semantic_ranks[note_id])
            match_type.append("semantic")

        if note_id in keyword_ranks:
            keyword_score = keyword_weight / (k + keyword_ranks[note_id])
            match_type.append("keyword")

        fused.append({
            "note_id": note_id,
            "score": semantic_score + keyword_score,
            "match_type": "+".join(match_type)
        })

    # Sort by fused score
    fused.sort(key=lambda x: x["score"], reverse=True)
    return fused
```

### 5. Document Chunking Strategy

For large documents (>1000 tokens), implement semantic chunking:

```python
class DocumentChunker:
    """Chunk large documents for optimal embedding"""

    def __init__(self, chunk_size: int = 512, overlap: int = 50):
        self.chunk_size = chunk_size  # tokens
        self.overlap = overlap  # overlapping tokens

    def chunk_document(
        self,
        content: str,
        metadata: dict
    ) -> list[tuple[str, dict]]:
        """
        Split document into overlapping chunks with metadata.

        Returns list of (chunk_text, chunk_metadata) tuples.
        """

        # Tokenize (approximate with words for simplicity)
        tokens = content.split()

        if len(tokens) <= self.chunk_size:
            # Document fits in single chunk
            return [(content, metadata)]

        chunks = []
        start = 0

        while start < len(tokens):
            end = start + self.chunk_size
            chunk_tokens = tokens[start:end]
            chunk_text = " ".join(chunk_tokens)

            # Add chunk metadata
            chunk_metadata = {
                **metadata,
                "chunk_index": len(chunks),
                "chunk_start": start,
                "chunk_end": end,
                "is_chunk": True
            }

            chunks.append((chunk_text, chunk_metadata))

            # Move to next chunk with overlap
            start = end - self.overlap

        return chunks

# Usage in sync worker
async def index_document(doc: Document, user_id: str):
    """Index a document with chunking"""

    chunker = DocumentChunker(chunk_size=512, overlap=50)
    chunks = chunker.chunk_document(
        content=doc.content,
        metadata={
            "user_id": user_id,
            "content_type": "file",
            "content_id": doc.path,
            "title": doc.title,
            "mime_type": doc.mime_type
        }
    )

    # Generate embeddings in batch
    chunk_texts = [chunk[0] for chunk in chunks]
    embeddings = await embedding_service.embed_batch(chunk_texts)

    # Upsert all chunks
    points = []
    for (chunk_text, chunk_metadata), embedding in zip(chunks, embeddings):
        points.append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    **chunk_metadata,
                    "excerpt": chunk_text[:200]  # Preview
                }
            )
        )

    await qdrant_client.upsert(
        collection_name="nextcloud_content",
        points=points
    )
```

### 6. Background Sync Worker

```python
# nextcloud_mcp_server/sync/vector_indexer.py
class VectorIndexer:
    """Indexes content into vector database"""

    def __init__(
        self,
        qdrant_client: AsyncQdrantClient,
        embedding_service: EmbeddingService,
        auth_provider: SyncAuthProvider  # From ADR-002
    ):
        self.qdrant = qdrant_client
        self.embeddings = embedding_service
        self.auth = auth_provider

    async def sync_user_notes(self, user_id: str):
        """Sync all notes for a user"""

        # Get authenticated client for user
        client = await self.auth.get_user_client(user_id)

        # Fetch all notes
        notes = await client.notes.list_notes()
        logger.info(f"Syncing {len(notes)} notes for {user_id}")

        # Check which notes need updating
        existing_ids = await self._get_indexed_note_ids(user_id)
        notes_to_update = [
            n for n in notes
            if f"note_{n.id}" not in existing_ids
            or n.modified > existing_ids[f"note_{n.id}"]
        ]

        if not notes_to_update:
            logger.info(f"All notes up-to-date for {user_id}")
            return

        # Generate embeddings in batch
        contents = [f"{n.title}\n\n{n.content}" for n in notes_to_update]
        embeddings = await self.embeddings.embed_batch(contents)

        # Prepare points for upsert
        points = []
        for note, embedding in zip(notes_to_update, embeddings):
            points.append(
                models.PointStruct(
                    id=f"note_{note.id}",
                    vector=embedding,
                    payload={
                        "user_id": user_id,
                        "content_type": "note",
                        "content_id": str(note.id),
                        "note_id": note.id,
                        "title": note.title,
                        "excerpt": note.content[:200],
                        "category": note.category,
                        "created_at": note.created,
                        "modified_at": note.modified,
                        "indexed_at": int(time.time())
                    }
                )
            )

        # Upsert to Qdrant
        await self.qdrant.upsert(
            collection_name="nextcloud_content",
            points=points
        )

        logger.info(f"Indexed {len(points)} notes for {user_id}")

    async def _get_indexed_note_ids(self, user_id: str) -> dict[str, int]:
        """Get map of note_id -> modified_at for indexed notes"""

        # Query Qdrant for existing notes
        scroll_result = await self.qdrant.scroll(
            collection_name="nextcloud_content",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id)
                    ),
                    models.FieldCondition(
                        key="content_type",
                        match=models.MatchValue(value="note")
                    )
                ]
            ),
            with_payload=["content_id", "modified_at"],
            limit=10000
        )

        return {
            point.payload["content_id"]: point.payload["modified_at"]
            for point, _ in scroll_result
        }

    async def delete_note(self, user_id: str, note_id: int):
        """Remove deleted note from index"""

        await self.qdrant.delete(
            collection_name="nextcloud_content",
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_id",
                            match=models.MatchValue(value=user_id)
                        ),
                        models.FieldCondition(
                            key="note_id",
                            match=models.MatchValue(value=note_id)
                        )
                    ]
                )
            )
        )
```

### 7. Configuration

#### 7.1 Environment Variables
```bash
# Vector Database
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=<secure-api-key>
QDRANT_COLLECTION=nextcloud_content

# Embedding Strategy (choose one)
# Option 1: OpenAI
OPENAI_API_KEY=sk-...
OPENAI_EMBEDDING_MODEL=text-embedding-3-small  # or text-embedding-3-large

# Option 2: Self-hosted
EMBEDDING_SERVICE_URL=http://embeddings:7997
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# Option 3: Local (fallback, no config needed)

# Search Configuration
SEMANTIC_SEARCH_ENABLED=true
HYBRID_SEARCH_DEFAULT_SEMANTIC_WEIGHT=0.7
HYBRID_SEARCH_DEFAULT_KEYWORD_WEIGHT=0.3
SEARCH_SCORE_THRESHOLD=0.7

# Sync Configuration
VECTOR_SYNC_INTERVAL=300  # seconds
VECTOR_SYNC_BATCH_SIZE=100
```

#### 7.2 Docker Compose

```yaml
services:
  # Vector Database
  qdrant:
    image: qdrant/qdrant:latest
    restart: always
    ports:
      - 127.0.0.1:6333:6333  # REST API
      - 127.0.0.1:6334:6334  # gRPC
    volumes:
      - qdrant_storage:/qdrant/storage
    environment:
      - QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY}
      - QDRANT__SERVICE__HTTP_PORT=6333
      - QDRANT__SERVICE__GRPC_PORT=6334

  # Embedding Service (optional - for self-hosted)
  embeddings:
    image: michaelf34/infinity:latest
    restart: always
    ports:
      - 127.0.0.1:7997:7997
    volumes:
      - embedding_models:/app/.cache
    environment:
      - MODEL_ID=BAAI/bge-small-en-v1.5
      - BATCH_SIZE=32
      - ENGINE=torch  # or optimum for better CPU performance
    # Optional: GPU support
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  # MCP Server with vector search
  mcp:
    build: .
    command: ["--transport", "streamable-http"]
    depends_on:
      - app
      - qdrant
      - embeddings  # optional
    environment:
      # ... existing env vars ...
      - SEMANTIC_SEARCH_ENABLED=true
      - QDRANT_URL=http://qdrant:6333
      - QDRANT_API_KEY=${QDRANT_API_KEY}
      # Choose embedding strategy
      - EMBEDDING_SERVICE_URL=http://embeddings:7997
      # OR
      # - OPENAI_API_KEY=${OPENAI_API_KEY}

  # Vector Sync Worker
  mcp-vector-sync:
    build: .
    command: ["python", "-m", "nextcloud_mcp_server.sync.vector_indexer"]
    depends_on:
      - app
      - qdrant
      - embeddings  # optional
    environment:
      # Nextcloud + Auth (from ADR-002)
      - NEXTCLOUD_HOST=http://app:80
      - ENABLE_OFFLINE_ACCESS=true
      - TOKEN_ENCRYPTION_KEY=${TOKEN_ENCRYPTION_KEY}
      # Vector Database
      - QDRANT_URL=http://qdrant:6333
      - QDRANT_API_KEY=${QDRANT_API_KEY}
      # Embeddings
      - EMBEDDING_SERVICE_URL=http://embeddings:7997
    volumes:
      - sync-tokens:/app/data

volumes:
  qdrant_storage:
  embedding_models:
  sync-tokens:
```

### 8. Performance Optimization

#### 8.1 Indexing Performance

```python
# Batch embedding generation
async def embed_batch_chunked(
    texts: list[str],
    batch_size: int = 100
) -> list[list[float]]:
    """Generate embeddings in chunks to avoid memory issues"""

    embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        batch_embeddings = await embedding_service.embed_batch(batch)
        embeddings.extend(batch_embeddings)
        await asyncio.sleep(0.1)  # Rate limiting

    return embeddings

# Parallel upsert with batching
async def upsert_points_batched(
    points: list[models.PointStruct],
    batch_size: int = 100
):
    """Upsert points in batches"""

    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        await qdrant_client.upsert(
            collection_name="nextcloud_content",
            points=batch,
            wait=False  # Don't wait for indexing
        )
```

#### 8.2 Search Performance

```python
# Search with prefetch for better accuracy
search_results = await qdrant_client.search(
    collection_name="nextcloud_content",
    query_vector=query_embedding,
    query_filter=user_filter,
    limit=20,
    with_payload=True,
    with_vectors=False,  # Don't return vectors (saves bandwidth)
    search_params=models.SearchParams(
        hnsw_ef=128,  # Higher = more accurate but slower
        exact=False   # Use HNSW index
    )
)
```

#### 8.3 Caching

```python
# Cache embeddings for common queries
from functools import lru_cache

@lru_cache(maxsize=1000)
def cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

async def embed_with_cache(text: str) -> list[float]:
    """Generate embedding with caching"""

    key = cache_key(text)

    # Check Redis cache
    cached = await redis.get(f"embedding:{key}")
    if cached:
        return json.loads(cached)

    # Generate embedding
    embedding = await embedding_service.embed(text)

    # Cache for 1 hour
    await redis.setex(
        f"embedding:{key}",
        3600,
        json.dumps(embedding)
    )

    return embedding
```

### 9. Monitoring and Metrics

```python
# Prometheus metrics
from prometheus_client import Counter, Histogram, Gauge

# Search metrics
semantic_search_count = Counter(
    'semantic_search_total',
    'Total semantic searches',
    ['user_id', 'content_type']
)

semantic_search_latency = Histogram(
    'semantic_search_duration_seconds',
    'Semantic search latency',
    ['phase']  # 'embedding', 'vector_search', 'verification'
)

# Indexing metrics
documents_indexed = Counter(
    'documents_indexed_total',
    'Total documents indexed',
    ['user_id', 'content_type']
)

index_queue_size = Gauge(
    'index_queue_size',
    'Number of documents waiting to be indexed'
)

# Usage
async def semantic_search(query: str, user_id: str):
    semantic_search_count.labels(user_id=user_id, content_type='note').inc()

    with semantic_search_latency.labels(phase='embedding').time():
        embedding = await embed(query)

    with semantic_search_latency.labels(phase='vector_search').time():
        results = await qdrant.search(...)

    with semantic_search_latency.labels(phase='verification').time():
        verified = await verify_access(results)

    return verified
```

## Consequences

### Benefits

1. **Semantic Understanding**
   - Find content by meaning, not just keywords
   - Support for natural language queries
   - Cross-lingual search potential
   - Better context discovery for LLMs

2. **User Experience**
   - More relevant search results
   - Discover related content across apps
   - Fast sub-second query latency
   - Hybrid search combines best of both worlds

3. **Architecture**
   - External sidecar (doesn't bloat MCP server)
   - Configurable embedding backend (cloud/self-hosted/local)
   - Multi-tenant with strict isolation
   - Scales horizontally (Qdrant cluster)

4. **Privacy & Security**
   - Self-hosted option available
   - Dual-phase authorization enforces permissions
   - Vector DB is cache, not source of truth
   - Per-user audit trail

5. **Developer Experience**
   - Simple async Python API
   - Comprehensive monitoring
   - Clear upgrade path (better embeddings, reranking)

### Limitations

1. **Complexity**
   - Additional infrastructure (Qdrant + embeddings)
   - More monitoring required
   - Embedding generation latency
   - Initial indexing time for large collections

2. **Cost**
   - Storage: ~4KB per document (embedding + metadata)
   - Compute: Embedding generation (API costs or GPU)
   - Memory: Qdrant keeps vectors in RAM for speed

3. **Operational**
   - Index maintenance and updates
   - Embedding model versioning
   - Handling deleted/moved content
   - Cold start indexing for new users

4. **Search Accuracy**
   - Quality depends on embedding model
   - May miss exact keyword matches (mitigated by hybrid search)
   - Cultural/domain-specific terms may not embed well
   - Requires tuning score thresholds

### Performance Characteristics

| Metric | Target | Notes |
|--------|--------|-------|
| Search latency | <200ms | Embedding + vector search + verification |
| Indexing throughput | >100 docs/sec | With batch embeddings |
| Memory per 10k docs | ~40MB | Qdrant vectors + metadata |
| Disk per 10k docs | ~40MB | Persistent storage |
| Search accuracy | >90% | At score_threshold=0.7 |

### Cost Estimates

**Small Deployment** (10 users, 1000 notes each):
- Initial indexing: 10,000 notes × $0.00002 = $0.20 (OpenAI)
- Monthly searches: 1000 queries × $0.00002 = $0.02
- Infrastructure: Qdrant (40MB RAM), Embeddings (optional)
- **Total**: ~$0.25/month (API) or self-hosted (negligible)

**Medium Deployment** (100 users, 500 notes each):
- Initial indexing: 50,000 notes × $0.00002 = $1.00
- Monthly searches: 10,000 queries × $0.00002 = $0.20
- Infrastructure: Qdrant (200MB RAM)
- **Total**: ~$1.20/month or self-hosted

**Self-Hosted** (any size):
- GPU instance: ~$0.50/hour (~$360/month for 24/7)
- Or CPU-only: negligible cost, slower embeddings

### Future Enhancements

1. **Multimodal Search**
   - Image embeddings (CLIP)
   - PDF/document layout understanding
   - Audio transcription + embedding

2. **Advanced Ranking**
   - Cross-encoder reranking
   - Learning-to-rank models
   - User feedback signals

3. **Query Understanding**
   - Query expansion
   - Spell correction
   - Entity extraction

4. **Performance**
   - Query result caching
   - Approximate nearest neighbor improvements
   - Quantization for reduced memory

5. **Features**
   - Saved searches
   - Search analytics
   - Recommended content

## Alternatives Considered

### Alternative 1: Elasticsearch/OpenSearch

**Approach**: Use traditional full-text search engine with vector plugin

**Pros**:
- Mature ecosystem
- Excellent keyword search
- Rich query DSL

**Cons**:
- Heavy infrastructure (JVM-based)
- Complex setup and tuning
- Vector search is plugin/add-on (not native)
- Higher resource usage

**Decision**: Rejected; Qdrant is purpose-built for vectors

### Alternative 2: ChromaDB

**Approach**: Embedded or client-server vector database

**Pros**:
- Simple Python API
- Easy to get started
- Good for prototyping

**Cons**:
- Sync-only Python client (no async)
- Limited multi-tenancy features
- Less mature than Qdrant
- Scaling concerns

**Decision**: Rejected; async and multi-tenancy are critical

### Alternative 3: Weaviate

**Approach**: Full-featured vector database with GraphQL

**Pros**:
- Very feature-rich
- Built-in vectorization
- Good documentation

**Cons**:
- More complex architecture
- Higher resource usage
- GraphQL adds complexity
- Overkill for our use case

**Decision**: Rejected; Qdrant provides better balance

### Alternative 4: pgvector (PostgreSQL Extension)

**Approach**: Add vector search to existing PostgreSQL

**Pros**:
- Leverages existing PostgreSQL expertise
- Transactional consistency
- Mature database ecosystem

**Cons**:
- This deployment uses MariaDB (would need PostgreSQL)
- Performance not as optimized as purpose-built vector DB
- Manual hybrid search implementation
- HNSW index limitations

**Decision**: Rejected; dedicated vector DB is better fit

### Alternative 5: Pinecone / Vertex AI Vector Search

**Approach**: Managed cloud vector database

**Pros**:
- Fully managed
- Excellent performance
- No infrastructure management

**Cons**:
- Cloud-only (no self-hosting)
- Recurring costs
- Vendor lock-in
- Data leaves premises

**Decision**: Rejected; self-hosting is important for privacy

## Related Decisions

- ADR-001: Enhanced Note Search (establishes need for better search)
- ADR-002: Vector Sync Authentication (defines how sync workers authenticate)
- [Future] ADR-004: Content Extraction and Document Processing
- [Future] ADR-005: Cross-App Semantic Search

## References

- [Qdrant Documentation](https://qdrant.tech/documentation/)
- [Sentence Transformers](https://www.sbert.net/)
- [OpenAI Embeddings Guide](https://platform.openai.com/docs/guides/embeddings)
- [Hybrid Search with RRF](https://qdrant.tech/articles/hybrid-search/)
- [HNSW Algorithm](https://arxiv.org/abs/1603.09320)
- [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
