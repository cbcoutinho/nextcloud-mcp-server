# ADR-008: MCP Sampling for Multi-App Semantic Search with RAG

**Status**: Proposed
**Date**: 2025-01-11
**Depends On**: ADR-007 (Background Vector Sync)

## Context

ADR-007 established a background synchronization architecture that maintains a vector database of Nextcloud content across multiple apps (notes, calendar, deck, files, contacts), enabling semantic search via the `nc_semantic_search` tool. This tool returns a list of relevant documents with excerpts, similarity scores, and metadata—providing the raw materials for answering user questions.

However, users typically don't want a list of documents—they want answers to their questions. When a user asks "What are my project goals?" or "When is my next dentist appointment?", they expect a natural language response that synthesizes information from multiple sources and document types, not a ranked list of excerpts. This is the pattern of Retrieval-Augmented Generation (RAG): retrieve relevant context from all Nextcloud apps, then generate a cohesive answer.

The challenge is: who should generate the answer, and how?

**Option 1: Server-side LLM**
The MCP server could maintain its own LLM connection (OpenAI API, Ollama, etc.), construct prompts from retrieved documents, and return generated answers directly. This approach has significant drawbacks:

- **Duplicate infrastructure**: MCP clients (like Claude Desktop) already have LLM capabilities. The server would duplicate this with its own LLM integration, API keys, and configuration.
- **Cost and billing**: The server operator bears LLM costs for all users, creating billing and quota management challenges.
- **Limited model choice**: Users are locked into whatever LLM the server configures. They cannot choose their preferred model or provider.
- **Privacy concerns**: User queries and document contents flow through a server-controlled LLM, creating a potential privacy boundary.
- **Configuration complexity**: Server operators must configure embedding services (for search) AND generation models (for answers), each with different API keys, rate limits, and failure modes.

**Option 2: Return documents, let client generate**
The server could simply return retrieved documents and rely on the MCP client's existing LLM to generate answers. The user would call `nc_notes_semantic_search`, receive documents, and then the client would include those documents in its context when responding to the user's original question. This approach also has limitations:

- **Context window waste**: The client must include all document content in its context window, even if only small excerpts are relevant. For 5-10 documents, this can consume significant context space.
- **Inconsistent behavior**: Whether the client synthesizes an answer or just displays documents depends on the client's implementation and the user's conversational style. There's no guaranteed answer generation.
- **Poor citations**: The client may generate an answer but fail to cite which specific documents were used, making it hard to verify claims.
- **User confusion**: Users see a tool that returns "search results" rather than "answers", requiring them to explicitly ask for synthesis.

**Option 3: MCP Sampling**
The Model Context Protocol specification includes a **sampling** capability that allows MCP servers to request LLM completions from their clients. The server constructs a prompt with retrieved context, sends it to the client via `sampling/createMessage`, and the client's LLM generates a response that the server can return as a tool result.

This approach combines the best of both options:

- **No server-side LLM**: The server has no API keys, no LLM configuration, no billing concerns.
- **User choice**: The MCP client controls which LLM is used (Claude, GPT-4, local Ollama) and who pays for it.
- **User transparency**: MCP clients SHOULD present sampling requests to users for approval, making it clear when the server is requesting an LLM call.
- **Consistent citations**: The server constructs a prompt that explicitly includes document references, ensuring generated answers cite sources.
- **Single tool call**: Users call one tool (`nc_notes_semantic_search_answer`) and receive a complete answer with citations—no multi-turn conversation needed.

The sampling approach shifts responsibility appropriately: the MCP server is responsible for information retrieval and context construction (its expertise), while the MCP client is responsible for LLM access and user preferences (its expertise). This follows the MCP design philosophy of separating concerns between servers (data access) and clients (user interaction).

However, sampling introduces new considerations:

**Client compatibility**: Not all MCP clients implement sampling. The server must gracefully degrade when sampling is unavailable, falling back to returning documents without generated answers.

**Latency**: Sampling adds a full round-trip to the client and back, plus LLM generation time. A typical flow involves: (1) client calls tool, (2) server retrieves documents, (3) server requests sampling from client, (4) client generates answer, (5) server returns answer to client. This can take 2-5 seconds depending on LLM speed, compared to 100-500ms for document retrieval alone.

**User approval**: MCP clients SHOULD prompt users to approve sampling requests, allowing users to review the prompt before sending it to their LLM. This is a privacy and security feature (prevents servers from making arbitrary LLM requests) but adds interaction friction.

