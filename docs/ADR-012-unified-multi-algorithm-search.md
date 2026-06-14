# ADR-012: Unified Multi-Algorithm Search with Client-Configurable Weighting

## Status
Accepted — implemented (hybrid search with RRF/DBSF fusion; see `search/hybrid.py`)

## Context

### Current State

The Nextcloud MCP server currently provides semantic search via vector similarity (Qdrant), as designed in ADR-003 and implemented through ADR-007. However, users and MCP clients have limited control over search behavior:

1. **Single algorithm only**: Only pure vector similarity search is available
2. **No algorithm selection**: MCP clients cannot choose between semantic, keyword, or fuzzy approaches
3. **No weighting control**: Clients cannot adjust the balance between different search methods
4. **Disconnected implementations**: Viz pane uses different search algorithms than MCP tools
5. **Limited flexibility**: No way to optimize search for different use cases (exact match vs. conceptual similarity)

### User Needs

Different search scenarios require different algorithms:

- **Exact match queries**: "Find note titled 'Q1 Budget'" → keyword search preferred
- **Conceptual queries**: "What are my goals for next quarter?" → semantic search preferred
- **Typo-tolerant queries**: "Find note about kuberntes" → fuzzy search needed
- **Balanced queries**: "Find documentation about API endpoints" → hybrid search optimal

Additionally, users need a **testing interface** (viz pane) to:
- Experiment with different search algorithms on their own documents
- Visualize search results and algorithm behavior
- Tune weights for optimal results
- Understand which algorithm works best for their queries

### Technical Requirements

1. **Unified interface**: Single MCP tool supporting multiple algorithms
2. **Client control**: MCP clients specify algorithm and weights via tool parameters
3. **Backward compatibility**: Existing `nc_semantic_search()` behavior preserved
4. **Shared implementation**: Viz pane and MCP tools use identical search algorithms
5. **User accessibility**: Viz pane available to all logged-in users with vector sync enabled
6. **Performance**: Minimal overhead for algorithm selection

## Decision

We will implement a **unified multi-algorithm search architecture** with the following components:

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MCP Client / User Browser                            │
│                                                                               │
│  ┌──────────────────────────┐         ┌──────────────────────────────────┐  │
│  │   MCP Tool Call          │         │   Viz Pane (Browser UI)          │  │
│  │                          │         │                                  │  │
│  │ nc_semantic_search(      │         │ - Algorithm selector dropdown    │  │
│  │   query="kubernetes",    │         │ - Weight adjustment sliders      │  │
│  │   algorithm="hybrid",    │         │ - Interactive 2D scatter plot    │  │
│  │   semantic_weight=0.5,   │         │ - Side-by-side comparison        │  │
│  │   keyword_weight=0.3,    │         │ - Real-time search testing       │  │
│  │   fuzzy_weight=0.2       │         │                                  │  │
│  │ )                        │         │                                  │  │
│  └───────────┬──────────────┘         └────────────┬─────────────────────┘  │
└──────────────┼─────────────────────────────────────┼────────────────────────┘
               │                                      │
               │ MCP Protocol                         │ HTTPS (htmx)
               │                                      │
