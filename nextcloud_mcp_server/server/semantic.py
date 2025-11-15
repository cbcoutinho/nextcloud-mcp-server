"""Semantic search MCP tools using vector database."""

import logging
from typing import Literal

from httpx import RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import (
    ErrorData,
    ModelHint,
    ModelPreferences,
    SamplingMessage,
    TextContent,
)

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.semantic import (
    SamplingSearchResponse,
    SemanticSearchResponse,
    SemanticSearchResult,
    VectorSyncStatusResponse,
)
from nextcloud_mcp_server.observability.metrics import (
    instrument_tool,
)
from nextcloud_mcp_server.search import (
    FuzzySearchAlgorithm,
    HybridSearchAlgorithm,
    KeywordSearchAlgorithm,
    SemanticSearchAlgorithm,
)

logger = logging.getLogger(__name__)


def configure_semantic_tools(mcp: FastMCP):
    """Configure semantic search tools for MCP server."""

    @mcp.tool()
    @require_scopes("semantic:read")
    @instrument_tool
    async def nc_semantic_search(
        query: str,
        ctx: Context,
        limit: int = 10,
        doc_types: list[str] | None = None,
        score_threshold: float = 0.7,
        algorithm: Literal["semantic", "keyword", "fuzzy", "hybrid"] = "hybrid",
        semantic_weight: float = 0.5,
        keyword_weight: float = 0.3,
        fuzzy_weight: float = 0.2,
    ) -> SemanticSearchResponse:
        """
        Search Nextcloud content using configurable algorithms with cross-app support.

        Supports multiple search algorithms with client-configurable weighting:
        - semantic: Vector similarity search (requires VECTOR_SYNC_ENABLED=true)
        - keyword: Token-based matching (title matches weighted 3x)
        - fuzzy: Character overlap matching (typo-tolerant)
        - hybrid: Combines all algorithms using Reciprocal Rank Fusion (default)

        Document types are queried from the vector database to determine what's
        actually indexed. Currently only "note" documents are fully supported.

        Args:
            query: Natural language search query
            limit: Maximum number of results to return (default: 10)
            doc_types: Document types to search (e.g., ["note", "file"]). None = search all indexed types (default)
            score_threshold: Minimum similarity score for semantic/hybrid (0-1, default: 0.7)
            algorithm: Search algorithm to use (default: "hybrid")
            semantic_weight: Weight for semantic results in hybrid mode (default: 0.5)
            keyword_weight: Weight for keyword results in hybrid mode (default: 0.3)
            fuzzy_weight: Weight for fuzzy results in hybrid mode (default: 0.2)

        Returns:
            SemanticSearchResponse with matching documents and relevance scores
        """
        from nextcloud_mcp_server.config import get_settings

        settings = get_settings()
        client = await get_client(ctx)
        username = client.username

        logger.info(
            f"Search: query='{query}', user={username}, algorithm={algorithm}, "
            f"limit={limit}, score_threshold={score_threshold}"
        )

        try:
            # Create appropriate algorithm instance
            if algorithm == "semantic":
                if not settings.vector_sync_enabled:
                    raise McpError(
                        ErrorData(
                            code=-1,
                            message="Semantic search requires VECTOR_SYNC_ENABLED=true",
                        )
                    )
                search_algo = SemanticSearchAlgorithm(score_threshold=score_threshold)
            elif algorithm == "keyword":
                search_algo = KeywordSearchAlgorithm()
            elif algorithm == "fuzzy":
                search_algo = FuzzySearchAlgorithm()
            elif algorithm == "hybrid":
                if semantic_weight > 0 and not settings.vector_sync_enabled:
                    raise McpError(
                        ErrorData(
                            code=-1,
                            message="Hybrid search with semantic component requires VECTOR_SYNC_ENABLED=true",
                        )
                    )
                search_algo = HybridSearchAlgorithm(
                    semantic_weight=semantic_weight,
                    keyword_weight=keyword_weight,
                    fuzzy_weight=fuzzy_weight,
                )
            else:
                raise McpError(
                    ErrorData(code=-1, message=f"Unknown algorithm: {algorithm}")
                )

            # Execute search across requested document types
            # If doc_types is None, search all indexed types (cross-app search)
            # If doc_types is a list, search only those types
            all_results = []

            if doc_types is None:
                # Cross-app search: search all indexed types
                # Pass None to search algorithm to let it query Qdrant for available types
                search_results = await search_algo.search(
                    query=query,
                    user_id=username,
                    limit=limit,
                    doc_type=None,  # Signal to search all types
                    nextcloud_client=client,
                    score_threshold=score_threshold,
                )
                all_results.extend(search_results)
            else:
                # Search specific document types
                # For each requested type, execute search and combine results
                for dtype in doc_types:
                    search_results = await search_algo.search(
                        query=query,
                        user_id=username,
                        limit=limit * 2,  # Get extra for combining
                        doc_type=dtype,
                        nextcloud_client=client,
                        score_threshold=score_threshold,
                    )
                    all_results.extend(search_results)

                # Sort combined results by score and limit
                all_results.sort(key=lambda r: r.score, reverse=True)
                all_results = all_results[:limit]

            search_results = all_results

            # Convert SearchResult objects to SemanticSearchResult for response
            results = []
            for r in search_results:
                results.append(
                    SemanticSearchResult(
                        id=r.id,
                        doc_type=r.doc_type,
                        title=r.title,
                        category=r.metadata.get("category", "") if r.metadata else "",
                        excerpt=r.excerpt,
                        score=r.score,
                        chunk_index=r.metadata.get("chunk_index", 0)
                        if r.metadata
                        else 0,
                        total_chunks=r.metadata.get("total_chunks", 1)
                        if r.metadata
                        else 1,
                    )
                )

            logger.info(f"Returning {len(results)} results from {algorithm} search")

            return SemanticSearchResponse(
                results=results,
                query=query,
                total_found=len(results),
                search_method=algorithm,
            )

        except ValueError as e:
            error_msg = str(e)
            if "No embedding provider configured" in error_msg:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Embedding service not configured. Set OLLAMA_BASE_URL environment variable.",
                    )
                )
            raise McpError(
                ErrorData(code=-1, message=f"Configuration error: {error_msg}")
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error during search: {str(e)}")
            )
        except Exception as e:
            logger.error(f"Search error: {e}", exc_info=True)
            raise McpError(ErrorData(code=-1, message=f"Search failed: {str(e)}"))

    @mcp.tool()
    @require_scopes("semantic:read")
    @instrument_tool
    async def nc_semantic_search_answer(
        query: str,
        ctx: Context,
        limit: int = 5,
        score_threshold: float = 0.7,
        max_answer_tokens: int = 500,
    ) -> SamplingSearchResponse:
        """
        Semantic search with LLM-generated answer using MCP sampling.

        Retrieves relevant documents from indexed Nextcloud apps (notes, calendar, deck,
        files, contacts) using vector similarity search, then uses MCP sampling to request
        the client's LLM to generate a natural language answer based on the retrieved context.

        This tool combines the power of semantic search (finding relevant content across
        all your Nextcloud apps) with LLM generation (synthesizing that content into
        coherent answers). The generated answer includes citations to specific documents
        with their types, allowing users to verify claims and explore sources.

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

        # 3. Check if client supports sampling
        from mcp.types import ClientCapabilities, SamplingCapability

        client_has_sampling = ctx.session.check_client_capability(
            ClientCapabilities(sampling=SamplingCapability())
        )

        # Log capability check result for debugging
        logger.info(
            f"Sampling capability check: client_has_sampling={client_has_sampling}, "
            f"query='{query}'"
        )
        if hasattr(ctx.session, "_client_params") and ctx.session._client_params:
            client_caps = ctx.session._client_params.capabilities
            logger.debug(
                f"Client advertised capabilities: "
                f"roots={client_caps.roots is not None}, "
                f"sampling={client_caps.sampling is not None}, "
                f"experimental={client_caps.experimental is not None}"
            )

        if not client_has_sampling:
            logger.info(
                f"Client does not support sampling (query: '{query}'), "
                f"returning {len(search_response.results)} documents"
            )
            return SamplingSearchResponse(
                query=query,
                generated_answer=(
                    f"[Sampling not supported by client]\n\n"
                    f"Your MCP client doesn't support answer generation. "
                    f"Found {search_response.total_found} relevant documents. "
                    f"Please review the sources below."
                ),
                sources=search_response.results,
                total_found=search_response.total_found,
                search_method="semantic_sampling_unsupported",
                success=True,
            )

        # 4. Fetch full content for notes to provide complete context to LLM
        # Filter out inaccessible notes (deleted or permissions changed)
        client = await get_client(ctx)
        accessible_results = []
        full_contents = []  # Full content for accessible notes

        for result in search_response.results:
            if result.doc_type == "note":
                try:
                    note = await client.notes.get_note(result.id)
                    # Note is accessible, store full content
                    accessible_results.append(result)
                    full_contents.append(note.get("content", ""))
                    logger.debug(
                        f"Fetched full content for note {result.id} "
                        f"(length: {len(full_contents[-1])} chars)"
                    )
                except Exception as e:
                    # Note might have been deleted or permissions changed
                    # Filter it out to avoid corrupting LLM with inaccessible data
                    logger.warning(
                        f"Failed to fetch full content for note {result.id}: {e}. "
                        f"Excluding from results."
                    )
            else:
                # Non-note document types (future: calendar, deck, files)
                # For now, keep them with excerpts
                accessible_results.append(result)
                full_contents.append(None)

        # Check if we filtered out all results
        if not accessible_results:
            logger.warning(f"All search results became inaccessible for query: {query}")
            return SamplingSearchResponse(
                query=query,
                generated_answer="All matching documents are no longer accessible.",
                sources=[],
                total_found=0,
                search_method="semantic_sampling",
                success=True,
            )

        # 5. Construct context from accessible documents with full content
        context_parts = []
        for idx, (result, content) in enumerate(
            zip(accessible_results, full_contents), 1
        ):
            # Use full content if available (notes), otherwise use excerpt
            if content is not None:
                content_field = f"Content: {content}"
            else:
                content_field = f"Excerpt: {result.excerpt}"

            context_parts.append(
                f"[Document {idx}]\n"
                f"Type: {result.doc_type}\n"
                f"Title: {result.title}\n"
                f"Category: {result.category}\n"
                f"{content_field}\n"
                f"Relevance Score: {result.score:.2f}\n"
            )

        context = "\n".join(context_parts)

        # 6. Construct prompt - reuse user's query, add context and instructions
        prompt = (
            f"{query}\n\n"
            f"Here are relevant documents from Nextcloud (notes, calendar events, deck cards, files, contacts):\n\n"
            f"{context}\n\n"
            f"Based on the documents above, please provide a comprehensive answer. "
            f"Cite the document numbers when referencing specific information."
        )

        logger.info(
            f"Initiating sampling request: query_length={len(query)}, "
            f"documents={len(search_response.results)}, "
            f"prompt_length={len(prompt)}, max_tokens={max_answer_tokens}"
        )

        # 6. Request LLM completion via MCP sampling with timeout
        import anyio

        try:
            with anyio.fail_after(30):
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

            # 7. Extract answer from sampling response
            if sampling_result.content.type == "text":
                generated_answer = sampling_result.content.text
            else:
                # Handle non-text responses (shouldn't happen for text prompts)
                generated_answer = f"Received non-text response of type: {sampling_result.content.type}"
                logger.warning(
                    f"Unexpected content type from sampling: {sampling_result.content.type}"
                )

            logger.info(
                f"Sampling successful: model={sampling_result.model}, "
                f"stop_reason={sampling_result.stopReason}, "
                f"answer_length={len(generated_answer)}"
            )

            return SamplingSearchResponse(
                query=query,
                generated_answer=generated_answer,
                sources=accessible_results,
                total_found=len(accessible_results),
                search_method="semantic_sampling",
                model_used=sampling_result.model,
                stop_reason=sampling_result.stopReason,
                success=True,
            )

        except TimeoutError:
            logger.warning(
                f"Sampling request timed out after 30 seconds for query: '{query}', "
                f"returning search results only"
            )
            return SamplingSearchResponse(
                query=query,
                generated_answer=(
                    f"[Sampling request timed out]\n\n"
                    f"The answer generation took too long (>30s). "
                    f"Found {len(accessible_results)} relevant documents. "
                    f"Please review the sources below or try a simpler query."
                ),
                sources=accessible_results,
                total_found=len(accessible_results),
                search_method="semantic_sampling_timeout",
                success=True,
            )

        except McpError as e:
            # Expected MCP protocol errors (user rejection, unsupported, etc.)
            error_msg = str(e)

            if "rejected" in error_msg.lower() or "denied" in error_msg.lower():
                # User explicitly declined - this is normal, not an error
                logger.info(f"User declined sampling request for query: '{query}'")
                search_method = "semantic_sampling_user_declined"
                user_message = "User declined to generate an answer"
            elif "not supported" in error_msg.lower():
                # Client doesn't support sampling - also normal
                logger.info(f"Sampling not supported by client for query: '{query}'")
                search_method = "semantic_sampling_unsupported"
                user_message = "Sampling not supported by this client"
            else:
                # Other MCP protocol errors
                logger.warning(
                    f"MCP error during sampling for query '{query}': {error_msg}"
                )
                search_method = "semantic_sampling_mcp_error"
                user_message = f"Sampling unavailable: {error_msg}"

            return SamplingSearchResponse(
                query=query,
                generated_answer=(
                    f"[{user_message}]\n\n"
                    f"Found {len(accessible_results)} relevant documents. "
                    f"Please review the sources below."
                ),
                sources=accessible_results,
                total_found=len(accessible_results),
                search_method=search_method,
                success=True,
            )

        except Exception as e:
            # Truly unexpected errors - these SHOULD have tracebacks
            logger.error(
                f"Unexpected error during sampling for query '{query}': "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

            return SamplingSearchResponse(
                query=query,
                generated_answer=(
                    f"[Unexpected error during sampling]\n\n"
                    f"Found {len(accessible_results)} relevant documents. "
                    f"Please review the sources below."
                ),
                sources=accessible_results,
                total_found=len(accessible_results),
                search_method="semantic_sampling_error",
                success=True,
            )

    @mcp.tool()
    @require_scopes("semantic:read")
    @instrument_tool
    async def nc_get_vector_sync_status(ctx: Context) -> VectorSyncStatusResponse:
        """Get the current vector sync status.

        Returns information about the vector sync process, including:
        - Number of documents indexed in the vector database
        - Number of documents pending processing
        - Current sync status (idle, syncing, or disabled)

        This is useful for determining when vector indexing is complete
        after creating or updating content across all indexed apps.
        """
        import os

        # Check if vector sync is enabled
        vector_sync_enabled = (
            os.getenv("VECTOR_SYNC_ENABLED", "false").lower() == "true"
        )

        if not vector_sync_enabled:
            return VectorSyncStatusResponse(
                indexed_count=0,
                pending_count=0,
                status="disabled",
                enabled=False,
            )

        try:
            # Get document receive stream from lifespan context
            lifespan_ctx = ctx.request_context.lifespan_context
            document_receive_stream = getattr(
                lifespan_ctx, "document_receive_stream", None
            )

            if document_receive_stream is None:
                logger.debug(
                    "document_receive_stream not available in lifespan context"
                )
                return VectorSyncStatusResponse(
                    indexed_count=0,
                    pending_count=0,
                    status="unknown",
                    enabled=True,
                )

            # Get pending count from stream statistics
            stream_stats = document_receive_stream.statistics()
            pending_count = stream_stats.current_buffer_used

            # Get Qdrant client and query indexed count
            indexed_count = 0
            try:
                from nextcloud_mcp_server.config import get_settings
                from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

                settings = get_settings()
                qdrant_client = await get_qdrant_client()

                # Count documents in collection
                count_result = await qdrant_client.count(
                    collection_name=settings.get_collection_name()
                )
                indexed_count = count_result.count

            except Exception as e:
                logger.warning(f"Failed to query Qdrant for indexed count: {e}")
                # Continue with indexed_count = 0

            # Determine status
            status = "syncing" if pending_count > 0 else "idle"

            return VectorSyncStatusResponse(
                indexed_count=indexed_count,
                pending_count=pending_count,
                status=status,
                enabled=True,
            )

        except Exception as e:
            logger.error(f"Error getting vector sync status: {e}")
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to retrieve vector sync status: {str(e)}",
                )
            )