**Prompt engineering**: The server must construct effective prompts that guide the LLM to generate useful, well-cited answers. Unlike Option 1 where the server controls the LLM directly, the server has less control over how the prompt is interpreted.

Despite these considerations, MCP sampling provides the most principled solution for RAG-enhanced semantic search. It respects the client-server boundary, avoids duplicate infrastructure, and delivers the user experience users expect from semantic search tools.

This ADR proposes adding a new tool, `nc_semantic_search_answer`, that uses MCP sampling to generate natural language answers from retrieved Nextcloud content across all indexed apps (notes, calendar, deck, files, contacts).

## Decision

We will implement a new MCP tool `nc_semantic_search_answer` that retrieves relevant documents via vector similarity search across all indexed Nextcloud apps and uses MCP sampling to generate natural language answers. The tool will construct a prompt that includes the user's original query and excerpts from retrieved documents (notes, calendar events, deck cards, files, contacts), request an LLM completion via `ctx.session.create_message()`, and return the generated answer along with source citations.

The existing `nc_semantic_search` tool will remain unchanged, providing users with a choice: call the original tool for raw document results, or call the new sampling-enhanced tool for generated answers. This dual-tool approach respects different use cases—some users want to browse documents, others want direct answers.

### API Design

**Tool Signature**:
```python
@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search_answer(
    query: str,
    ctx: Context,
    limit: int = 5,
    score_threshold: float = 0.7,
    max_answer_tokens: int = 500,
) -> SamplingSearchResponse
```

**Parameters**:
- `query`: The user's natural language question
- `ctx`: MCP context for session access
- `limit`: Maximum documents to retrieve (default 5)
- `score_threshold`: Minimum similarity score 0-1 (default 0.7)
- `max_answer_tokens`: Maximum tokens for generated answer (default 500)

**Response Model**:
```python
class SamplingSearchResponse(BaseResponse):
    query: str                              # Original user query
    generated_answer: str                   # LLM-generated answer
    sources: list[SemanticSearchResult]     # Supporting documents
    total_found: int                        # Total matching documents
    search_method: str = "semantic_sampling"
    model_used: str | None = None           # Model that generated answer
    stop_reason: str | None = None          # Why generation stopped
```

The response includes both the generated answer (for direct user consumption) and the source documents (for verification and citation). The `model_used` field records which LLM generated the answer, allowing users to understand which model provided the response.

### Sampling API Usage

The tool uses the MCP Python SDK's `ServerSession.create_message()` API:

```python
from mcp.types import SamplingMessage, TextContent, ModelPreferences, ModelHint

# Construct prompt with retrieved context
prompt = (
    f"{query}\n\n"
    f"Here are relevant documents from Nextcloud (notes, calendar events, deck cards, files, contacts):\n\n"
    f"{context}\n\n"
    f"Based on the documents above, please provide a comprehensive answer. "
    f"Cite the document numbers when referencing specific information."
)

# Request LLM completion via MCP sampling
sampling_result = await ctx.session.create_message(
    messages=[
        SamplingMessage(
            role="user",
            content=TextContent(type="text", text=prompt),
        )
    ],
    max_tokens=max_answer_tokens,
    temperature=0.7,
    model_preferences=ModelPreferences(
        hints=[ModelHint(name="claude-3-5-sonnet")],
        intelligencePriority=0.8,
        speedPriority=0.5,
    ),
    include_context="thisServer",
)

# Extract answer from response
if sampling_result.content.type == "text":
    generated_answer = sampling_result.content.text
```

**Key parameters**:
- `messages`: Chat-style messages with role ("user" or "assistant") and content
- `max_tokens`: Limits response length to control costs and latency
- `temperature`: 0.7 balances creativity with consistency for factual answers
- `model_preferences`: Hints suggest Claude Sonnet for balanced intelligence/speed
- `include_context`: "thisServer" includes MCP server context in client's LLM call

The `include_context` parameter is particularly important. When set to "thisServer", the MCP client provides its LLM with context about the server's capabilities, tools, and resources. This allows the LLM to reference the Nextcloud MCP server when generating answers, creating more contextually appropriate responses. For example, the LLM might say "Based on your Nextcloud Notes..." rather than generic phrasing.

### Prompt Construction

The prompt construction follows a structured template:

```
[User's original query]

Here are relevant documents from Nextcloud (notes, calendar events, deck cards, files, contacts):

[Document 1]
Type: note
Title: Project Kickoff Notes
Category: Work
Excerpt: The primary goal for Q1 2025 is to improve semantic search...
Relevance Score: 0.92

[Document 2]
Type: calendar_event
Title: Team Planning Meeting
Location: Conference Room A
Excerpt: Scheduled for Jan 15 at 2pm. Agenda: Discuss Q1 objectives and timeline...
Relevance Score: 0.88

[Document 3]
Type: deck_card
Title: Implement semantic search
Labels: feature, high-priority
Excerpt: This card tracks the semantic search implementation. Due: Jan 30...
Relevance Score: 0.85

Based on the documents above, please provide a comprehensive answer.
Cite the document numbers when referencing specific information.
```

This structure ensures:
- The user's original query is preserved verbatim
- Documents are clearly delineated and numbered for citation
- Metadata (title, category, score) provides context
- Explicit instruction to cite sources encourages proper attribution

The prompt is intentionally simple and fixed (not configurable). Allowing users to customize the prompt would complicate the API and introduce prompt injection risks. The fixed structure ensures consistent, well-cited answers across all users.

### Fallback Behavior

Sampling may fail for several reasons:
- Client doesn't support sampling (e.g., MCP Inspector without callbacks)
- User declines the sampling request
- Network errors during sampling round-trip
- LLM generation errors

The tool handles all failures gracefully by falling back to returning documents without a generated answer:

```python
try:
    sampling_result = await ctx.session.create_message(...)
    generated_answer = sampling_result.content.text
except Exception as e:
    logger.warning(f"Sampling failed: {e}, returning search results only")
    generated_answer = (
        f"[Sampling unavailable: {str(e)}]\n\n"
        f"Found {total_found} relevant documents. Please review the sources below."
    )
```

This ensures the tool always returns useful information—either a generated answer or the underlying documents—rather than failing completely. The user knows sampling was attempted (via the `[Sampling unavailable]` prefix) and can still access the retrieved context.

### No Results Handling

When semantic search finds no relevant documents (all below `score_threshold`), the tool returns a clear message without attempting sampling:

```python
if not search_response.results:
    return SamplingSearchResponse(
        query=query,
        generated_answer="No relevant documents found in your Nextcloud content for this query.",
        sources=[],
        total_found=0,
        search_method="semantic_sampling",
        success=True,
    )
```

This avoids wasting a sampling call (and user approval) when there's no content to base an answer on.

### User Experience Flow

**Typical successful flow**:
1. User calls `nc_semantic_search_answer` with query "What are my Q1 2025 objectives?"
2. Server retrieves 5 relevant documents via vector search (2 notes, 2 calendar events, 1 deck card)
3. Server constructs prompt with document excerpts showing mixed content types
4. Server sends `sampling/createMessage` request to client
5. Client prompts user: "MCP server wants to generate an answer using these documents. Allow?"
6. User approves (or client auto-approves based on configuration)
7. Client sends prompt to LLM (Claude, GPT-4, etc.)
8. LLM generates answer with citations: "Based on Document 1 (note: Project Kickoff), Document 2 (calendar: Team Planning Meeting), and Document 3 (deck card: Implement semantic search)..."
9. Client returns answer to server
10. Server returns `SamplingSearchResponse` with answer and sources
11. User sees complete answer with citations across multiple Nextcloud apps

**Fallback flow** (sampling unavailable):
1-3. Same as above
4. Server attempts `ctx.session.create_message()`
5. Client raises exception: "Sampling not supported"
6. Server catches exception, logs warning
7. Server returns `SamplingSearchResponse` with documents and "[Sampling unavailable]" message
8. User sees raw documents instead of generated answer

**No results flow**:
1-2. Same as above but no documents match threshold
3. Server returns `SamplingSearchResponse` with "No relevant documents" message
4. No sampling attempted (no prompt sent)
5. User sees clear "not found" message

This three-tier approach (answer → documents → error message) ensures users always receive useful feedback appropriate to the situation.

## Implementation

### Response Model

Add to `nextcloud_mcp_server/models/semantic.py` (new file for semantic search models):

