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
from nextcloud_mcp_server.models.notes import (
    AppendContentResponse,
    CreateNoteResponse,
    DeleteNoteResponse,
    Note,
    NoteSearchResult,
    NotesSettings,
    SamplingSearchResponse,
    SearchNotesResponse,
    SemanticSearchNotesResponse,
    SemanticSearchResult,
    UpdateNoteResponse,
)

logger = logging.getLogger(__name__)


def configure_notes_tools(mcp: FastMCP):
    @mcp.resource("notes://settings")
    async def notes_get_settings():
        """Get the Notes App settings"""
        ctx: Context = (
            mcp.get_context()
        )  # https://github.com/modelcontextprotocol/python-sdk/issues/244
        client = await get_client(ctx)
        settings_data = await client.notes.get_settings()
        return NotesSettings(**settings_data)

    @mcp.resource("nc://Notes/{note_id}/attachments/{attachment_filename}")
    async def nc_notes_get_attachment_resource(note_id: int, attachment_filename: str):
        """Get a specific attachment from a note"""
        ctx: Context = mcp.get_context()
        client = await get_client(ctx)
        # Assuming a method get_note_attachment exists in the client
        # This method should return the raw content and determine the mime type
        content, mime_type = await client.webdav.get_note_attachment(
            note_id=note_id, filename=attachment_filename
        )
        return {
            "contents": [
                {
                    # Use uppercase 'Notes' to match the decorator
                    "uri": f"nc://Notes/{note_id}/attachments/{attachment_filename}",
                    "mimeType": mime_type,  # Client needs to determine this
                    "data": content,  # Return raw bytes/data
                }
            ]
        }

    @mcp.resource("nc://Notes/{note_id}")
    async def nc_get_note_resource(note_id: int):
        """Get user note using note id"""

        ctx: Context = mcp.get_context()
        client = await get_client(ctx)
        try:
            note_data = await client.notes.get_note(note_id)
            return Note(**note_data)
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error retrieving note {note_id}: {str(e)}",
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Note {note_id} not found"))
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(code=-1, message=f"Access denied to note {note_id}")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to retrieve note {note_id}: {e.response.reason_phrase}",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:write")
    async def nc_notes_create_note(
        title: str, content: str, category: str, ctx: Context
    ) -> CreateNoteResponse:
        """Create a new note (requires notes:write scope)"""
        client = await get_client(ctx)
        try:
            note_data = await client.notes.create_note(
                title=title,
                content=content,
                category=category,
            )
            note = Note(**note_data)
            return CreateNoteResponse(
                id=note.id, title=note.title, category=note.category, etag=note.etag
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error creating note: {str(e)}")
            )
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to create notes",
                    )
                )
            elif e.response.status_code == 413:
                raise McpError(ErrorData(code=-1, message="Note content too large"))
            elif e.response.status_code == 409:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"A note with title '{title}' already exists in this category",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to create note: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:write")
    async def nc_notes_update_note(
        note_id: int,
        etag: str,
        title: str | None,
        content: str | None,
        category: str | None,
        ctx: Context,
    ) -> UpdateNoteResponse:
        """Update an existing note's title, content, or category (requires notes:write scope).

        REQUIRED: etag parameter must be provided to prevent overwriting concurrent changes.
        Get the current ETag by first retrieving the note using nc_notes_get_note tool.
        If the note has been modified by someone else since you retrieved it,
        the update will fail with a 412 error."""
        logger.info("Updating note %s", note_id)
        client = await get_client(ctx)
        try:
            note_data = await client.notes.update(
                note_id=note_id,
                etag=etag,
                title=title,
                content=content,
                category=category,
            )
            note = Note(**note_data)
            return UpdateNoteResponse(
                id=note.id, title=note.title, category=note.category, etag=note.etag
            )
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error updating note {note_id}: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Note {note_id} not found"))
            elif e.response.status_code == 412:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Note {note_id} has been modified by someone else. Please refresh and try again.",
                    )
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied: insufficient permissions to update note {note_id}",
                    )
                )
            elif e.response.status_code == 413:
                raise McpError(
                    ErrorData(code=-1, message="Updated note content is too large")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to update note {note_id}: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:write")
    async def nc_notes_append_content(
        note_id: int, content: str, ctx: Context
    ) -> AppendContentResponse:
        """Append content to an existing note. The tool adds a `\n---\n`
        between the note and what will be appended."""

        logger.info("Appending content to note %s", note_id)
        client = await get_client(ctx)
        try:
            note_data = await client.notes.append_content(
                note_id=note_id, content=content
            )
            note = Note(**note_data)
            return AppendContentResponse(
                id=note.id, title=note.title, category=note.category, etag=note.etag
            )
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error appending to note {note_id}: {str(e)}",
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Note {note_id} not found"))
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied: insufficient permissions to modify note {note_id}",
                    )
                )
            elif e.response.status_code == 413:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Content to append would make the note too large",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to append content to note {note_id}: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:read")
    async def nc_notes_search_notes(query: str, ctx: Context) -> SearchNotesResponse:
        """Search notes by title or content, returning only id, title, and category (requires notes:read scope)."""
        client = await get_client(ctx)
        try:
            search_results_raw = await client.notes_search_notes(query=query)

            # Convert to NoteSearchResult models, including the _score field
            results = [
                NoteSearchResult(
                    id=result["id"],
                    title=result["title"],
                    category=result["category"],
                    score=result.get("_score"),  # Include search score if available
                )
                for result in search_results_raw
            ]

            return SearchNotesResponse(
                results=results, query=query, total_found=len(results)
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error searching notes: {str(e)}")
            )
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to search notes",
                    )
                )
            elif e.response.status_code == 400:
                raise McpError(
                    ErrorData(code=-1, message="Invalid search query format")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Search failed: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:read")
    async def nc_notes_get_note(note_id: int, ctx: Context) -> Note:
        """Get a specific note by its ID (requires notes:read scope)"""
        client = await get_client(ctx)
        try:
            note_data = await client.notes.get_note(note_id)
            return Note(**note_data)
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error getting note {note_id}: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Note {note_id} not found"))
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(code=-1, message=f"Access denied to note {note_id}")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to retrieve note {note_id}: {e.response.reason_phrase}",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:read")
    async def nc_notes_get_attachment(
        note_id: int, attachment_filename: str, ctx: Context
    ) -> dict[str, str]:
        """Get a specific attachment from a note"""
        client = await get_client(ctx)
        try:
            content, mime_type = await client.webdav.get_note_attachment(
                note_id=note_id, filename=attachment_filename
            )
            return {  # type: ignore
                "uri": f"nc://Notes/{note_id}/attachments/{attachment_filename}",
                "mimeType": mime_type,
                "data": content,
            }
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error getting attachment {attachment_filename} for note {note_id}: {str(e)}",
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Attachment {attachment_filename} not found for note {note_id}",
                    )
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied to attachment {attachment_filename} for note {note_id}",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to retrieve attachment: {e.response.reason_phrase}",
                    )
                )

    @mcp.tool()
    @require_scopes("notes:read")
    async def nc_notes_semantic_search(
        query: str, ctx: Context, limit: int = 10, score_threshold: float = 0.7
    ) -> SemanticSearchNotesResponse:
        """
        Semantic search for notes using vector embeddings.

        Searches notes by meaning rather than exact keywords. Requires vector
        database synchronization to be enabled (VECTOR_SYNC_ENABLED=true).

        Args:
            query: Natural language search query
            limit: Maximum number of results to return (default: 10)
            score_threshold: Minimum similarity score (0-1, default: 0.7)

        Returns:
            SemanticSearchNotesResponse with matching notes and similarity scores
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

        try:
            # Generate embedding for query
            embedding_service = get_embedding_service()
            query_embedding = await embedding_service.embed(query)

            # Search Qdrant with user filtering
            qdrant_client = await get_qdrant_client()
            search_response = await qdrant_client.query_points(
                collection_name=settings.qdrant_collection,
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

            # Deduplicate by note ID (multiple chunks per note)
            seen_note_ids = set()
            results = []

            for result in search_response.points:
                note_id = int(result.payload["doc_id"])

                # Skip if we've already seen this note
                if note_id in seen_note_ids:
                    continue

                seen_note_ids.add(note_id)

                # Verify access via Nextcloud API (dual-phase authorization)
                try:
                    note = await client.notes.get_note(note_id)

                    results.append(
                        SemanticSearchResult(
                            id=note_id,
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
                        # User lost access, skip this note
                        continue
                    elif e.response.status_code == 404:
                        # Note was deleted but not yet removed from vector DB
                        continue
                    else:
                        # Log other errors but continue processing
                        logger.warning(
                            f"Error verifying access to note {note_id}: {e.response.status_code}"
                        )
                        continue

            return SemanticSearchNotesResponse(
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
    @require_scopes("notes:read")
    async def nc_notes_semantic_search_answer(
        query: str,
        ctx: Context,
        limit: int = 5,
        score_threshold: float = 0.7,
        max_answer_tokens: int = 500,
    ) -> SamplingSearchResponse:
        """
        Semantic search with LLM-generated answer using MCP sampling.

        Retrieves relevant documents from Nextcloud Notes using vector similarity
        search, then uses MCP sampling to request the client's LLM to generate
        a natural language answer based on the retrieved context.

        This tool combines the power of semantic search (finding relevant content)
        with LLM generation (synthesizing that content into coherent answers). The
        generated answer includes citations to specific documents, allowing users
        to verify claims and explore sources.

        The LLM generation happens client-side via MCP sampling. The MCP client
        controls which model is used, who pays for it, and whether to prompt the
        user for approval. This keeps the server simple (no LLM API keys needed)
        while giving users full control over their LLM interactions.

        Args:
            query: Natural language question to answer (e.g., "What are my project goals?")
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
            >>> # Query about project goals
            >>> result = await nc_notes_semantic_search_answer(
            ...     query="What are my Q1 2025 project goals?",
            ...     ctx=ctx
            ... )
            >>> print(result.generated_answer)
            "Based on Document 1 (Project Kickoff) and Document 3 (Q1 Planning),
            your main goals are: 1) Improve semantic search accuracy by 20%,
            2) Deploy new embedding model, 3) Reduce indexing latency..."

            >>> # Query about learning
            >>> result = await nc_notes_semantic_search_answer(
            ...     query="What did I learn about Python async/await last month?",
            ...     ctx=ctx,
            ...     limit=10
            ... )
            >>> len(result.sources)  # Up to 10 documents
            7
        """
        # 1. Retrieve relevant documents via existing semantic search
        search_response = await nc_notes_semantic_search(
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
                generated_answer="No relevant documents found in your Nextcloud Notes for this query.",
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
            f"Here are relevant documents from Nextcloud Notes:\n\n"
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
                generated_answer = f"Received non-text response of type: {sampling_result.content.type}"
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

    @mcp.tool()
    @require_scopes("notes:write")
    async def nc_notes_delete_note(note_id: int, ctx: Context) -> DeleteNoteResponse:
        """Delete a note permanently"""
        logger.info("Deleting note %s", note_id)
        client = await get_client(ctx)
        try:
            await client.notes.delete_note(note_id)
            return DeleteNoteResponse(
                status_code=200,
                message=f"Note {note_id} deleted successfully",
                deleted_id=note_id,
            )
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error deleting note {note_id}: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Note {note_id} not found"))
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied: insufficient permissions to delete note {note_id}",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to delete note {note_id}: server error ({e.response.status_code})",
                    )
                )
