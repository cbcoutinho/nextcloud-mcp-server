"""Semantic search MCP tools using vector database."""

import logging

from httpx import HTTPStatusError, RequestError
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

logger = logging.getLogger(__name__)


def configure_semantic_tools(mcp: FastMCP):
    """Configure semantic search tools for MCP server."""

    @mcp.tool()
    @require_scopes("semantic:read")
    async def nc_semantic_search(
        query: str, ctx: Context, limit: int = 10, score_threshold: float = 0.7
    ) -> SemanticSearchResponse:
        """
        Semantic search across all indexed Nextcloud apps using vector embeddings.

        Searches documents by meaning rather than exact keywords across notes, calendar
        events, deck cards, files, and contacts. Requires vector database synchronization
        to be enabled (VECTOR_SYNC_ENABLED=true).

        Args:
            query: Natural language search query
            limit: Maximum number of results to return (default: 10)
            score_threshold: Minimum similarity score (0-1, default: 0.7)

        Returns:
            SemanticSearchResponse with matching documents and similarity scores
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from nextcloud_mcp_server.config import get_settings
        from nextcloud_mcp_server.embedding import get_embedding_service
        from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

        settings = get_settings()

        # Check if vector sync is enabled
        if not settings.vector_sync_enabled:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="Semantic search is not enabled. Set VECTOR_SYNC_ENABLED=true and ensure vector database is configured.",
                )
            )

        client = await get_client(ctx)
        username = client.username

        logger.info(
            f"Semantic search: query='{query}', user={username}, "
            f"limit={limit}, score_threshold={score_threshold}"
        )

        try:
            # Generate embedding for query
            embedding_service = get_embedding_service()
            query_embedding = await embedding_service.embed(query)
            logger.debug(
                f"Generated embedding for query (dimension={len(query_embedding)})"
            )

            # Search Qdrant with user filtering
            # Note: Currently only searching notes (doc_type="note")
            # Future: Remove doc_type filter to search all apps
            qdrant_client = await get_qdrant_client()
            search_response = await qdrant_client.query_points(
                collection_name=settings.get_collection_name(),
                query=query_embedding,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="user_id",
                            match=MatchValue(value=username),
                        ),
                        FieldCondition(
                            key="doc_type",
                            match=MatchValue(value="note"),
                        ),
                    ]
                ),
                limit=limit * 2,  # Get extra for filtering
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,  # Don't return vectors to save bandwidth
            )

            logger.info(
                f"Qdrant returned {len(search_response.points)} results "
                f"(before deduplication and access verification)"
            )
            if search_response.points:
                # Log top 3 scores to help with threshold tuning
                top_scores = [p.score for p in search_response.points[:3]]
                logger.debug(f"Top 3 similarity scores: {top_scores}")

            # Deduplicate by document ID (multiple chunks per document)
            seen_doc_ids = set()
            results = []

            for result in search_response.points:
                doc_id = int(result.payload["doc_id"])
                doc_type = result.payload.get("doc_type", "note")

                # Skip if we've already seen this document
                if doc_id in seen_doc_ids:
                    continue

                seen_doc_ids.add(doc_id)

                # Verify access via Nextcloud API (dual-phase authorization)
                # Currently only supports notes, will be extended to other apps
                if doc_type == "note":
                    try:
                        note = await client.notes.get_note(doc_id)

                        results.append(
                            SemanticSearchResult(
                                id=doc_id,
                                doc_type="note",
                                title=result.payload["title"],
                                category=note.get("category", ""),
                                excerpt=result.payload["excerpt"],
                                score=result.score,
                                chunk_index=result.payload["chunk_index"],
                                total_chunks=result.payload["total_chunks"],
                            )
                        )

                        if len(results) >= limit:
                            break

                    except HTTPStatusError as e:
                        if e.response.status_code == 403:
                            # User lost access, skip this document
                            logger.debug(f"Skipping note {doc_id}: access denied (403)")
                            continue
                        elif e.response.status_code == 404:
                            # Document was deleted but not yet removed from vector DB
                            logger.debug(
                                f"Skipping note {doc_id}: not found (404), "
                                f"likely deleted after indexing"
                            )
                            continue
                        else:
                            # Log other errors but continue processing
                            logger.warning(
                                f"Error verifying access to note {doc_id}: {e.response.status_code}"
                            )
                            continue

            logger.info(
                f"Returning {len(results)} results after deduplication and access verification"
            )
            if results:
                result_details = [
                    f"note_{r.id} (score={r.score:.3f}, title='{r.title}')"
                    for r in results[:5]  # Show top 5
                ]
                logger.debug(f"Top results: {', '.join(result_details)}")

            return SemanticSearchResponse(
                results=results,
                query=query,
                total_found=len(results),
                search_method="semantic",
            )

        except ValueError as e:
            if "No embedding provider configured" in str(e):
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Embedding service not configured. Set OLLAMA_BASE_URL environment variable.",
                    )
                )
            raise McpError(ErrorData(code=-1, message=f"Configuration error: {str(e)}"))
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error during search: {str(e)}")
            )
        except Exception as e:
            logger.error(f"Semantic search error: {e}", exc_info=True)
            raise McpError(
                ErrorData(code=-1, message=f"Semantic search failed: {str(e)}")
            )

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

        # 4. Construct context from retrieved documents
        context_parts = []
        for idx, result in enumerate(search_response.results, 1):
            context_parts.append(
                f"[Document {idx}]\n"
                f"Type: {result.doc_type}\n"
                f"Title: {result.title}\n"
                f"Category: {result.category}\n"
                f"Excerpt: {result.excerpt}\n"
                f"Relevance Score: {result.score:.2f}\n"
            )

        context = "\n".join(context_parts)

        # 5. Construct prompt - reuse user's query, add context and instructions
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
                sources=search_response.results,
                total_found=search_response.total_found,
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
                    f"Found {search_response.total_found} relevant documents. "
                    f"Please review the sources below or try a simpler query."
                ),
                sources=search_response.results,
                total_found=search_response.total_found,
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
                    f"Found {search_response.total_found} relevant documents. "
                    f"Please review the sources below."
                ),
                sources=search_response.results,
                total_found=search_response.total_found,
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
                    f"Found {search_response.total_found} relevant documents. "
                    f"Please review the sources below."
                ),
                sources=search_response.results,
                total_found=search_response.total_found,
                search_method="semantic_sampling_error",
                success=True,
            )

    @mcp.tool()
    @require_scopes("semantic:read")
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