```python
from pydantic import Field

class SamplingSearchResponse(BaseResponse):
    """Response from semantic search with LLM-generated answer via MCP sampling.

    This response includes both a generated natural language answer (created by
    the MCP client's LLM via sampling) and the source documents used to generate
    that answer. Users can read the answer for quick information and review
    sources for verification and deeper exploration.

    Attributes:
        query: The original user query
        generated_answer: Natural language answer generated by client's LLM
        sources: List of semantic search results used as context
        total_found: Total number of matching documents found
        search_method: Always "semantic_sampling" for this response type
        model_used: Name of model that generated the answer (e.g., "claude-3-5-sonnet")
        stop_reason: Why generation stopped ("endTurn", "maxTokens", etc.)
    """

    query: str = Field(..., description="Original user query")
    generated_answer: str = Field(
        ...,
        description="LLM-generated answer based on retrieved documents"
    )
    sources: list[SemanticSearchResult] = Field(
        default_factory=list,
        description="Source documents with excerpts and relevance scores"
    )
    total_found: int = Field(..., description="Total matching documents")
    search_method: str = Field(
        default="semantic_sampling",
        description="Search method used"
    )
    model_used: str | None = Field(
        default=None,
        description="Model that generated the answer"
    )
    stop_reason: str | None = Field(
        default=None,
        description="Reason generation stopped"
    )
```

### Tool Implementation

Add to `nextcloud_mcp_server/server/semantic.py` (new file for semantic search tools):

```python
import logging
from mcp.types import ModelHint, ModelPreferences, SamplingMessage, TextContent

logger = logging.getLogger(__name__)


@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search_answer(
    query: str,
    ctx: Context,
    limit: int = 5,
    score_threshold: float = 0.7,
    max_answer_tokens: int = 500,
) -> SamplingSearchResponse:
    """
    Semantic search with LLM-generated answer using MCP sampling.

    Retrieves relevant documents from Nextcloud across all indexed apps (notes,
    calendar, deck, files, contacts) using vector similarity search, then uses
    MCP sampling to request the client's LLM to generate a natural language
    answer based on the retrieved context.

    This tool combines the power of semantic search (finding relevant content
    across all your Nextcloud apps) with LLM generation (synthesizing that
    content into coherent answers). The generated answer includes citations
    to specific documents with their types, allowing users to verify claims
    and explore sources.

    The LLM generation happens client-side via MCP sampling. The MCP client
    controls which model is used, who pays for it, and whether to prompt the
    user for approval. This keeps the server simple (no LLM API keys needed)
    while giving users full control over their LLM interactions.

    Args:
        query: Natural language question to answer (e.g., "What are my Q1 objectives?" or "When is my next dentist appointment?")
        ctx: MCP context for session access
        limit: Maximum number of documents to retrieve (default: 5)
        score_threshold: Minimum similarity score 0-1 (default: 0.7)
        max_answer_tokens: Maximum tokens for generated answer (default: 500)

    Returns:
        SamplingSearchResponse containing:
        - generated_answer: Natural language answer with citations
        - sources: List of documents with excerpts and relevance scores
        - model_used: Which model generated the answer
        - stop_reason: Why generation stopped

    Note: Requires MCP client to support sampling. If sampling is unavailable,
    the tool gracefully degrades to returning documents with an explanation.
    The client may prompt the user to approve the sampling request.

    Examples:
        >>> # Query about objectives across multiple apps
        >>> result = await nc_semantic_search_answer(
        ...     query="What are my Q1 2025 project goals?",
        ...     ctx=ctx
        ... )
        >>> print(result.generated_answer)
        "Based on Document 1 (note: Project Kickoff), Document 2 (calendar event:
        Q1 Planning Meeting), and Document 3 (deck card: Implement semantic search),
        your main goals are: 1) Improve semantic search accuracy by 20%,
        2) Deploy new embedding model, 3) Reduce indexing latency..."

        >>> # Query about appointments
        >>> result = await nc_semantic_search_answer(
        ...     query="When is my next dentist appointment?",
        ...     ctx=ctx,
        ...     limit=10
        ... )
        >>> len(result.sources)  # Calendar events and related notes
        3
    """
    # 1. Retrieve relevant documents via existing semantic search
    search_response = await nc_semantic_search(
        query=query,
        ctx=ctx,
        limit=limit,
        score_threshold=score_threshold,
    )

    # 2. Handle no results case - don't waste a sampling call
    if not search_response.results:
        logger.debug(f"No documents found for query: {query}")
        return SamplingSearchResponse(
            query=query,
            generated_answer="No relevant documents found in your Nextcloud content for this query.",
            sources=[],
            total_found=0,
            search_method="semantic_sampling",
            success=True,
        )

    # 3. Construct context from retrieved documents
    context_parts = []
    for idx, result in enumerate(search_response.results, 1):
        context_parts.append(
            f"[Document {idx}]\n"
            f"Title: {result.title}\n"
            f"Category: {result.category}\n"
            f"Excerpt: {result.excerpt}\n"
            f"Relevance Score: {result.score:.2f}\n"
        )

    context = "\n".join(context_parts)

    # 4. Construct prompt - reuse user's query, add context and instructions
    prompt = (
        f"{query}\n\n"
        f"Here are relevant documents from Nextcloud (notes, calendar events, deck cards, files, contacts):\n\n"
        f"{context}\n\n"
        f"Based on the documents above, please provide a comprehensive answer. "
        f"Cite the document numbers when referencing specific information."
    )

    logger.debug(
        f"Requesting sampling for query: {query} "
        f"({len(search_response.results)} documents retrieved)"
    )

    # 5. Request LLM completion via MCP sampling
    try:
        sampling_result = await ctx.session.create_message(
            messages=[
                SamplingMessage(
                    role="user",
                    content=TextContent(type="text", text=prompt),
                )
            ],
            max_tokens=max_answer_tokens,
            temperature=0.7,
            model_preferences=ModelPreferences(
                hints=[ModelHint(name="claude-3-5-sonnet")],
                intelligencePriority=0.8,
                speedPriority=0.5,
            ),
            include_context="thisServer",
        )

        # 6. Extract answer from sampling response
        if sampling_result.content.type == "text":
            generated_answer = sampling_result.content.text
        else:
            # Handle non-text responses (shouldn't happen for text prompts)
            generated_answer = (
                f"Received non-text response of type: {sampling_result.content.type}"
            )
            logger.warning(
                f"Unexpected content type from sampling: {sampling_result.content.type}"
            )

        logger.info(
            f"Sampling successful: model={sampling_result.model}, "
            f"stop_reason={sampling_result.stopReason}"
        )

        return SamplingSearchResponse(
            query=query,
            generated_answer=generated_answer,
            sources=search_response.results,
            total_found=search_response.total_found,
            search_method="semantic_sampling",
            model_used=sampling_result.model,
            stop_reason=sampling_result.stopReason,
            success=True,
        )

    except Exception as e:
        # Fallback: Return documents without generated answer
        logger.warning(
            f"Sampling failed ({type(e).__name__}: {e}), "
            f"returning search results only"
        )

        return SamplingSearchResponse(
            query=query,
            generated_answer=(
                f"[Sampling unavailable: {str(e)}]\n\n"
                f"Found {search_response.total_found} relevant documents. "
                f"Please review the sources below."
            ),
            sources=search_response.results,
            total_found=search_response.total_found,
            search_method="semantic_sampling_fallback",
            success=True,
        )
```