┌──────────────▼──────────────────────────────────────▼────────────────────────┐
│                        MCP Server (/app endpoint)                             │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │              Unified Search Interface (server/semantic.py)              │ │
│  │                                                                         │ │
│  │  @mcp.tool() nc_semantic_search(algorithm, weights...)                 │ │
│  │  ├─ Validate parameters (weights sum ≤1.0)                             │ │
│  │  ├─ Dispatch to algorithm selector                                     │ │
│  │  └─ Return ranked SearchResponse                                       │ │
│  └────────────────────────────┬────────────────────────────────────────────┘ │
│                                │                                              │
│  ┌────────────────────────────▼────────────────────────────────────────────┐ │
│  │              Algorithm Dispatcher (search/algorithms.py)                │ │
│  │                                                                         │ │
│  │  if algorithm == "semantic":    → semantic.py                          │ │
│  │  if algorithm == "keyword":     → keyword.py                           │ │
│  │  if algorithm == "fuzzy":       → fuzzy.py                             │ │
│  │  if algorithm == "hybrid":      → hybrid.py (RRF fusion)               │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐           │
│  │  semantic.py     │  │  keyword.py      │  │  fuzzy.py        │           │
│  │                  │  │                  │  │                  │           │
│  │ • Query Qdrant   │  │ • Token matching │  │ • Char overlap   │           │
│  │ • Cosine dist    │  │ • Title weight   │  │ • 70% threshold  │           │
│  │ • Score ≥0.7     │  │ • ADR-001 logic  │  │ • Simple impl    │           │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘           │
│           │                     │                      │                     │
│           └─────────────────────┼──────────────────────┘                     │
│                                 │                                            │
│  ┌──────────────────────────────▼──────────────────────────────────────────┐ │
│  │                    hybrid.py (Reciprocal Rank Fusion)                   │ │
│  │                                                                         │ │
│  │  1. Run algorithms in parallel (semantic, keyword, fuzzy)              │ │
│  │  2. Collect ranked results from each                                   │ │
│  │  3. Apply RRF formula: score = weight / (k + rank)                     │ │
│  │  4. Combine scores across algorithms                                   │ │
│  │  5. Re-rank by combined score                                          │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────┬───────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
         ┌──────────▼──────────┐         ┌─────────▼────────────┐
         │ Qdrant Vector DB    │         │ Nextcloud APIs       │
         │                     │         │                      │
         │ • Vector search     │         │ • Access verification│
         │ • user_id filter    │         │ • Full metadata fetch│
         │ • Score threshold   │         │ • Permission checks  │
         │ • 768-dim embeddings│         │                      │
         └─────────────────────┘         └──────────────────────┘
```

### Data Flow

#### MCP Tool Request
```
1. Client calls nc_semantic_search(query, algorithm="hybrid", weights...)
2. Server validates parameters (weights sum ≤1.0)
3. Dispatcher routes to hybrid.py
4. Hybrid search runs semantic, keyword, fuzzy in parallel
5. RRF combines results with weighted scores
6. Access verification via Nextcloud API
7. Return ranked SearchResponse to client
```

#### Viz Pane Request (Server-Side Processing)
```
1. User navigates to /app (Vector Visualization tab)
2. Browser loads vector-viz fragment via htmx
3. User enters query and adjusts algorithm/weights
4. htmx sends request to /app/vector-viz endpoint
5. Server executes search via search/algorithms.py:
   - Filters by user_id (multi-tenant security)
   - Applies selected algorithm (semantic/keyword/fuzzy/hybrid)
   - Filters by document type (notes/files/calendar/contacts)
   - Retrieves matching results + metadata
6. Server performs PCA reduction (768-dim → 2D):
   - Converts matching results to 2D coordinates
   - Only sends coordinates + metadata (not full vectors)
   - Dramatically reduces bandwidth (e.g., 768 floats → 2 floats per doc)
7. Server returns JSON: {results: [...], coordinates_2d: [...], stats: {...}}
8. Browser receives lightweight response
9. Plotly.js renders interactive scatter plot
10. Matching results highlighted (blue), non-matches grayed (40% opacity)
```

**Performance Benefits of Server-Side Processing**:
- **Bandwidth reduction**: ~384x less data (2 floats vs 768 floats per document)
- **Client efficiency**: Browser only handles visualization, not computation
- **Scalability**: Can visualize 10,000+ documents without client-side lag
- **Security**: Raw vectors never leave server
- **Consistency**: Same search logic as MCP tool (no drift)

### 1. Core Search Algorithms

Four search algorithms will be available:

#### a) Semantic Search (Vector Similarity)
- **Method**: Cosine distance in 768-dimensional embedding space
- **Implementation**: Qdrant `query_points` with user_id filtering
- **Use case**: Conceptual queries, finding related content
- **Current status**: Implemented in `nextcloud_mcp_server/server/semantic.py`

#### b) Keyword Search (Token-Based)
- **Method**: Token matching with weighted scoring (from ADR-001)
- **Implementation**: Title matches weighted 3x higher than content
- **Use case**: Exact phrase matching, known titles
- **Current status**: Designed in ADR-001, not implemented

#### c) Fuzzy Search (Character Overlap)
- **Method**: Simple character-based similarity (70% threshold)
- **Implementation**: Character set comparison (current viz pane approach)
- **Use case**: Typo tolerance, approximate matching
- **Current status**: Implemented in viz pane only

#### d) Hybrid Search (Multi-Algorithm Fusion)
- **Method**: Reciprocal Rank Fusion (RRF) from ADR-003
- **Implementation**: Parallel execution + score combination
- **Use case**: Balanced queries, general-purpose search
- **Current status**: Designed in ADR-003, not implemented

### 2. Unified MCP Tool Interface

```python
@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search(
    query: str,
    ctx: Context,
    limit: int = 10,
    score_threshold: float = 0.7,
    algorithm: Literal["semantic", "keyword", "fuzzy", "hybrid"] = "hybrid",
    semantic_weight: float = 0.5,
    keyword_weight: float = 0.3,
    fuzzy_weight: float = 0.2,
) -> SearchResponse:
    """
    Search Nextcloud content using configurable algorithms.

    Args:
        query: Natural language search query
        ctx: MCP context for authentication
        limit: Maximum results to return
        score_threshold: Minimum similarity score (semantic/hybrid only)
        algorithm: Search algorithm to use
        semantic_weight: Weight for semantic results (hybrid only, default: 0.5)
        keyword_weight: Weight for keyword results (hybrid only, default: 0.3)
        fuzzy_weight: Weight for fuzzy results (hybrid only, default: 0.2)

    Returns:
        Ranked search results with scores and excerpts
    """
