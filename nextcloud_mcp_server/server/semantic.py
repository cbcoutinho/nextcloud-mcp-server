"""Semantic search MCP tools using vector database."""

import logging

import anyio
from httpx import RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import (
    ErrorData,
    ModelHint,
    ModelPreferences,
    SamplingMessage,
    TextContent,
    ToolAnnotations,
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
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.search.context import get_chunk_with_context

logger = logging.getLogger(__name__)


def configure_semantic_tools(mcp: FastMCP):
    """Configure semantic search tools for MCP server."""

    @mcp.tool(
        title="Semantic Search",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Search doesn't modify data
            openWorldHint=True,  # Queries external Nextcloud service
        ),
    )
    @require_scopes("semantic:read")
    @instrument_tool
    async def nc_semantic_search(
        query: str,
        ctx: Context,
        limit: int = 10,
        doc_types: list[str] | None = None,
        score_threshold: float = 0.0,
        fusion: str = "rrf",
        include_context: bool = False,
        context_chars: int = 300,
    ) -> SemanticSearchResponse:
        """
        Search Nextcloud content using BM25 hybrid search with cross-app support.

        Uses Qdrant's native hybrid search combining:
        - Dense semantic vectors: For conceptual similarity and natural language queries
        - BM25 sparse vectors: For precise keyword matching, acronyms, and specific terms

        Results are automatically fused using the selected fusion algorithm in the
        database for optimal relevance. This provides the best of both semantic
        understanding and keyword precision.

        Requires VECTOR_SYNC_ENABLED=true. Currently only "note" documents are
        fully supported for indexing.

        Args:
            query: Natural language or keyword search query
            limit: Maximum number of results to return (default: 10)
            doc_types: Document types to search (e.g., ["note", "file"]). None = search all indexed types (default)
            score_threshold: Minimum fusion score (0-1, default: 0.0)
            fusion: Fusion algorithm: "rrf" (Reciprocal Rank Fusion, default) or "dbsf" (Distribution-Based Score Fusion)
                   RRF: Good general-purpose fusion using reciprocal ranks
                   DBSF: Uses distribution-based normalization, may better balance different score ranges
            include_context: Whether to expand results with surrounding context (default: False)
            context_chars: Number of characters to include before/after matched chunk (default: 300)

        Returns:
            SemanticSearchResponse with matching documents ranked by fusion scores
        """
        from nextcloud_mcp_server.config import get_settings

        settings = get_settings()
        client = await get_client(ctx)
        username = client.username

        logger.info(
            f"BM25 hybrid search: query='{query}', user={username}, "
            f"limit={limit}, score_threshold={score_threshold}, fusion={fusion}"
        )

        # Check that vector sync is enabled
        if not settings.vector_sync_enabled:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="BM25 hybrid search requires VECTOR_SYNC_ENABLED=true",
                )
            )

        try:
            # Create BM25 hybrid search algorithm with specified fusion
            search_algo = BM25HybridSearchAlgorithm(
                score_threshold=score_threshold, fusion=fusion
            )

            # Execute search across requested document types
            # If doc_types is None, search all indexed types (cross-app search)
            # If doc_types is a list, search only those types
            all_results = []

            if doc_types is None:
                # Cross-app search: search all indexed types
                # Get unverified results from Qdrant
                unverified_results = await search_algo.search(
                    query=query,
                    user_id=username,
                    limit=limit * 2,  # Get extra for access filtering
                    doc_type=None,  # Signal to search all types
                    score_threshold=score_threshold,
                )
                all_results.extend(unverified_results)
            else:
                # Search specific document types
                # For each requested type, execute search and combine results
                for dtype in doc_types:
                    unverified_results = await search_algo.search(
                        query=query,
                        user_id=username,
                        limit=limit * 2,  # Get extra for combining and filtering
                        doc_type=dtype,
                        score_threshold=score_threshold,
                    )
                    all_results.extend(unverified_results)

                # Sort combined results by score
                all_results.sort(key=lambda r: r.score, reverse=True)

            # Note: BM25HybridSearchAlgorithm already deduplicates at chunk level
            # (doc_id, doc_type, chunk_start, chunk_end), which allows multiple
            # chunks from the same document while preventing duplicate chunks.
            # No additional deduplication needed here - multiple chunks per document
            # are valuable for RAG contexts.
            # Qdrant already filters by user_id for multi-tenant isolation.
            # Sampling tool will verify access when fetching full content.
            search_results = all_results[
                :limit
            ]  # Final limit after chunk-level dedup in algorithm

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
                        chunk_start_offset=r.chunk_start_offset,
                        chunk_end_offset=r.chunk_end_offset,
                        page_number=r.page_number,
                    )
                )

            # Expand results with surrounding context if requested
            if include_context and results:
                logger.info(
                    f"Expanding {len(results)} results with context "
                    f"(context_chars={context_chars})"
                )

                # Fetch context for all results in parallel
                # Limit concurrent requests to prevent connection pool exhaustion
                max_concurrent = 20
                semaphore = anyio.Semaphore(max_concurrent)
                expanded_results = [None] * len(results)

                async def fetch_context(index: int, result: SemanticSearchResult):
                    """Fetch context for a single result (parallel with semaphore)."""
                    async with semaphore:
                        # Only expand if we have valid chunk offsets
                        if (
                            result.chunk_start_offset is None
                            or result.chunk_end_offset is None
                        ):
                            # Keep result as-is without context expansion
                            expanded_results[index] = result
                            return

                        try:
                            chunk_context = await get_chunk_with_context(
                                nc_client=client,
                                user_id=username,
                                doc_id=result.id,
                                doc_type=result.doc_type,
                                chunk_start=result.chunk_start_offset,
                                chunk_end=result.chunk_end_offset,
                                page_number=result.page_number,
                                chunk_index=result.chunk_index,
                                total_chunks=result.total_chunks,
                                context_chars=context_chars,
                            )

                            if chunk_context:
                                # Create new result with context fields populated
                                expanded_results[index] = SemanticSearchResult(
                                    id=result.id,
                                    doc_type=result.doc_type,
                                    title=result.title,
                                    category=result.category,
                                    excerpt=result.excerpt,
                                    score=result.score,
                                    chunk_index=result.chunk_index,
                                    total_chunks=result.total_chunks,
                                    chunk_start_offset=result.chunk_start_offset,
                                    chunk_end_offset=result.chunk_end_offset,
                                    page_number=result.page_number,
                                    # Context expansion fields
                                    has_context_expansion=True,
                                    marked_text=chunk_context.marked_text,
                                    before_context=chunk_context.before_context,
                                    after_context=chunk_context.after_context,
                                    has_before_truncation=chunk_context.has_before_truncation,
                                    has_after_truncation=chunk_context.has_after_truncation,
                                )
                                logger.debug(
                                    f"Expanded context for {result.doc_type} {result.id}"
                                )
                            else:
                                # Context expansion failed, keep original result
                                expanded_results[index] = result
                                logger.debug(
                                    f"Failed to expand context for {result.doc_type} {result.id}, "
                                    "keeping original result"
                                )
                        except Exception as e:
                            # Context expansion failed, keep original result
                            expanded_results[index] = result
                            logger.warning(
                                f"Error expanding context for {result.doc_type} {result.id}: {e}"
                            )

                # Run all context fetches in parallel using anyio task group
                async with anyio.create_task_group() as tg:
                    for idx, result in enumerate(results):
                        tg.start_soon(fetch_context, idx, result)

                # Replace results with expanded versions
                results = [r for r in expanded_results if r is not None]
                logger.info(
                    f"Context expansion completed: {len(results)} results with context"
                )

            logger.info(f"Returning {len(results)} results from BM25 hybrid search")

            return SemanticSearchResponse(
                results=results,
                query=query,
                total_found=len(results),
                search_method=f"bm25_hybrid_{fusion}",
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

    @mcp.tool(
        title="Search with AI-Generated Answer",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Search doesn't modify data
            openWorldHint=False,  # Searches only indexed Nextcloud data
        ),
    )
    @require_scopes("semantic:read")
    @instrument_tool
    async def nc_semantic_search_answer(
        query: str,
        ctx: Context,
        limit: int = 5,
        score_threshold: float = 0.7,
        max_answer_tokens: int = 500,
        fusion: str = "rrf",
        include_context: bool = False,
        context_chars: int = 300,
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
            fusion: Fusion algorithm: "rrf" (Reciprocal Rank Fusion, default) or "dbsf" (Distribution-Based Score Fusion)
            include_context: Whether to expand results with surrounding context (default: False)
            context_chars: Number of characters to include before/after matched chunk (default: 300)

        Returns:
            SamplingSearchResponse containing:
            - generated_answer: Natural language answer with citations
            - sources: List of documents with excerpts and relevance scores
            - model_used: Which model generated the answer
            - stop_reason: Why generation stopped

        Note: Requires MCP client to support sampling. If sampling is unavailable,
        the tool gracefully degrades to returning documents with an explanation.
        The client may prompt the user to approve the sampling request.
        """
        # 1. Retrieve relevant documents via existing semantic search
        search_response = await nc_semantic_search(
            query=query,
            ctx=ctx,
            limit=limit,
            score_threshold=score_threshold,
            fusion=fusion,
            include_context=include_context,
            context_chars=context_chars,
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

        # 4. Fetch full content for notes in parallel (also verifies access)
        # Use anyio task group for concurrent fetching with semaphore to prevent
        # connection pool exhaustion
        client = await get_client(ctx)
        accessible_results = [None] * len(search_response.results)
        full_contents = [None] * len(search_response.results)

        # Limit concurrent requests to prevent connection pool exhaustion
        max_concurrent = 20
        semaphore = anyio.Semaphore(max_concurrent)

        async def fetch_content(index: int, result: SemanticSearchResult):
            """Fetch full content for a single document (parallel with semaphore)."""
            async with semaphore:
                if result.doc_type == "note":
                    try:
                        note = await client.notes.get_note(result.id)
                        # Note is accessible, store result and full content
                        content = note.get("content", "")
                        accessible_results[index] = result
                        full_contents[index] = content
                        logger.debug(
                            f"Fetched full content for note {result.id} "
                            f"(length: {len(content)} chars)"
                        )
                    except Exception as e:
                        # Note might have been deleted or permissions changed
                        # Leave as None to filter out later
                        logger.debug(
                            f"Note {result.id} not accessible: {e}. "
                            f"Excluding from results."
                        )
                else:
                    # Non-note document types (future: calendar, deck, files)
                    # For now, keep them with excerpts
                    accessible_results[index] = result
                    # full_contents[index] remains None (will use excerpt)

        # Run all fetches in parallel using anyio task group
        async with anyio.create_task_group() as tg:
            for idx, result in enumerate(search_response.results):
                tg.start_soon(fetch_content, idx, result)

        # Filter out None (inaccessible notes) while preserving order
        final_pairs = [
            (r, c) for r, c in zip(accessible_results, full_contents) if r is not None
        ]
        accessible_results = [r for r, c in final_pairs]
        full_contents = [c for r, c in final_pairs]

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
        # Note: 5 minute timeout to accommodate slower local LLMs (e.g., Ollama)
        sampling_timeout_seconds = 300

        try:
            with anyio.fail_after(sampling_timeout_seconds):
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
                f"Sampling request timed out after {sampling_timeout_seconds} seconds for query: '{query}', "
                f"returning search results only"
            )
            return SamplingSearchResponse(
                query=query,
                generated_answer=(
                    f"[Sampling request timed out]\n\n"
                    f"The answer generation took too long (>{sampling_timeout_seconds}s). "
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

    @mcp.tool(
        title="Check Indexing Status",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Only checks status
            openWorldHint=True,
        ),
    )
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
                from qdrant_client.models import Filter

                from nextcloud_mcp_server.config import get_settings
                from nextcloud_mcp_server.vector.placeholder import (
                    get_placeholder_filter,
                )
                from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

                settings = get_settings()
                qdrant_client = await get_qdrant_client()

                # Count documents in collection, excluding placeholders
                # Placeholders are zero-vector points used to track processing state
                count_result = await qdrant_client.count(
                    collection_name=settings.get_collection_name(),
                    count_filter=Filter(must=[get_placeholder_filter()]),
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