### Import Updates

Add to top of `nextcloud_mcp_server/server/semantic.py`:

```python
from mcp.types import ModelHint, ModelPreferences, SamplingMessage, TextContent
```

Add to `nextcloud_mcp_server/models/semantic.py` exports:

```python
__all__ = [
    "SemanticSearchResult",
    "SemanticSearchResponse",
    "SamplingSearchResponse",
]
```

## Consequences

### Benefits

**Improved User Experience**: Users receive direct answers to questions rather than lists of documents, matching expectations from modern AI interfaces.

**Proper Attribution**: Generated answers include citations to source documents, allowing users to verify claims and explore deeper.

**No Server-Side LLM**: The server has no LLM dependencies, API keys, or billing concerns. All LLM interactions happen client-side.

**User Control**: MCP clients control which model is used and may prompt users to approve sampling requests, maintaining transparency and user agency.

**Graceful Degradation**: The tool works even when sampling is unavailable, falling back to returning documents. Existing clients continue working without changes.

**Consistent Architecture**: Follows MCP's client-server separation: servers provide data access, clients provide user interaction and LLM capabilities.

### Limitations

**Sampling Support Required**: Not all MCP clients implement sampling. Users with basic clients see fallback behavior (documents without answers).

**Added Latency**: Sampling adds 2-5 seconds to tool execution due to client round-trip and LLM generation time. Users must wait longer for answers than for raw search results.

**User Approval Friction**: MCP clients SHOULD prompt users to approve sampling requests. This adds an extra interaction step before answers are generated.

**Limited Prompt Control**: The server cannot fully control how the client's LLM interprets the prompt. Different models may generate different quality answers.

**No Caching**: Each query requires a new sampling call. The server doesn't cache generated answers (clients may cache if they choose).

**Token Costs**: LLM generation consumes tokens from the user's or client's quota. Heavy users may incur costs or hit rate limits.

### Performance Characteristics