```

**Key decisions**:
- **Single tool name**: Keep `nc_semantic_search` for backward compatibility
- **Algorithm parameter**: Explicit selection via enum
- **Weight parameters**: Client-configurable, only apply to hybrid mode
- **Validation**: Weights must sum to ≤1.0, enforced server-side
- **Defaults**: Hybrid mode with balanced weights (semantic 50%, keyword 30%, fuzzy 20%)

### 3. Shared Algorithm Implementation

Extract search algorithms into reusable module:

```
nextcloud_mcp_server/
├── search/
│   ├── __init__.py
│   ├── algorithms.py          # Core search implementations
│   ├── semantic.py             # Vector similarity search
│   ├── keyword.py              # Token-based search (ADR-001)
│   ├── fuzzy.py                # Character overlap search
│   └── hybrid.py               # RRF fusion (ADR-003)
└── server/
    └── semantic.py             # MCP tool wrapper
```

**Benefits**:
- Viz pane and MCP tools share identical implementations
- Testable in isolation
- Easy to add new algorithms (e.g., BM25, neural reranking)
- Clear separation of concerns

### 4. Viz Pane Integration

Update viz pane (`nextcloud_mcp_server/auth/userinfo_routes.py`) to:

1. **Use shared algorithms**: Import from `search/algorithms.py`
2. **Server-side filtering**: All search and filtering operations happen server-side
   - Query execution via shared search backend
   - Document type filtering (notes, files, calendar, contacts)
   - User ID filtering for multi-tenant security
   - Only matching results + metadata sent to client
   - Reduces bandwidth and improves performance
3. **PCA reduction**: Server performs dimensionality reduction (768-dim → 2D)
   - Only 2D coordinates sent to browser for visualization
   - Dramatically reduces data transfer vs sending full vectors
   - Enables visualization of large document collections
4. **User accessibility**: Available to all users with vector sync enabled
5. **Security**: Filter results by `user_id` (only show user's own documents)
6. **Interactive testing**: Allow users to:
   - Select algorithm type
   - Adjust weights (hybrid mode)
   - Compare results across algorithms
   - Visualize result distribution in 2D space

#### Viz Pane UI Components

```
┌────────────────────────────────────────────────────────────────────────┐
│ Vector Visualization                                          [Status] │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐  │
│ │ Search Configuration                                             │  │
│ │                                                                  │  │
│ │ Query: [_______________________________________________] [Search]│  │
│ │                                                                  │  │
│ │ Algorithm: [Hybrid ▼]  [Semantic] [Keyword] [Fuzzy]             │  │
│ │                                                                  │  │
│ │ Weights (Hybrid Mode):                                           │  │
│ │   Semantic: [========50========] 0.5                             │  │
│ │   Keyword:  [======30======    ] 0.3                             │  │
│ │   Fuzzy:    [====20====        ] 0.2                             │  │
│ │                                                                  │  │
│ │ Document Types: ☑ Notes  ☑ Files  ☑ Calendar  ☑ Contacts        │  │
│ └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐  │
│ │ Vector Space Visualization (PCA 2D Projection)                   │  │
│ │                                                                  │  │
│ │        ▲                                                         │  │
│ │    PC2 │     ●  ● ●      🔵 Matching results (full opacity)     │  │
│ │        │  ●     ●  ●     ⚪ Non-matching results (40% opacity)   │  │
│ │        │    🔵  ● ●                                              │  │
│ │        │  ●  🔵  ●       Hover: Show document title + excerpt    │  │
│ │        │  ● ●  🔵 ●      Click: Open document in Nextcloud       │  │
│ │    ────┼──●─🔵──●─●────► PC1                                     │  │
│ │        │   ● ●  ●                                                │  │
│ │        │    🔵 ●   ●     Explained Variance:                     │  │
│ │        │  ●    ●  ●      PC1: 23.4% | PC2: 18.7%                 │  │
│ │        │     ● ●                                                 │  │
│ │                                                                  │  │
│ └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐  │
│ │ Search Results (12 matching documents)                           │  │
│ │                                                                  │  │
│ │ 🔵 Kubernetes Setup Guide                        Score: 0.87     │  │
│ │    "...configure kubectl to connect to cluster..."              │  │
│ │    [Open in Nextcloud]                                           │  │
│ │                                                                  │  │
│ │ 🔵 Container Orchestration Notes                 Score: 0.82     │  │
│ │    "...deployment strategies for kubernetes..."                 │  │
│ │    [Open in Nextcloud]                                           │  │
│ │                                                                  │  │
│ │ 🔵 K8s Troubleshooting                           Score: 0.79     │  │
│ │    "...common kuberntes errors and solutions..."                │  │
│ │    [Open in Nextcloud]                                           │  │
│ │                                                                  │  │
│ │ [Show More Results...]                                           │  │
│ └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│ ┌──────────────────────────────────────────────────────────────────┐  │
│ │ Algorithm Performance Comparison                                 │  │
│ │                                                                  │  │
│ │ Algorithm    │ Results │ Avg Score │ Time (ms) │ Precision     │  │
│ │ ─────────────┼─────────┼───────────┼───────────┼───────────     │  │
│ │ Semantic     │   45    │   0.78    │   145ms   │  ████░ 0.82   │  │
│ │ Keyword      │   23    │   0.91    │    42ms   │  ███░░ 0.67   │  │
│ │ Fuzzy        │   67    │   0.72    │    89ms   │  ██░░░ 0.45   │  │
│ │ Hybrid (RRF) │   52    │   0.84    │   198ms   │  █████ 0.89   │  │
│ └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

