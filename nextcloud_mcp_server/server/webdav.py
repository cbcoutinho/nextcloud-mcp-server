import base64
import contextlib
import logging

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models import DirectoryListing, FileInfo, SearchFilesResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)

logger = logging.getLogger(__name__)


def configure_webdav_tools(mcp: FastMCP):
    # WebDAV file system tools
    @mcp.tool(
        title="List Files and Directories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_directory(
        ctx: Context, path: str = ""
    ) -> DirectoryListing:
        """List files and directories in the specified NextCloud path.

        When ``EXCLUDED_TAGS`` is configured: raises ``ToolError`` if the
        listed path itself is tagged (or sits inside a tagged folder),
        and otherwise omits any tagged children from the listing. The
        early guard is consistent with the mutating tools and avoids a
        round-trip to Nextcloud for a known-excluded path.

        Args:
            path: Directory path to list (empty string for root directory)

        Returns:
            DirectoryListing with files, total_count, directories_count, files_count, and total_size
        """
        client = await get_client(ctx)

        # Resolve once and use for both the path-itself guard and the
        # children filter below.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

        items = await client.webdav.list_directory(path)

        # Filter out child files/folders carrying an excluded tag.
        if excluded:
            items = [
                i for i in items if not is_path_excluded(i.get("path", ""), excluded)
            ]

        # Convert to FileInfo models
        file_infos = [FileInfo(**item) for item in items]

        # Calculate metadata
        directories_count = sum(1 for f in file_infos if f.is_directory)
        files_count = sum(1 for f in file_infos if not f.is_directory)
        total_size = sum(f.size or 0 for f in file_infos if not f.is_directory)

        return DirectoryListing(
            path=path,
            files=file_infos,
            total_count=len(file_infos),
            directories_count=directories_count,
            files_count=files_count,
            total_size=total_size,
        )

    @mcp.tool(
        title="Read File",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_read_file(
        path: str, ctx: Context, force_processor: str | None = None
    ):
        """Read the content of a file from NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        file (or an ancestor folder) carries an excluded system tag.

        Args:
            path: Full path to the file to read
            force_processor: Force a specific document processor by name instead of
                auto-selecting. Set to ``"docling"`` to parse the file with a
                docling-serve instance even when it is a PDF that already has a text
                layer -- useful when the text layer misses tables/figures or is
                incomplete. Requires that processor to be enabled/registered;
                raises ``ToolError`` otherwise. ``None`` = auto-select.

        Returns:
            Dict with path, content, content_type, size, etag, and optional
            parsing metadata
            - Text files are decoded to UTF-8
            - Documents (PDF, DOCX, etc.) are parsed and text is extracted
            - Other binary files are base64 encoded
            - ``etag``: pass this back into ``nc_webdav_write_file``'s
              ``if_match`` when writing this same path later, so a manual
              edit made elsewhere in the meantime (e.g. in the Nextcloud web
              UI) is detected as a conflict instead of silently overwritten.
        """
        client = await get_client(ctx)

        # Block reads of paths carrying an excluded tag.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

        content, content_type, etag = await client.webdav.read_file(path)

        # Imported lazily so server startup never loads the document-parsing
        # stack (document_processors -> pymupdf -> _isolation). That stack is an
        # ingest-layer concern and, before this, broke Windows startup via a
        # Unix-only ``import resource`` (#877). It is only needed when a file is
        # actually read and parsed.
        from nextcloud_mcp_server.document_processors import (  # noqa: PLC0415
            get_registry,
        )
        from nextcloud_mcp_server.utils.document_parser import (  # noqa: PLC0415
            is_parseable_document,
            parse_document,
        )

        # force_processor is client/LLM-controlled: validate it against the
        # registered-processor allowlist (a dict-key lookup, never interpolated
        # into a URL/path) and surface a clear error with the available names
        # rather than the opaque base64 fallback parse_document would otherwise
        # return for an unknown/unconfigured processor.
        if force_processor is not None:
            registry = get_registry()
            if registry.get_processor(force_processor) is None:
                available = ", ".join(registry.list_processors()) or "none"
                raise ToolError(
                    f"Unknown document processor {force_processor!r}. Ensure document "
                    f"processing is enabled (ENABLE_DOCUMENT_PROCESSING) and the "
                    f"processor is configured (e.g. ENABLE_DOCLING + DOCLING_API_URL "
                    f"for 'docling'). Available: {available}"
                )

        # Parse when the type is auto-parseable OR the caller forced a processor.
        # is_parseable_document() also checks that document processing is enabled.
        if force_processor is not None or is_parseable_document(content_type):
            # Optional interactive cap (ADR-032): bound the SYNCHRONOUS parse so a
            # slow VLM/OCR convert returns base64 quickly instead of blocking past
            # the MCP client's own timeout. Read lazily so test/env overrides apply.
            # Disabled (None) -> nullcontext, i.e. unchanged behavior. Only wraps this
            # interactive tool; the async ingest/worker path is never bounded here.
            read_cap = get_settings().document_read_timeout_seconds
            cap_ctx = (
                anyio.fail_after(read_cap)
                if read_cap is not None
                else contextlib.nullcontext()
            )
            try:
                logger.info(
                    "Parsing document %r of type %r%s",
                    path,
                    content_type,
                    f" with forced processor {force_processor!r}"
                    if force_processor
                    else "",
                )
                with cap_ctx:
                    parsed_text, metadata = await parse_document(
                        content,
                        content_type,
                        filename=path,
                        progress_callback=ctx.report_progress,
                        processor_name=force_processor,
                    )
                return {
                    "path": path,
                    "content": parsed_text,
                    "content_type": content_type,
                    "size": len(content),
                    "parsed": True,
                    "parsing_metadata": metadata,
                    "etag": etag,
                }
            except TimeoutError as e:
                # Caught before the generic Exception (subclass-first). When the cap
                # is set this is our anyio.fail_after tripping; when it is None the
                # TimeoutError bubbled from a backend's own anyio timeout (e.g. the
                # Mistral OCR path) -- either way base64 is the right fallback.
                if read_cap is not None:
                    logger.warning(
                        "Parsing document %r exceeded the interactive read cap "
                        "(%ss), falling back to base64",
                        path,
                        read_cap,
                    )
                else:
                    logger.warning(
                        "Failed to parse document %r, falling back to base64: %s",
                        path,
                        e,
                    )
                # Fall through to base64 encoding on timeout
            except Exception as e:
                logger.warning(
                    "Failed to parse document %r, falling back to base64: %s",
                    path,
                    e,
                )
                # Fall through to base64 encoding on parse failure

        # For text files, decode content for easier viewing
        if content_type and content_type.startswith("text/"):
            try:
                decoded_content = content.decode("utf-8")
                return {
                    "path": path,
                    "content": decoded_content,
                    "content_type": content_type,
                    "size": len(content),
                    "etag": etag,
                }
            except UnicodeDecodeError:
                pass

        # For binary files, return metadata and base64 encoded content

        return {
            "path": path,
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
            "size": len(content),
            "encoding": "base64",
            "etag": etag,
        }

    @mcp.tool(
        title="Write File",
        annotations=ToolAnnotations(
            # Not idempotent: the write is fail-closed. A create (no if_match)
            # succeeds once then returns 412 ("already exists") on repeat, and
            # an if_match overwrite is invalidated by its own success (the etag
            # changes) -- mirroring nc_notes_update_note's etag-guarded update.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_write_file(
        path: str,
        content: str,
        ctx: Context,
        content_type: str | None = None,
        if_match: str | None = None,
    ):
        """Write content to a file in NextCloud.

        Writes are **fail-closed**: an existing file is never silently
        overwritten. What happens depends on ``if_match`` (see below).

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        target path (or an ancestor folder) carries an excluded system tag;
        when the decoded content exceeds ``WEBDAV_WRITE_MAX_MB``; when the
        write conflicts with a concurrent edit or an existing/missing file
        (412); or when the file is locked by another client (423) -- see
        ``if_match`` below.

        Args:
            path: Full path where to write the file
            content: File content (text or base64 for binary)
            content_type: MIME type (auto-detected if not provided, use 'type;base64' for binary)
            if_match: Controls overwrite safety.
                - Omit (``None``) to **create a new file**: the write fails
                  with a ``ToolError`` if the path already exists. To change an
                  existing file you must first read it with
                  ``nc_webdav_read_file`` and pass the etag it returned.
                - Pass that **etag** to overwrite the file only if it has not
                  changed since you read it; if someone edited it in the
                  meantime (e.g. in the Nextcloud web UI) the write fails --
                  re-read and retry deliberately rather than looping.
                - Pass the literal ``"*"`` to **force-overwrite** an existing
                  file unconditionally (fails if the file does not exist).

        Returns:
            Dict with status_code indicating success
        """
        client = await get_client(ctx)

        # Block writes to excluded paths.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

        # Handle base64 encoded content
        if content_type and "base64" in content_type.lower():
            content_bytes = base64.b64decode(content)
            content_type = content_type.replace(";base64", "")
        else:
            content_bytes = content.encode("utf-8")

        # Pre-flight size gate: a single-shot PUT built from one in-memory MCP
        # tool argument has no chunked/streaming path, so fail fast with a
        # clear error rather than risk a timeout or OOM on a huge file.
        max_mb = get_settings().webdav_write_max_mb
        if max_mb:
            size_mb = len(content_bytes) / (1024 * 1024)
            if size_mb > max_mb:
                raise ToolError(
                    f"Refusing to write {path!r}: {size_mb:.1f} MB exceeds the "
                    f"configured WEBDAV_WRITE_MAX_MB ({max_mb} MB). Write the "
                    "file directly via the Nextcloud web UI or a synced client "
                    "instead, or raise WEBDAV_WRITE_MAX_MB if this size is "
                    "expected."
                )

        result = await client.webdav.write_file(
            path, content_bytes, content_type, if_match=if_match
        )
        # write_file returns (rather than raises) on 412/423 -- known
        # conflict statuses the caller must react to, mirroring
        # move_resource/copy_resource. Raise here so they surface exactly
        # like any other actionable failure of this tool.
        status_code = result.get("status_code")
        if status_code in (412, 423):
            raise ToolError(f"{result['message']} ({path!r})")
        return result

    @mcp.tool(
        title="Create Directory",
        annotations=ToolAnnotations(
            idempotentHint=True,  # Creating existing dir returns 405 = same end state
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_create_directory(path: str, ctx: Context):
        """Create a directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        target path lies inside a folder carrying an excluded system tag.

        Args:
            path: Full path of the directory to create

        Returns:
            Dict with status_code (201 for created, 405 if already exists)
        """
        client = await get_client(ctx)

        # Block directory creation at or inside excluded paths.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(
                f"Access denied: {path!r} is or is inside a path tagged "
                "with an excluded tag"
            )

        return await client.webdav.create_directory(path)

    @mcp.tool(
        title="Delete File or Directory",
        annotations=ToolAnnotations(
            destructiveHint=True,  # Permanently deletes data
            idempotentHint=True,  # Deleting deleted resource = same end state
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_delete_resource(path: str, ctx: Context):
        """Delete a file or directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        target path (or an ancestor folder) carries an excluded system tag.

        Args:
            path: Full path of the file or directory to delete

        Returns:
            Dict with status_code indicating result (404 if not found)
        """
        client = await get_client(ctx)

        # Block deletion of excluded files/directories.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

        return await client.webdav.delete_resource(path)

    @mcp.tool(
        title="Move or Rename File",
        annotations=ToolAnnotations(
            idempotentHint=False,  # Moving changes source and dest
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_move_resource(
        source_path: str, destination_path: str, ctx: Context, overwrite: bool = False
    ):
        """Move or rename a file or directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and either
        the source or destination path (or one of their ancestor folders)
        carries an excluded system tag.

        Args:
            source_path: Full path of the file or directory to move
            destination_path: New path for the file or directory
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = await get_client(ctx)

        # Block moves involving excluded paths on either side.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(source_path, excluded):
            raise ToolError(
                f"Access denied: source {source_path!r} is tagged with an excluded tag"
            )
        if is_path_excluded(destination_path, excluded):
            raise ToolError(
                f"Access denied: destination {destination_path!r} is or is "
                "inside a path tagged with an excluded tag"
            )

        return await client.webdav.move_resource(
            source_path, destination_path, overwrite
        )

    @mcp.tool(
        title="Copy File or Directory",
        annotations=ToolAnnotations(
            idempotentHint=False,  # Creates new resource each time
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_copy_resource(
        source_path: str, destination_path: str, ctx: Context, overwrite: bool = False
    ):
        """Copy a file or directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and either
        the source or destination path (or one of their ancestor folders)
        carries an excluded system tag.

        Args:
            source_path: Full path of the file or directory to copy
            destination_path: Destination path for the copy
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = await get_client(ctx)

        # Block copies involving excluded paths on either side.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(source_path, excluded):
            raise ToolError(
                f"Access denied: source {source_path!r} is tagged with an excluded tag"
            )
        if is_path_excluded(destination_path, excluded):
            raise ToolError(
                f"Access denied: destination {destination_path!r} is or is "
                "inside a path tagged with an excluded tag"
            )

        return await client.webdav.copy_resource(
            source_path, destination_path, overwrite
        )

    @mcp.tool(
        title="Search Files",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_search_files(
        ctx: Context,
        scope: str = "",
        name_pattern: str | None = None,
        mime_type: str | None = None,
        only_favorites: bool = False,
        limit: int | None = None,
    ) -> SearchFilesResponse:
        """Search for files in NextCloud using WebDAV SEARCH.

        This is a high-level search tool that supports common search patterns.
        For more complex queries, use the specific search tools.

        Args:
            scope: Directory path to search in (empty string for user root)
            name_pattern: File name pattern (supports % wildcard, e.g., "%.txt" for all text files)
            mime_type: MIME type to filter by (supports % wildcard, e.g., "image/%" for all images)
            only_favorites: If True, only return favorited files
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = await get_client(ctx)

        # Resolve once and use for both the scope guard and the result filter.
        excluded = await get_excluded_file_paths(client.webdav)
        if scope and is_path_excluded(scope, excluded):
            raise ToolError(
                f"Access denied: scope {scope!r} is tagged with an excluded tag"
            )

        # Build where conditions based on filters
        conditions = []

        if name_pattern:
            conditions.append(
                f"""
                <d:like>
                    <d:prop>
                        <d:displayname/>
                    </d:prop>
                    <d:literal>{name_pattern}</d:literal>
                </d:like>
            """
            )

        if mime_type:
            conditions.append(
                f"""
                <d:like>
                    <d:prop>
                        <d:getcontenttype/>
                    </d:prop>
                    <d:literal>{mime_type}</d:literal>
                </d:like>
            """
            )

        if only_favorites:
            conditions.append(
                """
                <d:eq>
                    <d:prop>
                        <oc:favorite/>
                    </d:prop>
                    <d:literal>1</d:literal>
                </d:eq>
            """
            )

        # Combine conditions with AND if multiple
        if len(conditions) > 1:
            where_conditions = f"""
                <d:and>
                    {"".join(conditions)}
                </d:and>
            """
        elif len(conditions) == 1:
            where_conditions = conditions[0]
        else:
            where_conditions = None

        # Include extended properties
        properties = [
            "displayname",
            "getcontentlength",
            "getcontenttype",
            "getlastmodified",
            "resourcetype",
            "getetag",
            "fileid",
            "favorite",
        ]

        results = await client.webdav.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            limit=limit,
        )

        # Filter out tagged-excluded paths from the result set.
        if excluded:
            results = [
                r for r in results if not is_path_excluded(r.get("path", ""), excluded)
            ]

        # Convert to FileInfo models
        file_infos = [FileInfo(**result) for result in results]

        # Build filters applied dict
        filters = {}
        if name_pattern:
            filters["name_pattern"] = name_pattern
        if mime_type:
            filters["mime_type"] = mime_type
        if only_favorites:
            filters["only_favorites"] = True

        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied=filters if filters else None,
        )

    @mcp.tool(
        title="Find Files by Name",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_find_by_name(
        pattern: str, ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """Find files by name pattern in NextCloud.

        Args:
            pattern: Name pattern to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        if scope and is_path_excluded(scope, excluded):
            raise ToolError(
                f"Access denied: scope {scope!r} is tagged with an excluded tag"
            )
        results = await client.webdav.find_by_name(
            pattern=pattern, scope=scope, limit=limit
        )
        if excluded:
            results = [
                r for r in results if not is_path_excluded(r.get("path", ""), excluded)
            ]
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"name_pattern": pattern},
        )

    @mcp.tool(
        title="Find Files by Type",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_find_by_type(
        mime_type: str, ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """Find files by MIME type in NextCloud.

        Args:
            mime_type: MIME type to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        if scope and is_path_excluded(scope, excluded):
            raise ToolError(
                f"Access denied: scope {scope!r} is tagged with an excluded tag"
            )
        results = await client.webdav.find_by_type(
            mime_type=mime_type, scope=scope, limit=limit
        )
        if excluded:
            results = [
                r for r in results if not is_path_excluded(r.get("path", ""), excluded)
            ]
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"mime_type": mime_type},
        )

    @mcp.tool(
        title="List Favorite Files",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_favorites(
        ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """List all favorite files in NextCloud.

        Args:
            scope: Directory path to search in (empty string for all favorites)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of favorite files
        """
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        if scope and is_path_excluded(scope, excluded):
            raise ToolError(
                f"Access denied: scope {scope!r} is tagged with an excluded tag"
            )
        results = await client.webdav.list_favorites(scope=scope, limit=limit)
        if excluded:
            results = [
                r for r in results if not is_path_excluded(r.get("path", ""), excluded)
            ]
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"only_favorites": True},
        )
