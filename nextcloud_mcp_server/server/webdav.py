import atexit
import base64
import io
import logging
import mimetypes
import os
import tempfile
import zipfile

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models import DirectoryListing, FileInfo, SearchFilesResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.utils.document_parser import (
    is_parseable_document,
    parse_document,
)

logger = logging.getLogger(__name__)

# Registry of local temp paths created by nc_webdav_download_to_temp.
# Used to prevent nc_webdav_cleanup_temp from deleting arbitrary paths.
# Plain set is safe: asyncio is single-threaded and GIL protects simple ops.
_temp_registry: set[str] = set()


def _cleanup_temp_files_on_exit() -> None:
    """Remove all temp files registered by nc_webdav_download_to_temp on process exit."""
    for path in list(_temp_registry):
        try:
            os.unlink(path)
            logger.debug("atexit: removed temp file '%s'", path)
        except OSError:
            pass


atexit.register(_cleanup_temp_files_on_exit)


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

        Args:
            path: Directory path to list (empty string for root directory)

        Returns:
            DirectoryListing with files, total_count, directories_count, files_count, and total_size
        """
        client = await get_client(ctx)
        items = await client.webdav.list_directory(path)

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
    async def nc_webdav_read_file(path: str, ctx: Context):
        """Read a file from Nextcloud and return its content inline.

        IMPORTANT — choose the right tool for the file type:

        ✅ Use THIS tool for:
          - Plain text files (Markdown, CSV, JSON, XML, YAML, source code, logs)
            that fit in the context window (roughly < 1 MB of text).
          - PDFs, when the document-processing feature is enabled server-side
            (text is extracted automatically).

        ❌ Do NOT use this tool for:
          - ZIP-based office formats (ODS, ODT, ODP, DOCX, XLSX, PPTX, EPUB …).
            If server-side document processing is enabled (ENABLE_DOCUMENT_PROCESSING=true)
            and a processor supports the type (e.g. Unstructured handles DOCX/XLSX),
            text is extracted automatically — check the server configuration.
            When doc-processing is disabled or unsupported for the type, the raw
            archive bytes are meaningless in context; use
            nc_webdav_list_archive_members + nc_webdav_read_archive_member instead.
          - Images (PNG, JPEG, GIF, TIFF, HEIC, RAW …).
            Binary image data cannot be interpreted here. Use
            nc_webdav_download_to_temp and process locally with tools such as
            `convert`, `exiftool`, or `ffmpeg` — only if you have local shell access.
          - Audio or video files (MP4, MKV, MP3, FLAC …).
            Use nc_webdav_download_to_temp + `ffmpeg`/`ffprobe` if you have shell
            access; otherwise these files cannot be processed via MCP.
          - Any binary file larger than ~1 MB. The file will be returned as a
            base64 blob that wastes the entire context without yielding useful
            information. Check the file size with nc_webdav_list_directory first.

        Fallback behaviour (binary files not covered above):
          The raw bytes are base64-encoded and returned. This is rarely useful
          — prefer the dedicated tools described above.

        Args:
            path: Full path to the file to read

        Returns:
            Dict with path, content, content_type, size, and optional parsing metadata
            - Text files: content decoded to UTF-8 string
            - PDFs (doc-processing enabled): extracted plain text
            - Other binary files: content base64-encoded (avoid for large files)
        """
        client = await get_client(ctx)
        content, content_type = await client.webdav.read_file(path)

        # Check if this is a parseable document (PDF, DOCX, etc.)
        # is_parseable_document() checks if document processing is enabled
        if is_parseable_document(content_type):
            try:
                logger.info(f"Parsing document '{path}' of type '{content_type}'")
                parsed_text, metadata = await parse_document(
                    content,
                    content_type,
                    filename=path,
                    progress_callback=ctx.report_progress,
                )
                return {
                    "path": path,
                    "content": parsed_text,
                    "content_type": content_type,
                    "size": len(content),
                    "parsed": True,
                    "parsing_metadata": metadata,
                }
            except Exception as e:
                logger.warning(
                    f"Failed to parse document '{path}', falling back to base64: {e}"
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
        }

    @mcp.tool(
        title="Write File",
        annotations=ToolAnnotations(
            idempotentHint=True,  # HTTP PUT without version control is idempotent
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_write_file(
        path: str, content: str, ctx: Context, content_type: str | None = None
    ):
        """Write content to a file in NextCloud.

        Args:
            path: Full path where to write the file
            content: File content (text or base64 for binary)
            content_type: MIME type (auto-detected if not provided, use 'type;base64' for binary)

        Returns:
            Dict with status_code indicating success
        """
        client = await get_client(ctx)

        # Handle base64 encoded content
        if content_type and "base64" in content_type.lower():
            content_bytes = base64.b64decode(content)
            content_type = content_type.replace(";base64", "")
        else:
            content_bytes = content.encode("utf-8")

        return await client.webdav.write_file(path, content_bytes, content_type)

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

        Args:
            path: Full path of the directory to create

        Returns:
            Dict with status_code (201 for created, 405 if already exists)
        """
        client = await get_client(ctx)
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

        Args:
            path: Full path of the file or directory to delete

        Returns:
            Dict with status_code indicating result (404 if not found)
        """
        client = await get_client(ctx)
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

        Args:
            source_path: Full path of the file or directory to move
            destination_path: New path for the file or directory
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = await get_client(ctx)
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

        Args:
            source_path: Full path of the file or directory to copy
            destination_path: Destination path for the copy
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = await get_client(ctx)
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
        results = await client.webdav.find_by_name(
            pattern=pattern, scope=scope, limit=limit
        )
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
        results = await client.webdav.find_by_type(
            mime_type=mime_type, scope=scope, limit=limit
        )
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
        results = await client.webdav.list_favorites(scope=scope, limit=limit)
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"only_favorites": True},
        )

    @mcp.tool(
        title="List Archive Members",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_archive_members(path: str, ctx: Context) -> dict:
        """List the files contained inside a ZIP-based archive stored in Nextcloud.

        Supported archive formats (all are ZIP-based):
          Office: ODS, ODT, ODP, ODG, DOCX, XLSX, PPTX
          Other:  ZIP, JAR, EPUB

        Use this tool first to discover the internal structure of an archive,
        then call nc_webdav_read_archive_member to read a specific member.

        Typical ODF layout:
          mimetype          — identifies the ODF sub-type
          content.xml       — document content
          styles.xml        — formatting styles
          meta.xml          — document metadata
          settings.xml      — application settings
          META-INF/manifest.xml — archive manifest

        Args:
            path: Nextcloud path to the archive file (e.g. "Documents/report.ods")

        Returns:
            Dict with path, content_type, archive_size, member_count, and a
            members list. Each member has: name, size (uncompressed),
            compressed_size, is_dir.

        Raises:
            ValueError: if the file is not a valid ZIP archive
        """
        client = await get_client(ctx)
        content, content_type = await client.webdav.read_file(path)

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                members = [
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressed_size": info.compress_size,
                        "is_dir": info.is_dir(),
                    }
                    for info in zf.infolist()
                ]
        except zipfile.BadZipFile as exc:
            raise ValueError(
                f"'{path}' (content-type: {content_type}) is not a valid ZIP archive. "
                f"For plain text files use nc_webdav_read_file; for images/video/audio "
                f"use nc_webdav_download_to_temp."
            ) from exc

        return {
            "path": path,
            "content_type": content_type,
            "archive_size": len(content),
            "member_count": len(members),
            "members": members,
        }

    @mcp.tool(
        title="Read Archive Member",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_read_archive_member(
        path: str, member_path: str, ctx: Context
    ) -> dict:
        """Extract and return a single file from inside a ZIP-based archive in Nextcloud.

        The whole archive is downloaded, but only the requested member is
        returned — it never appears in the context as a base64 blob.

        Supported archive formats: ODS, ODT, ODP, ODG, DOCX, XLSX, PPTX,
        ZIP, JAR, EPUB (anything that Python's zipfile module can open).

        Typical use-cases:
          - Read content.xml from an ODS/ODT/ODP to get document content
          - Read word/document.xml from a DOCX
          - Read xl/worksheets/sheet1.xml from an XLSX
          - Inspect META-INF/manifest.xml to understand archive structure

        Use nc_webdav_list_archive_members first to discover available member paths.

        Args:
            path: Nextcloud path to the archive (e.g. "Documents/budget.ods")
            member_path: Path of the member inside the archive
                         (e.g. "content.xml" or "META-INF/manifest.xml")

        Returns:
            Dict with archive_path, member_path, content, content_type, size.
            Text members (XML, HTML, JSON, plain text …) are returned as UTF-8
            strings. Binary members are base64-encoded with encoding="base64".

        Raises:
            ValueError: if the archive is not valid ZIP, or the member is not found
        """
        client = await get_client(ctx)
        content, content_type = await client.webdav.read_file(path)

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                try:
                    member_bytes = zf.read(member_path)
                except KeyError as exc:
                    available = [i.filename for i in zf.infolist() if not i.is_dir()]
                    raise ValueError(
                        f"Member '{member_path}' not found in '{path}'. "
                        f"Available files: {available[:30]}"
                        + (" (truncated)" if len(available) > 30 else "")
                    ) from exc
        except zipfile.BadZipFile as exc:
            raise ValueError(f"'{path}' is not a valid ZIP archive.") from exc

        member_mime = mimetypes.guess_type(member_path)[0] or "application/octet-stream"

        # Return text members decoded; XML files are always text even without
        # an explicit text/* MIME type.
        is_text = (
            member_mime.startswith("text/")
            or member_mime
            in {
                "application/xml",
                "application/json",
                "application/javascript",
            }
            or member_path.endswith((".xml", ".json", ".html", ".css", ".js", ".svg"))
        )

        if is_text:
            try:
                return {
                    "archive_path": path,
                    "member_path": member_path,
                    "content": member_bytes.decode("utf-8"),
                    "content_type": member_mime,
                    "size": len(member_bytes),
                }
            except UnicodeDecodeError:
                pass  # fall through to base64

        return {
            "archive_path": path,
            "member_path": member_path,
            "content": base64.b64encode(member_bytes).decode("ascii"),
            "content_type": member_mime,
            "size": len(member_bytes),
            "encoding": "base64",
        }

    @mcp.tool(
        title="Download File to Temp",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_download_to_temp(path: str, ctx: Context) -> dict:
        """Download a Nextcloud file to a local temporary path and return that path.

        IMPORTANT — this tool is only useful when you have access to local shell
        tools (e.g. Claude Code's Bash tool). In Claude Desktop without shell
        access the returned path cannot be acted upon and you should not call
        this tool.

        Use this tool for file types that require native processing:
          Images   — then use: convert, exiftool, ffmpeg, identify
          Video    — then use: ffmpeg, ffprobe, mediainfo
          Audio    — then use: ffmpeg, ffprobe, sox
          PDFs     — then use: pdftotext, pdfinfo, pdftk, mutool
          Archives — for formats NOT supported by nc_webdav_list_archive_members
                     (e.g. .tar.gz, .7z, .rar): use tar, 7z, unrar
          Any large binary that requires local tooling

        For ZIP-based office formats (ODS, DOCX, XLSX …) prefer
        nc_webdav_list_archive_members + nc_webdav_read_archive_member —
        they avoid creating temp files entirely.

        Cleanup: always call nc_webdav_cleanup_temp when finished to free disk
        space. All remaining temp files are also removed automatically when the
        MCP server process exits (via an atexit handler).

        Args:
            path: Nextcloud path to the file (e.g. "Videos/holiday.mp4")

        Returns:
            Dict with:
              local_path    — absolute path on the local filesystem
              original_path — original Nextcloud path
              filename      — basename of the original file
              content_type  — MIME type reported by Nextcloud
              size          — file size in bytes
        """
        client = await get_client(ctx)
        content, content_type = await client.webdav.read_file(path)

        filename = os.path.basename(path.rstrip("/"))
        _root, suffix = os.path.splitext(filename)

        fd, local_path = tempfile.mkstemp(suffix=suffix, prefix="nc_download_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
        except Exception:
            try:
                os.unlink(local_path)
            except OSError:
                pass
            raise

        _temp_registry.add(local_path)
        logger.debug(
            "Downloaded '%s' to temp path '%s' (%d bytes)",
            path,
            local_path,
            len(content),
        )

        return {
            "local_path": local_path,
            "original_path": path,
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
        }

    @mcp.tool(
        title="Remove Temp File",
        annotations=ToolAnnotations(
            destructiveHint=True,
            idempotentHint=False,  # errors on second call (path no longer in registry)
            openWorldHint=False,  # operates on local filesystem only
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_cleanup_temp(local_path: str, ctx: Context) -> dict:
        """Remove a temporary file created by nc_webdav_download_to_temp.

        Only paths that were created by nc_webdav_download_to_temp in this
        server session can be removed — arbitrary filesystem paths are rejected.

        Call this when you are done processing a downloaded file to free
        disk space.

        Args:
            local_path: The local_path value returned by nc_webdav_download_to_temp

        Returns:
            Dict with status ("ok" or "error") and the local_path.
        """
        if local_path not in _temp_registry:
            return {
                "status": "error",
                "local_path": local_path,
                "message": (
                    "Path was not created by nc_webdav_download_to_temp in this "
                    "session, or has already been cleaned up."
                ),
            }

        try:
            os.unlink(local_path)
            _temp_registry.discard(local_path)
            logger.debug("Removed temp file '%s'", local_path)
            return {"status": "ok", "local_path": local_path}
        except FileNotFoundError:
            # File already gone — treat as success and clean up registry.
            _temp_registry.discard(local_path)
            return {
                "status": "ok",
                "local_path": local_path,
                "note": "File was already removed.",
            }
        except OSError as exc:
            # Do NOT discard — leave in registry so the caller can retry.
            return {"status": "error", "local_path": local_path, "message": str(exc)}