**Key UI Features**:

1. **Search Input**: Real-time query testing with instant visualization
2. **Algorithm Selector**: Dropdown + quick-select buttons
3. **Weight Sliders**: Visual adjustment with live preview (hybrid mode only)
4. **Document Type Filters**: Checkboxes for notes, files, calendar, contacts
5. **2D Scatter Plot**: Interactive Plotly.js visualization
   - Blue dots = matching documents (full opacity)
   - Gray dots = non-matching documents (40% opacity)
   - Hover = show title + excerpt tooltip
   - Click = open document in Nextcloud
   - Zoom/pan controls for exploration
6. **Results Panel**: Ranked list with scores and excerpts
7. **Performance Table**: Compare algorithm speed and accuracy
8. **Explained Variance**: Show how much information PCA preserves

**Technology Stack**:
- **Frontend**: htmx for dynamic loading, Alpine.js for reactivity
- **Visualization**: Plotly.js for interactive scatter plots
- **Styling**: Tailwind CSS (consistent with existing /app UI)
- **Backend**: Shared `search/algorithms.py` implementation

### 5. Reciprocal Rank Fusion (RRF) for Hybrid Search

Following ADR-003's design:

```python
def reciprocal_rank_fusion(
    results: dict[str, list[SearchResult]],
    weights: dict[str, float],
    k: int = 60
) -> list[SearchResult]:
    """
    Combine multiple ranked result lists using RRF.

    Args:
        results: Dict of algorithm_name -> ranked results
        weights: Dict of algorithm_name -> weight (0-1)
        k: RRF constant (default: 60, standard value)

    Returns:
        Combined and re-ranked results
    """
    scores = defaultdict(float)

    for algo_name, algo_results in results.items():
        weight = weights.get(algo_name, 0.0)
        for rank, result in enumerate(algo_results, start=1):
            # RRF formula: 1 / (k + rank)
            rrf_score = weight / (k + rank)
            scores[result.doc_id] += rrf_score

    # Sort by combined score, return top results
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

**RRF properties**:
- **Rank-based**: Uses position, not raw scores (handles score scale differences)
- **Proven effective**: Standard approach in information retrieval
- **Configurable**: `k` parameter controls rank decay (default: 60)
- **Weight support**: Allows algorithm-specific importance

## Implementation Plan

### Phase 1: Extract and Unify Algorithms (Week 1)

1. Create `nextcloud_mcp_server/search/` module
2. Implement `algorithms.py` with base interface
3. Extract semantic search logic from `server/semantic.py`
4. Implement keyword search from ADR-001 design
5. Extract fuzzy search from viz pane
6. Implement RRF hybrid search from ADR-003
7. Add comprehensive unit tests for each algorithm

### Phase 2: Update MCP Tool (Week 1-2)

1. Add `algorithm` parameter to `nc_semantic_search()`
2. Add weight parameters (`semantic_weight`, etc.)
3. Implement algorithm dispatcher
4. Add parameter validation (weights sum ≤1.0)
5. Update response model to include algorithm metadata
6. Maintain backward compatibility (default: hybrid)
7. Add integration tests for all algorithm modes

### Phase 3: Update Viz Pane (Week 2)

**Critical: All processing must happen server-side**

1. **Remove client-side search filtering**
   - Delete JavaScript-based keyword/fuzzy matching
   - Remove client-side document type filtering
   - No search logic in browser
2. **Implement server-side endpoint** (`/app/vector-viz`)
   - Accept query, algorithm, weights, doc_type filters
   - Execute search via `search/algorithms.py`
   - Filter results by user_id (security)
   - Perform PCA reduction (768-dim → 2D)
   - Return JSON with 2D coordinates + metadata only
3. **Update frontend**
   - htmx form submission to `/app/vector-viz`
   - Algorithm selector dropdown
   - Weight adjustment sliders (htmx updates on change)
   - Document type checkboxes
   - Plotly.js visualization of server response
4. **Performance optimization**
   - Limit results to user's documents only
   - Cache PCA transformation (invalidate on new vectors)
   - Stream large result sets if needed
   - Add loading indicators for server processing

### Phase 4: Documentation and Testing (Week 2-3)

1. Update MCP tool documentation
2. Add algorithm selection guide
3. Document weight tuning recommendations
4. Add end-to-end tests (MCP + viz pane)
5. Performance benchmarks for each algorithm
6. Update CLAUDE.md with search patterns

## Consequences

### Positive

1. **Flexibility**: MCP clients can optimize search for their use case
2. **Unified implementation**: Single source of truth for search algorithms
3. **User empowerment**: Viz pane enables query testing and tuning
4. **Backward compatible**: Existing semantic search behavior preserved
5. **Extensible**: Easy to add new algorithms (BM25, neural reranking)
6. **Testable**: Each algorithm can be unit tested independently
7. **Standards-based**: RRF is proven in production systems

### Negative

1. **Complexity**: More parameters for clients to understand
2. **API surface**: Larger tool signature (8 parameters)
3. **Performance**: Hybrid search requires multiple queries
4. **Validation overhead**: Weight validation adds processing
5. **Documentation burden**: Need to explain when to use each algorithm

### Neutral

1. **Weight defaults**: May need tuning based on user feedback
2. **Algorithm performance**: Will vary by content type and query
3. **Viz pane adoption**: Unknown if users will utilize testing interface

## Alternatives Considered

### Alternative 1: Separate Tools Per Algorithm

```python
@mcp.tool()
async def nc_semantic_search(query: str, ctx: Context, ...) -> SearchResponse:
    """Pure vector similarity search."""

