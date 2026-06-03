"""Semantic search MCP tools using vector database."""

import logging
from typing import Annotated

import anyio
from httpx import RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import (
    ClientCapabilities,
    ErrorData,
    ModelHint,
    ModelPreferences,
    SamplingCapability,
    SamplingMessage,
    TextContent,
    ToolAnnotations,
)
from pydantic import Field
from qdrant_client.models import Filter

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
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
from nextcloud_mcp_server.search.access_filter import (
    MAX_PATH_PREFIXES,
    list_accessible_owners,
    normalize_path_prefixes,
)
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.search.context import get_chunk_with_context
from nextcloud_mcp_server.search.verification import verify_search_results
from nextcloud_mcp_server.utils.validation import parse_modified_timestamp
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

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
    @require_scopes("semantic.read")
    @instrument_tool
    async def nc_semantic_search(
        query: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        doc_types: list[str] | None = None,
        score_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0,
        fusion: str = "rrf",
        include_context: bool = False,
        context_chars: Annotated[int, Field(ge=0)] = 300,
        modified_after: Annotated[
            str | int | None,
            Field(
                description=(
                    "Only return documents modified at or after this time. "
                    "RFC 3339 / ISO 8601 datetime (e.g. '2026-01-01T00:00:00Z') "
                    "or Unix seconds. None = no lower bound."
                ),
            ),
        ] = None,
        modified_before: Annotated[
            str | int | None,
            Field(
                description=(
                    "Only return documents modified at or before this time. "
                    "RFC 3339 / ISO 8601 datetime or Unix seconds. "
                    "None = no upper bound."
                ),
            ),
        ] = None,
        path_prefix: Annotated[
            str | None,
            Field(
                description=(
                    "Deprecated single-folder filter; prefer path_prefixes. "
                    "Restrict to files under this folder/path "
                    "(e.g. '/Projects/Reports'). Matches the file_path of "
                    "indexed files only, so setting it implicitly limits "
                    "results to files. None = no path filter."
                ),
            ),
        ] = None,
        path_prefixes: Annotated[
            list[str] | None,
            Field(
                max_length=MAX_PATH_PREFIXES,
                description=(
                    "Restrict to files under any of these folders/paths "
                    "(e.g. ['/Projects/Reports', '/Shared/Specs']). Folders are "
                    "OR-ed together. Matches the file_path of indexed files "
                    "only, so setting it implicitly limits results to files. "
                    f"Capped at {MAX_PATH_PREFIXES} folders to bound the "
                    "OR-filter width. None or empty = no path filter."
                ),
            ),
        ] = None,
    ) -> SemanticSearchResponse:
        """
        Search Nextcloud content using BM25 hybrid search with cross-app support.

        Uses Qdrant's native hybrid search combining:
        - Dense semantic vectors: For conceptual similarity and natural language queries
        - BM25 sparse vectors: For precise keyword matching, acronyms, and specific terms

        Results are automatically fused using the selected fusion algorithm in the
        database for optimal relevance. This provides the best of both semantic
        understanding and keyword precision.

        Requires VECTOR_SYNC_ENABLED=true. Supports indexing of notes, files,
        news items, and deck cards.

        Args:
            query: Natural language or keyword search query
            limit: Maximum number of results to return (default: 10)
            doc_types: Document types to search (e.g., ["note", "file", "deck_card", "news_item"]). None = search all indexed types (default)
            score_threshold: Minimum fusion score (0-1, default: 0.0)
            fusion: Fusion algorithm: "rrf" (Reciprocal Rank Fusion, default) or "dbsf" (Distribution-Based Score Fusion)
                   RRF: Good general-purpose fusion using reciprocal ranks
                   DBSF: Uses distribution-based normalization, may better balance different score ranges
            include_context: Whether to expand results with surrounding context (default: False)
            context_chars: Number of characters to include before/after matched chunk (default: 300)
            modified_after: Only return documents whose last-modified time is at or after this
                instant. Accepts an RFC 3339 / ISO 8601 datetime (e.g. "2026-01-01T00:00:00Z";
                a naive datetime is treated as UTC) or Unix seconds. None = no lower bound
                (default).
            modified_before: Only return documents whose last-modified time is at or before this
                instant. Same formats as modified_after. None = no upper bound (default). Must be
                >= modified_after when both are supplied.
            path_prefix: Deprecated single-folder filter; prefer path_prefixes. Restrict to files
                under this folder/path (e.g. "/Projects/Reports"). Folded into path_prefixes.
            path_prefixes: Restrict to files under any of these folders/paths (OR-ed), e.g.
                ["/Projects/Reports", "/Shared/Specs"]. Matches the file_path of indexed files
                only — setting it implicitly limits results to files. None/empty = no path filter
                (default).

        Returns:
            SemanticSearchResponse with matching documents ranked by fusion scores.

            Verification fields (ADR-019 verify-on-read):
            - verified_chunk_count: chunk rows that passed access checks
              (sized in chunks; counted before trimming to ``limit``, so it
              can exceed ``len(results)`` when a doc has multiple matching
              chunks).
            - dropped_document_count: unique ``(doc_id, doc_type)`` pairs
              evicted as ghost records during this search (sized in
              documents, not chunks).
        """
        settings = get_settings()
        client = await get_client(ctx)
        username = client.username

        logger.info(
            "BM25 hybrid search: query=%r, user=%s, "
            "limit=%d, score_threshold=%s, fusion=%s",
            query,
            username,
            limit,
            score_threshold,
            fusion,
        )

        # Check that vector sync is enabled
        if not settings.vector_sync_enabled:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="BM25 hybrid search requires VECTOR_SYNC_ENABLED=true",
                )
            )

        # Normalize the RFC 3339 / Unix-seconds date bounds to int Unix seconds
        # for the numeric ``modified_at`` Range filter (ADR-027). A bad format
        # surfaces as a clean McpError rather than a 500.
        try:
            modified_after_ts = parse_modified_timestamp(
                modified_after, param_name="modified_after"
            )
            modified_before_ts = parse_modified_timestamp(
                modified_before, param_name="modified_before"
            )
        except ValueError as exc:
            raise McpError(ErrorData(code=-1, message=str(exc))) from exc

        # Cross-field invariant: a per-parameter pydantic ``Field`` constraint
        # (validated by FastMCP from the signature) bounds each date on its own
        # but cannot express the relationship between them. Guard it here so an
        # inverted range surfaces a clean McpError rather than silently
        # returning zero results (ADR-027).
        if (
            modified_after_ts is not None
            and modified_before_ts is not None
            and modified_after_ts > modified_before_ts
        ):
            raise McpError(
                ErrorData(
                    code=-1,
                    message=(
                        "modified_after must be <= modified_before "
                        f"(got modified_after={modified_after!r}, "
                        f"modified_before={modified_before!r})"
                    ),
                )
            )

        # Merge the legacy single path_prefix and the path_prefixes list into one
        # cleaned list, dropping blank/whitespace entries so an empty UI field
        # doesn't filter out every result (ADR-027 Phase 2).
        folder_prefixes = normalize_path_prefixes(path_prefix, path_prefixes)

        # Expand the caller's identity to every owner whose content they
        # have read access to via Nextcloud shares. Lets a user find files
        # owners have shared with them without having to re-index those
        # files under their own user_id.
        accessible_owners = await list_accessible_owners(client.sharing, username)

        try:
            # The nc_semantic_search tool deliberately uses BM25-hybrid (dense +
            # sparse with RRF/DBSF fusion) as the single tool-layer algorithm.
            # SemanticSearchAlgorithm is not dead code — it backs the dense-only
            # option that the visualization/API surfaces expose explicitly
            # (auth/viz_routes.py and api/visualization.py). Both algorithms take
            # accessible_owners, so ACL-aware search works on every surface.
            search_algo = BM25HybridSearchAlgorithm(
                score_threshold=score_threshold, fusion=fusion
            )

            # Execute search across requested document types
            # If doc_types is None, search all indexed types (cross-app search)
            # If doc_types is a list, search only those types
            all_results = []

            if doc_types is None:
                # Cross-app search: search all indexed types
                # Get unverified results from Qdrant.
                #
                # NOTE (ADR-019): Over-fetch by 2× to absorb ghost-record drops
                # during verify-on-read. When ghost density is high (e.g. a
                # large board share was just revoked) this budget can still
                # under-deliver against the requested ``limit``; the index
                # self-heals via lazy eviction so subsequent searches recover.
                # The 2× factor is a deliberate v1 trade-off — raising it
                # costs Nextcloud round-trips on every search. Trim to
                # ``limit`` happens AFTER verification.
                # TODO(ADR-019): expose VERIFICATION_OVERFETCH so operators
                # with persistent high ghost density can tune this without a
                # code change.
                unverified_results = await search_algo.search(
                    query=query,
                    user_id=username,
                    limit=limit * 2,
                    doc_type=None,  # Signal to search all types
                    score_threshold=score_threshold,
                    accessible_owners=accessible_owners,
                    modified_after=modified_after_ts,
                    modified_before=modified_before_ts,
                    path_prefixes=folder_prefixes,
                )
                all_results.extend(unverified_results)
            else:
                # Search specific document types.
                #
                # Per-Qdrant-query cost: this branch issues ONE query per
                # requested doc_type, each capped at `limit * 2`. With N
                # types in `doc_types`, the pre-merge result pool is
                # therefore N × `limit * 2`, NOT `limit * 2`. That is more
                # Qdrant work than the cross-app branch above (which makes a
                # single multi-type query returning `limit * 2` total).
                #
                # The post-merge trim below clamps the pool back down to
                # `limit * 2` so verification (and the Nextcloud round-trips
                # it triggers) sees the same budget as the cross-app branch.
                # The per-type Qdrant cost remains higher; pre-trim cost
                # scales linearly with len(doc_types).
                for dtype in doc_types:
                    unverified_results = await search_algo.search(
                        query=query,
                        user_id=username,
                        limit=limit * 2,
                        doc_type=dtype,
                        score_threshold=score_threshold,
                        accessible_owners=accessible_owners,
                        modified_after=modified_after_ts,
                        modified_before=modified_before_ts,
                        path_prefixes=folder_prefixes,
                    )
                    all_results.extend(unverified_results)

                # Sort combined results by score, then cap to `limit * 2` to
                # match the cross-app branch's over-fetch budget. Without this
                # cap, N requested doc_types × `limit * 2` results would all
                # flow into verification, multiplying the Nextcloud round-trip
                # cost by N.
                all_results.sort(key=lambda r: r.score, reverse=True)
                all_results = all_results[: limit * 2]

            # ADR-019: Verify-on-read. The vector index is a recall layer;
            # Nextcloud is the source of truth for access. Filter out ghost
            # records (deleted/unshared docs not yet reconciled by webhooks)
            # BEFORE trimming to `limit`, so we don't lose accessible results
            # to the limit slot that ghosts would otherwise occupy. We also
            # run this BEFORE context expansion to avoid re-fetching docs that
            # are about to be dropped. Pass the lifespan-owned task group so
            # eviction of dropped points is fire-and-forget (does not block
            # the response).
            # Direct attribute access — both AppContext and OAuthAppContext
            # expose ``eviction_task_group`` as a @property (see app.py),
            # reading dynamically from the module-level VectorSyncState
            # singleton. A defensive ``getattr(..., None)`` here would mask
            # typos; if a future lifespan-context type forgets the property,
            # AttributeError surfaces during the first search rather than
            # silently degrading to inline eviction for the life of the
            # process.
            eviction_task_group = (
                ctx.request_context.lifespan_context.eviction_task_group
            )
            verification_start = anyio.current_time()
            verified_results, dropped_count = await verify_search_results(
                client,
                all_results,
                eviction_task_group=eviction_task_group,
            )
            verified_chunk_count = len(verified_results)
            logger.debug(
                "Verification completed in %.2fs: kept %d chunk(s), dropped %d doc(s)",
                anyio.current_time() - verification_start,
                verified_chunk_count,
                dropped_count,
            )
            # Safe to log titles now: these results passed verify-on-read, so the
            # caller is confirmed to have access (unverified titles were never
            # logged — see the search algorithms).
            if verified_results:
                logger.debug(
                    "Top verified results: %s",
                    ", ".join(
                        f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                        for r in verified_results[:5]
                    ),
                )
            search_results = verified_results[:limit]

            # Convert SearchResult objects to SemanticSearchResult for response.
            # SearchResult.id is `str` (Qdrant keyword-indexed payload), but
            # every currently indexed type uses numeric ids and the MCP response
            # model narrows to `int`. Casting here makes the narrowing explicit
            # and surfaces any future non-numeric-id type as a loud failure at
            # the boundary instead of silently widening the public API.
            results = []
            for r in search_results:
                try:
                    narrowed_id = int(r.id)
                except (TypeError, ValueError) as e:
                    # Re-raise with explicit context so the outer handler logs
                    # something operators can act on (the generic "Search
                    # failed: invalid literal for int()" is opaque).
                    raise TypeError(
                        f"SemanticSearchResult.id must be int-convertible, "
                        f"got {r.id!r} (type={type(r.id).__name__}) for "
                        f"doc_type={r.doc_type!r}. This indicates a doc_type "
                        f"with non-numeric ids has been indexed but the "
                        f"public response model has not been widened. Add "
                        f"the doc_type to the SemanticSearchResult.id type "
                        f"or convert at the verifier layer."
                    ) from e
                results.append(
                    SemanticSearchResult(
                        id=narrowed_id,
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
                    "Expanding %d results with context (context_chars=%d)",
                    len(results),
                    context_chars,
                )

                # Fetch context for all results in parallel.
                # Limit concurrent requests to prevent connection pool exhaustion.
                #
                # Intentionally distinct from settings.verification_concurrency:
                # that knob bounds Nextcloud round-trips during access
                # verification (ADR-019); this one bounds context-expansion
                # fetches that run only when ``include_context=True``. Operators
                # tuning one rarely want the other in lockstep, so they share
                # the default value (20) but not the env var.
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
                                # SemanticSearchResult.id is the int-narrowed
                                # public form; get_chunk_with_context queries
                                # Qdrant where doc_id is keyword-indexed as str.
                                doc_id=str(result.id),
                                doc_type=result.doc_type,
                                chunk_start=result.chunk_start_offset,
                                chunk_end=result.chunk_end_offset,
                                page_number=result.page_number,
                                chunk_index=result.chunk_index,
                                total_chunks=result.total_chunks,
                                context_chars=context_chars,
                                # Forward the share-expanded owner set so context
                                # expansion works for shared files (the per-file
                                # file_accessible_by_id gate inside still enforces
                                # access). Without this the lookup stays self-only
                                # and silently falls back to the plain excerpt.
                                accessible_owners=accessible_owners,
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
                                    "Expanded context for %s %s",
                                    result.doc_type,
                                    result.id,
                                )
                            else:
                                # Context expansion failed, keep original result
                                expanded_results[index] = result
                                logger.debug(
                                    "Failed to expand context for %s %s, "
                                    "keeping original result",
                                    result.doc_type,
                                    result.id,
                                )
                        except Exception as e:
                            # Context expansion failed, keep original result
                            expanded_results[index] = result
                            logger.warning(
                                "Error expanding context for %s %s: %s",
                                result.doc_type,
                                result.id,
                                e,
                            )

                # Run all context fetches in parallel using anyio task group
                async with anyio.create_task_group() as tg:
                    for idx, result in enumerate(results):
                        tg.start_soon(fetch_context, idx, result)

                # Replace results with expanded versions
                results = [r for r in expanded_results if r is not None]
                logger.info(
                    "Context expansion completed: %d results with context",
                    len(results),
                )

            logger.info("Returning %d results from BM25 hybrid search", len(results))

            return SemanticSearchResponse(
                results=results,
                query=query,
                total_found=len(results),
                search_method=f"bm25_hybrid_{fusion}",
                verified_chunk_count=verified_chunk_count,
                dropped_document_count=dropped_count,
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
            logger.error("Search error: %s", e, exc_info=True)
            raise McpError(ErrorData(code=-1, message=f"Search failed: {str(e)}"))

    @mcp.tool(
        title="Search with AI-Generated Answer",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Search doesn't modify data
            openWorldHint=True,  # Calls into Nextcloud via nc_semantic_search
        ),
    )
    @require_scopes("semantic.read")
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

        Latency profile: For each note in the result page, this tool fetches
        the full note body via ``client.notes.get_note`` after upstream
        verify-on-read has already round-tripped to the same endpoint as a
        race guard (ADR-019). Expect one additional Nextcloud round-trip per
        note result; raising ``limit`` above the default of 5 amplifies this
        cost roughly linearly. File / news / deck results do not pay this
        cost — they reuse the verified excerpt.
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
            logger.debug("No documents found for query: %r", query)
            return SamplingSearchResponse(
                query=query,
                generated_answer="No relevant documents found in your Nextcloud content for this query.",
                sources=[],
                total_found=0,
                search_method="semantic_sampling",
                success=True,
            )

        # 3. Check if client supports sampling
        client_has_sampling = ctx.session.check_client_capability(
            ClientCapabilities(sampling=SamplingCapability())
        )

        # Log capability check result for debugging
        logger.info(
            "Sampling capability check: client_has_sampling=%s, query=%r",
            client_has_sampling,
            query,
        )
        if hasattr(ctx.session, "_client_params") and ctx.session._client_params:
            client_caps = ctx.session._client_params.capabilities
            logger.debug(
                "Client advertised capabilities: "
                "roots=%s, sampling=%s, experimental=%s",
                client_caps.roots is not None,
                client_caps.sampling is not None,
                client_caps.experimental is not None,
            )

        if not client_has_sampling:
            logger.info(
                "Client does not support sampling (query: %r), returning %d documents",
                query,
                len(search_response.results),
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

        # 4. Fetch full content for notes in parallel.
        # Access verification has already happened upstream in
        # nc_semantic_search via verify_search_results (ADR-019), so any
        # exception here is a sub-second race (doc deleted between
        # verification and this fetch) — drop the result in that case.
        client = await get_client(ctx)
        accessible_results = [None] * len(search_response.results)
        full_contents = [None] * len(search_response.results)

        # Limit concurrent requests to prevent connection pool exhaustion.
        #
        # Intentionally distinct from settings.verification_concurrency:
        # that knob bounds Nextcloud round-trips during access
        # verification (ADR-019). This one bounds the answer tool's
        # full-content fetch — a separate request phase tied to RAG
        # answer generation. Operators tuning one rarely want the other
        # in lockstep, so they share the default value (20) but not the
        # env var.
        max_concurrent = 20
        semaphore = anyio.Semaphore(max_concurrent)

        async def fetch_content(index: int, result: SemanticSearchResult):
            """Fetch full content for a single document (parallel with semaphore)."""
            async with semaphore:
                if result.doc_type == "note":
                    # SemanticSearchResult.id is typed `int` (Pydantic enforces
                    # at construction); no defensive cast is needed here. The
                    # catch-all below covers only the verify-then-delete race.
                    try:
                        note = await client.notes.get_note(result.id)
                        content = note.get("content", "")
                        accessible_results[index] = result
                        full_contents[index] = content
                        logger.debug(
                            "Fetched full content for note %s (length: %d chars)",
                            result.id,
                            len(content),
                        )
                    except Exception as e:
                        # Race window after verify_search_results — drop result.
                        logger.debug(
                            "Note %s disappeared between verification and "
                            "content fetch: %s. Excluding from results.",
                            result.id,
                            e,
                        )
                else:
                    # Non-note types (file, news_item, deck_card) keep the
                    # excerpt — already access-verified upstream.
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
            logger.warning(
                "All search results became inaccessible for query: %r", query
            )
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
            "Initiating sampling request: query_length=%d, documents=%d, "
            "prompt_length=%d, max_tokens=%d",
            len(query),
            len(search_response.results),
            len(prompt),
            max_answer_tokens,
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
                    "Unexpected content type from sampling: %s",
                    sampling_result.content.type,
                )

            logger.info(
                "Sampling successful: model=%s, stop_reason=%s, answer_length=%d",
                sampling_result.model,
                sampling_result.stopReason,
                len(generated_answer),
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
                "Sampling request timed out after %d seconds for query: %r, "
                "returning search results only",
                sampling_timeout_seconds,
                query,
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
                logger.info("User declined sampling request for query: %r", query)
                search_method = "semantic_sampling_user_declined"
                user_message = "User declined to generate an answer"
            elif "not supported" in error_msg.lower():
                # Client doesn't support sampling - also normal
                logger.info("Sampling not supported by client for query: %r", query)
                search_method = "semantic_sampling_unsupported"
                user_message = "Sampling not supported by this client"
            else:
                # Other MCP protocol errors
                logger.warning(
                    "MCP error during sampling for query %r: %s",
                    query,
                    error_msg,
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
                "Unexpected error during sampling for query %r: %s: %s",
                query,
                type(e).__name__,
                e,
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
    @require_scopes("semantic.read")
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

        # Check if vector sync is enabled (supports both old and new env var names)
        settings = get_settings()
        if not settings.vector_sync_enabled:
            return VectorSyncStatusResponse(
                indexed_count=0,
                pending_count=0,
                status="disabled",
                enabled=False,
            )

        try:
            # Get document receive stream from lifespan context. Direct
            # attribute access matches the eviction_task_group pattern at
            # ``nc_semantic_search`` (see comment there): both AppContext
            # and OAuthAppContext define ``document_receive_stream``, so a
            # missing attribute is a typo that should fail loudly. The
            # value itself can legitimately be ``None`` before sync starts,
            # which the check below handles.
            # Outstanding-work view depends on the queue backend (Deck #183):
            # memory → stream buffer depth; postgres → procrastinate job counts.
            # Direct attribute access matches the eviction_task_group pattern at
            # ``nc_semantic_search``: both AppContext and OAuthAppContext define
            # these, so a missing attribute is a typo that should fail loudly.
            from nextcloud_mcp_server.vector.ingest_status import (  # noqa: PLC0415
                get_ingest_pending,
            )

            lifespan_ctx = ctx.request_context.lifespan_context
            pending = await get_ingest_pending(
                task_producer=lifespan_ctx.task_producer,
                document_receive_stream=lifespan_ctx.document_receive_stream,
                ingest_queue=settings.ingest_queue,
            )

            # Get Qdrant client and query indexed count
            indexed_count = 0
            try:
                qdrant_client = await get_qdrant_client()

                # Count documents in collection, excluding placeholders
                # Placeholders are zero-vector points used to track processing state
                count_result = await qdrant_client.count(
                    collection_name=settings.get_collection_name(),
                    count_filter=Filter(must=[get_placeholder_filter()]),
                )
                indexed_count = count_result.count

            except Exception as e:
                logger.warning("Failed to query Qdrant for indexed count: %s", e)
                # Continue with indexed_count = 0

            # Determine status
            status = "syncing" if pending.pending > 0 else "idle"

            return VectorSyncStatusResponse(
                indexed_count=indexed_count,
                pending_count=pending.pending,
                status=status,
                enabled=True,
                ingest_queue=settings.ingest_queue,
                job_counts=pending.job_counts,
            )

        except Exception as e:
            logger.error("Error getting vector sync status: %s", e)
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to retrieve vector sync status: {str(e)}",
                )
            )