**Typical latency**:
- Document retrieval (vector search): 100-300ms
- Sampling round-trip (client communication): 50-200ms
- LLM generation (client-side): 1-4 seconds
- **Total**: 2-5 seconds end-to-end

**Throughput**: Sampling is fully async. The server can handle multiple concurrent sampling requests (limited by MCP client's concurrency, not server capacity).

**Resource usage**: Minimal server-side. No GPU, no LLM model loading, no large memory requirements. Sampling happens entirely client-side.

### Security Considerations

**Prompt Injection Risk**: If user queries contain adversarial text designed to manipulate LLM behavior, those queries are included verbatim in the sampling prompt. Mitigation: The structured prompt format and explicit instructions ("based on documents above") constrain LLM behavior.

**Data Privacy**: User queries and document excerpts are sent to the client's LLM. For cloud LLMs (OpenAI, Anthropic), this means data leaves the server's control. Mitigation: MCP clients SHOULD present sampling requests to users for approval, making data flows transparent. Users choose their LLM provider.

**Sampling Abuse**: A malicious server could spam sampling requests to drain user quotas. Mitigation: MCP clients control approval and can rate-limit or block sampling from misbehaving servers.

## Alternatives Considered

### Server-Side LLM Integration

**Approach**: Configure the MCP server with OpenAI API key or local Ollama instance. Generate answers server-side.

**Rejected Because**:
- Duplicates LLM infrastructure that MCP clients already have
- Creates billing and API key management burden for server operators
- Locks users into server-configured models
- Violates MCP's client-server separation principle

### Multi-Turn Conversation Pattern

**Approach**: `nc_notes_semantic_search` returns documents. User asks follow-up question. Client's LLM uses previous tool results as context.

**Rejected Because**:
- Requires users to know to ask follow-up questions
- Consumes context window with full document content
- Inconsistent behavior across clients
- Poor citation (LLM may not reference which documents it used)

### Pre-Generated Summaries

**Approach**: Generate and cache summaries during indexing. Return summaries instead of excerpts.

**Rejected Because**:
- Summaries become stale as documents change
- Summary quality depends on server-side LLM (same problems as server-side generation)
- Summaries are generic, not tailored to specific queries

### Streaming Responses

**Approach**: Use MCP sampling with streaming to return incremental answer chunks.

**Deferred Because**:
- MCP sampling streaming support unclear in current specification
- Adds significant implementation complexity
- Tool responses in MCP are typically atomic
- Can be added later without breaking changes

## Related Decisions

**ADR-007**: Background Vector Sync provides the semantic search infrastructure that this ADR enhances with LLM generation.

**ADR-004**: Progressive Consent architecture applies to sampling—users consent to sampling requests via MCP client approval prompts.

## References

- [MCP Specification - Sampling](https://modelcontextprotocol.io/docs/specification/2025-06-18/client/sampling)
- [MCP Python SDK - ServerSession.create_message](https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/server/session.py#L215)
- [MCP Python SDK - Sampling Example](https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/snippets/servers/sampling.py)
- [MCP Types - SamplingMessage](https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/types.py#L1038)
- [MCP Types - CreateMessageResult](https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/types.py#L1073)
- [Retrieval-Augmented Generation (RAG) - Lewis et al. 2020](https://arxiv.org/abs/2005.11401)

## Implementation Checklist

- [ ] Create ADR-008 document (this file)
- [ ] Create `nextcloud_mcp_server/models/semantic.py` for semantic search models
- [ ] Add `SamplingSearchResponse` model to `nextcloud_mcp_server/models/semantic.py`
- [ ] Create `nextcloud_mcp_server/server/semantic.py` for semantic search tools
- [ ] Implement `nc_semantic_search_answer` tool in `nextcloud_mcp_server/server/semantic.py`
- [ ] Add MCP sampling type imports (`SamplingMessage`, `TextContent`, etc.)
- [ ] Write unit tests with mocked sampling (`tests/unit/server/test_semantic.py`)
- [ ] Create integration tests (`tests/integration/test_sampling.py`)
- [ ] Update `README.md` with new tool documentation in dedicated Semantic Search section
- [ ] Update `CLAUDE.md` with sampling pattern guidance
- [ ] Test with MCP client supporting sampling (Claude Desktop, MCP Inspector with callbacks)
- [ ] Document client requirements and fallback behavior
- [ ] Update oauth-architecture.md to add semantic:read scope
- [ ] Create ADR-009 to document semantic:read scope decision