@mcp.tool()
async def nc_keyword_search(query: str, ctx: Context, ...) -> SearchResponse:
    """Pure keyword matching."""

@mcp.tool()
async def nc_hybrid_search(query: str, ctx: Context, weights: dict, ...) -> SearchResponse:
    """Hybrid search with weights."""
```

**Rejected because**:
- API proliferation (3+ tools instead of 1)
- Harder to discover capabilities
- Backward compatibility issues
- DRY violation (repeated parameters)

### Alternative 2: Server-Wide Configuration Only

```python
# .env configuration
SEARCH_ALGORITHM=hybrid
SEMANTIC_WEIGHT=0.5
KEYWORD_WEIGHT=0.3
FUZZY_WEIGHT=0.2
```

**Rejected because**:
- No per-query flexibility
- MCP clients cannot optimize for different tasks
- Requires server restart for changes
- User's requirement: "expose a way for users to override the default weights"

### Alternative 3: Production-Grade Fuzzy (Levenshtein/RapidFuzz)

**Rejected because**:
- Adds external dependency
- Simple character overlap performs adequately
- Can always upgrade later if needed
- User's preference: "Keep simple character overlap"

## Related ADRs

- **ADR-001**: Enhanced Note Search (keyword algorithm design)
- **ADR-003**: Vector Database and Semantic Search (hybrid search + RRF design)
- **ADR-007**: Background Vector Sync (semantic search implementation)
- **ADR-008**: MCP Sampling for RAG (uses semantic search results)
- **ADR-009**: Semantic Search OAuth Scope (security model)
- **ADR-011**: Improving Semantic Search Quality (mentions future "ADR-013" for hybrid search)

**This ADR supersedes**:
- ADR-011's placeholder for "ADR-013: Hybrid Search"

**This ADR implements**:
- ADR-003's hybrid search design (previously unimplemented)
- ADR-001's keyword search design (previously unimplemented)

## References

- **Reciprocal Rank Fusion**: Cormack, G. V., Clarke, C. L., & Buettcher, S. (2009). "Reciprocal rank fusion outperforms condorcet and individual rank learning methods." SIGIR '09.
- **Vector Search**: Malkov, Y. A., & Yashunin, D. A. (2018). "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs." TPAMI.
- **Hybrid Search Best Practices**: Qdrant documentation on hybrid search patterns
- **MCP Protocol**: Model Context Protocol specification for tool design

## Implementation Notes

### Weight Validation

```python
def validate_weights(
    semantic_weight: float,
    keyword_weight: float,
    fuzzy_weight: float
) -> None:
    """Validate hybrid search weights."""
    if semantic_weight < 0 or keyword_weight < 0 or fuzzy_weight < 0:
        raise ValueError("Weights must be non-negative")

    total = semantic_weight + keyword_weight + fuzzy_weight
    if total > 1.0:
        raise ValueError(f"Weights sum to {total:.2f}, must be ≤1.0")

    if total == 0.0:
        raise ValueError("At least one weight must be > 0")
```

### Backward Compatibility

The default behavior (`algorithm="hybrid"` with balanced weights) provides better results than current pure semantic search, while maintaining the same tool name and signature structure. Existing clients will automatically benefit from hybrid search without code changes.

### Performance Considerations

- **Semantic search**: ~50-200ms (vector DB query)
- **Keyword search**: ~10-50ms (in-memory token matching)
- **Fuzzy search**: ~20-100ms (character comparison)
- **Hybrid search**: ~100-300ms (parallel execution + fusion)

Parallel execution of algorithms minimizes hybrid search latency.

### Security Model

All algorithms respect the same security boundaries:
1. **User filtering**: Qdrant queries filter by `user_id`
2. **Access verification**: Results verified via Nextcloud API
3. **OAuth scope**: `semantic:read` required for all algorithms
4. **Viz pane**: Shows only current user's documents

## Success Metrics

1. **Adoption**: % of MCP clients using algorithm parameter
2. **Performance**: Search latency percentiles (p50, p95, p99)
3. **Quality**: User satisfaction with result relevance
4. **Viz pane usage**: % of users accessing testing interface
5. **Weight distribution**: Most common weight configurations

## Future Enhancements

1. **Additional algorithms**: BM25, TF-IDF, neural reranking
2. **Auto-tuning**: Learn optimal weights per user
3. **Query analysis**: Automatic algorithm selection based on query
4. **Cross-app search**: Extend beyond notes to calendar, files, etc.
5. **Feedback loop**: Use click-through rate to improve weights
