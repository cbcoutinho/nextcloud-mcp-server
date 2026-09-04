import base64
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Literal, Optional

import anyio
from anyio.to_thread import run_sync
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base
from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.webdav import like_predicate
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.links import file_url, with_links
from nextcloud_mcp_server.models import (
    CopyResourceResponse,
    CreateFileCommentResponse,
    DirectoryListing,
    FileComment,
    FileInfo,
    ListFileCommentsResponse,
    MoveResourceResponse,
    ReadFileResponse,
    SearchFilesResponse,
    WriteFileResponse,
)
from nextcloud_mcp_server.models.webdav import (
    FileVersion,
    ListTrashResponse,
    ListVersionsResponse,
    ParseStatus,
    RestoreFromTrashResponse,
    RestoreVersionResponse,
    TrashEntry,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)
from nextcloud_mcp_server.utils.message_splitter import (
    COMMENT_MAX_LENGTH,
    is_blank_comment,
    measured_length,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle / lazy-import guard
    from nextcloud_mcp_server.client import NextcloudClient
    from nextcloud_mcp_server.document_processors.source import DocumentSource

logger = logging.getLogger(__name__)

# move_resource/copy_resource return (rather than raise) on these, since they
# are conditions a caller reacts to rather than transport failures. They are
# what makes ``success`` False on the typed response.
_WEBDAV_CONFLICT_STATUSES = frozenset({404, 409, 412})

#: Ceiling on the bytes an unparsed file may contribute to one MCP response.
#: Base64 inflates by ~4/3 and the whole thing lands in a model's context, so a
#: large binary is not merely expensive to return -- it is unusable once it
#: arrives. Past this the response carries the file's metadata and says why the
#: content is absent. A constant rather than a setting: the useful bound is the
#: client's context, not anything an operator knows better.
RAW_CONTENT_MAX_BYTES = 5 * 1024 * 1024


def _stamp_url(response: ReadFileResponse, url: str | None) -> ReadFileResponse:
    """Attach the Files-app link to a response, in place.

    Stamped after the fact rather than passed into every ``ReadFileResponse(...)``
    in this module: the read path builds one at five separate sites, and a
    constructor argument that one of them forgets reverts silently to None (the
    failure mode ``test_semantic_result_field_parity.py`` exists to catch in the
    semantic tool). Every return goes through here instead, so a sixth site
    added later is linked by construction rather than by remembering to.
    """
    response.url = url
    return response


async def _raw_response(
    source: "DocumentSource",
    path: str,
    parse_status: ParseStatus,
    notes: list[str],
    *,
    parse_tier: str | None = None,
    parse_processor: str | None = None,
    parsing_metadata: dict | None = None,
) -> ReadFileResponse:
    """Return the file itself: decoded text, or base64 bytes.

    Reads back from the spool rather than from a download buffer held across the
    parse -- that buffer is what used to make peak memory scale with document
    size -- and stops at :data:`RAW_CONTENT_MAX_BYTES` so the "we could not parse
    it" fallback cannot itself blow up the response.

    Peak here is bounded accordingly: on the only path that reaches this with a
    parse behind it (a FAILED parse), the failed result carries no text, so what
    is resident is one capped read. A successful parse returns its text directly
    and never calls this.

    The read runs on a worker thread: it is a synchronous disk read that would
    otherwise stall every other request on this event loop.
    """
    size = source.size
    content_type = source.content_type
    # Genuinely optional: only a streamed (spooled) source carries the origin's
    # etag; an in-memory one has no transport response to have read it from.
    etag = getattr(source, "etag", None)

    def _read_capped() -> bytes | None:
        if size > RAW_CONTENT_MAX_BYTES:
            return None
        with source.open() as fh:
            return fh.read()

    content = await run_sync(_read_capped)

    if content is None:
        return ReadFileResponse(
            path=path,
            content="",
            content_type=content_type,
            size=size,
            parse_status=parse_status,
            parse_tier=parse_tier,
            parse_processor=parse_processor,
            parse_notes=[
                *notes,
                f"The file itself ({size / (1024 * 1024):.1f} MB) is too large to "
                f"return inline; download it from Nextcloud directly.",
            ],
            parsing_metadata=parsing_metadata,
            etag=etag,
        )

    if content_type.startswith("text/"):
        try:
            return ReadFileResponse(
                path=path,
                content=content.decode("utf-8"),
                content_type=content_type,
                size=size,
                parse_status=parse_status,
                parse_tier=parse_tier,
                parse_processor=parse_processor,
                parse_notes=notes,
                parsing_metadata=parsing_metadata,
                etag=etag,
            )
        except UnicodeDecodeError:
            # Mislabelled text/*: fall through and hand back the bytes.
            pass

    return ReadFileResponse(
        path=path,
        content=base64.b64encode(content).decode("ascii"),
        content_type=content_type,
        size=size,
        encoding="base64",
        content_format="base64",
        parse_status=parse_status,
        parse_tier=parse_tier,
        parse_processor=parse_processor,
        parse_notes=notes,
        parsing_metadata=parsing_metadata,
        etag=etag,
    )


def _as_int(raw: Any) -> Optional[int]:
    """DAV numbers arrive as text; a non-numeric one costs that field, not the row."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _resolve_commented_file(client: "NextcloudClient", path: str) -> int:
    """Resolve ``path`` to the file ID the comments collection is keyed by.

    Shared by both comment tools so the excluded-tag guard and the
    does-it-exist check cannot drift between reading and writing comments.

    Raises:
        ToolError: If the path is excluded by tag, resolves to nothing, or
            resolves to something that is not a numeric file id.
    """
    excluded = await get_excluded_file_paths(client.webdav)
    if is_path_excluded(path, excluded):
        raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

    file_id = await client.webdav.get_fileid(path)
    if file_id is None:
        raise ToolError(f"File not found: {path!r}")
    try:
        return int(file_id)
    except ValueError:
        # Nextcloud always reports a numeric oc:fileid; anything else means the
        # PROPFIND response shape changed, and a clear refusal beats a
        # ValueError from deep inside the URL we would have built with it.
        raise ToolError(
            f"Unexpected non-numeric file id {file_id!r} for {path!r}"
        ) from None


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
    @with_links
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
        path: str,
        ctx: Context,
        parse_document: Literal["auto", "markdown", "raw"] = "auto",
    ) -> ReadFileResponse:
        """Read the content of a file from NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        file (or an ancestor folder) carries an excluded system tag.

        Args:
            path: Full path to the file to read
            parse_document: How to handle a document (PDF, DOCX, image, ...):

                - ``"auto"`` (default): extract its text. Cheapest route that
                  works -- a PDF with a good text layer is read directly, and a
                  scanned one escalates to OCR when the server has OCR enabled.
                - ``"markdown"``: additionally reconstruct structure (headings,
                  tables) rather than returning a flat text layer. Costs a
                  second, slower parse and is bounded by a page ceiling, when it
                  cannot be honoured the response says so instead of pretending.
                - ``"raw"``: do not parse. Text files are decoded, anything else
                  comes back base64-encoded.

                Files no processor handles (plain text, JSON, archives) are
                unaffected by this argument.

        Returns:
            ``ReadFileResponse``. Alongside ``path``/``content``/``content_type``/
            ``size``/``etag`` it always describes what you are actually holding:

            - ``parse_status``: ``parsed`` / ``failed`` / ``skipped`` /
              ``not_applicable``.
            - ``content_format``: ``markdown``, ``text`` or ``base64``.
            - ``parse_tier`` / ``parse_processor``: which extraction tier
              produced the content (``fast``, ``structured``, ``ocr``).
            - ``parse_notes``: **if this is non-empty, tell the user what
              degraded** (OCR unavailable, structure not reconstructed, size cap,
              parse failure) rather than presenting the content as the complete
              document.
            - ``etag``: pass this back into ``nc_webdav_write_file``'s
              ``if_match`` when writing this same path later, so a manual edit
              made elsewhere in the meantime (e.g. in the Nextcloud web UI) is
              detected as a conflict instead of silently overwritten.
            - ``url``: a link that opens the file in Nextcloud. Offer it when
              reporting on the file, and especially when ``parse_notes`` says
              the extraction degraded.
        """
        client = await get_client(ctx)

        # Block reads of paths carrying an excluded tag.
        excluded = await get_excluded_file_paths(client.webdav)
        if is_path_excluded(path, excluded):
            raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

        # Nextcloud's /f/ route needs the fileid, and a WebDAV GET does not
        # return one -- only a PUT sends OC-FileId -- so it costs a Depth-0
        # PROPFIND. Negligible beside the download-and-parse that follows, and
        # skipped entirely when no browser-reachable base URL is configured,
        # since there would be nothing to build a link from. Resolved once here
        # rather than at each of the five sites that build the response.
        #
        # Fail-open on purpose: the link is a convenience, the content is the
        # tool's job. A PROPFIND that 403s, times out or returns unparseable XML
        # must cost the caller its link, never its read -- so the lookup is
        # caught broadly (get_fileid raises HTTPStatusError, RequestError or
        # ParseError depending on how it goes wrong) and logged rather than
        # propagated. The read that follows surfaces any real access problem
        # with a far better error than this lookup could.
        browser_base = astrolabe_browser_base()
        url = None
        if browser_base:
            try:
                url = file_url(browser_base, await client.webdav.get_fileid(path))
            except Exception as e:
                logger.debug("No file link for %r: fileid lookup failed: %s", path, e)

        # Imported lazily so server startup never loads the document-parsing
        # stack (document_processors -> pymupdf -> _isolation). That stack is an
        # ingest-layer concern and, before this, broke Windows startup via a
        # Unix-only ``import resource`` (#877). It is only needed when a file is
        # actually read and parsed.
        #
        # Imported as a MODULE, not by name: this tool has a parameter called
        # ``parse_document``, and ``from ... import parse_document`` would rebind
        # it and silently discard the caller's choice.
        from nextcloud_mcp_server.client.webdav import OversizeDownload  # noqa: PLC0415
        from nextcloud_mcp_server.utils import document_parser  # noqa: PLC0415
        from nextcloud_mcp_server.vector.spool import (  # noqa: PLC0415
            download_ceiling,
            spooled_document,
        )

        settings = get_settings()
        ceiling = download_ceiling(settings)

        # Stream the document to a spool file instead of buffering the whole
        # response: this tool runs in the API role, which is not sized to hold a
        # multi-hundred-MB document in memory, and the tiered pipeline parses
        # straight from the path (page-windowed) once it is there. The ceiling is
        # the same one ingest streams under, so a runaway transfer is aborted
        # rather than filling the disk. The block owns the spool file: everything
        # that touches the document must happen inside it.
        try:
            async with spooled_document(
                client,
                path,
                spool_dir=settings.document_spool_dir,
                max_bytes=ceiling,
            ) as source:
                content_type = source.content_type
                etag = source.etag

                if parse_document != "raw" and document_parser.is_parseable_document(
                    content_type
                ):
                    # Optional interactive cap (ADR-032): bound the SYNCHRONOUS
                    # parse so a slow VLM/OCR convert returns the raw file quickly
                    # instead of blocking past the MCP client's own timeout.
                    # Disabled (None) -> nullcontext. Only wraps this interactive
                    # tool; the async ingest/worker path is never bounded here.
                    read_cap = settings.document_read_timeout_seconds
                    cap_ctx = (
                        anyio.fail_after(read_cap)
                        if read_cap is not None
                        else contextlib.nullcontext()
                    )
                    try:
                        logger.info(
                            "Parsing document %r of type %r (mode=%s)",
                            path,
                            content_type,
                            parse_document,
                        )
                        with cap_ctx:
                            result = await document_parser.parse_document_source(
                                source,
                                prefer_markdown=(parse_document == "markdown"),
                                progress_callback=ctx.report_progress,
                            )
                    except TimeoutError as e:
                        # Caught before the generic Exception (subclass-first). When
                        # the cap is set this is our anyio.fail_after tripping; when
                        # it is None the TimeoutError bubbled from a backend's own
                        # anyio timeout (e.g. the Mistral OCR path).
                        note = (
                            f"Parsing was aborted after {read_cap}s "
                            f"(DOCUMENT_READ_TIMEOUT_SECONDS); the raw file is "
                            f"returned instead."
                            if read_cap is not None
                            else f"Parsing timed out ({e}); the raw file is returned "
                            f"instead."
                        )
                        logger.warning("Parsing document %r timed out: %s", path, e)
                        return _stamp_url(
                            await _raw_response(source, path, "failed", [note]), url
                        )
                    except Exception as e:
                        logger.warning("Failed to parse document %r: %s", path, e)
                        return _stamp_url(
                            await _raw_response(
                                source,
                                path,
                                "failed",
                                [
                                    f"Parsing failed ({type(e).__name__}: {e}); the "
                                    f"raw file is returned instead."
                                ],
                            ),
                            url,
                        )

                    summary = document_parser.summarize_parse(
                        result,
                        settings,
                        markdown_requested=(parse_document == "markdown"),
                    )
                    if summary.status == "failed":
                        # An unsuccessful parse is never reported as content: hand
                        # back the raw file with the reason attached.
                        return _stamp_url(
                            await _raw_response(
                                source,
                                path,
                                "failed",
                                summary.notes,
                                parse_tier=summary.tier,
                                parse_processor=summary.processor,
                                parsing_metadata=result.metadata,
                            ),
                            url,
                        )
                    return _stamp_url(
                        ReadFileResponse(
                            path=path,
                            content=result.text,
                            content_type=content_type,
                            size=source.size,
                            parsed=True,
                            parse_status="parsed",
                            parse_tier=summary.tier,
                            parse_processor=summary.processor,
                            content_format=summary.content_format,
                            parse_notes=summary.notes,
                            parsing_metadata=result.metadata,
                            etag=etag,
                        ),
                        url,
                    )

                status: ParseStatus = (
                    "skipped" if parse_document == "raw" else "not_applicable"
                )
                return _stamp_url(await _raw_response(source, path, status, []), url)
        except OversizeDownload as e:
            # The transfer was aborted mid-flight, so there is no file left to
            # describe -- not even its content type. Say that plainly rather than
            # returning an empty response that reads like an empty document.
            raise ToolError(
                f"{path!r} was not downloaded: it exceeds the {ceiling} byte "
                f"transfer ceiling for a single read (twice "
                f"DOCUMENT_MAX_PDF_SIZE_MB). {e}"
            ) from e

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
    ) -> WriteFileResponse:
        """Write content to a file in NextCloud.

        Writes are **fail-closed**: an existing file is never silently
        overwritten. What happens depends on ``if_match`` (see below).

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and the
        target path (or an ancestor folder) carries an excluded system tag,
        when the decoded content exceeds ``WEBDAV_WRITE_MAX_MB``, when the
        write conflicts with a concurrent edit or an existing/missing file
        (412), or when the file is locked by another client (423) -- see
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
                  changed since you read it. If someone edited it in the
                  meantime (e.g. in the Nextcloud web UI) the write fails --
                  re-read and retry deliberately rather than looping.
                - Pass the literal ``"*"`` to **force-overwrite** an existing
                  file unconditionally (fails if the file does not exist).

        Returns:
            ``WriteFileResponse`` with ``path``, ``status_code``, ``size``,
            ``created`` (True when a new file was created, i.e. HTTP 201, False
            when an existing file was overwritten, i.e. HTTP 204) and ``etag``.

            ``etag`` is the file as just written — pass it straight back as
            ``if_match`` on the next write to chain edits without an intervening
            read. It is ``None`` when the server did not return one (some
            proxies strip it). Re-read the file to obtain it in that case.
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
        # 201 Created for a new file (create-only / If-None-Match), 204 No
        # Content when an existing file was overwritten (If-Match).
        return WriteFileResponse(
            path=path,
            status_code=status_code,
            created=status_code == 201,
            size=len(content_bytes),
            etag=result.get("etag"),
        )

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
        source_path: str,
        destination_path: str,
        ctx: Context,
        overwrite: bool = False,
        if_destination_match: str | None = None,
    ) -> MoveResourceResponse:
        """Move or rename a file or directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and either
        the source or destination path (or one of their ancestor folders)
        carries an excluded system tag.

        Args:
            source_path: Full path of the file or directory to move
            destination_path: New path for the file or directory
            overwrite: Whether to overwrite the destination if it exists (default: False)
            if_destination_match: Optional ETag of the destination (from
                nc_webdav_read_file or nc_webdav_write_file). The move then
                replaces the destination only if it is still that exact version,
                so ``overwrite=True`` cannot clobber a file someone else changed
                in the meantime. Requires ``overwrite=True``. ``"*"`` is not
                accepted. Files only — a directory destination always fails the
                check with 412.

        Returns:
            ``MoveResourceResponse``. ``success`` is
            False for the known conflicts — 404 when the source does not
            exist, 412 when the destination exists and ``overwrite`` is
            False, 409 for a missing parent — with ``message`` explaining
            which. Other failures raise.
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

        result = await client.webdav.move_resource(
            source_path,
            destination_path,
            overwrite,
            if_destination_match=if_destination_match,
        )
        status_code = result.get("status_code")
        return MoveResourceResponse(
            success=status_code not in _WEBDAV_CONFLICT_STATUSES,
            status_code=status_code,
            message=result.get("message"),
            source_path=source_path,
            destination_path=destination_path,
            overwrite=overwrite,
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
        source_path: str,
        destination_path: str,
        ctx: Context,
        overwrite: bool = False,
        if_destination_match: str | None = None,
    ) -> CopyResourceResponse:
        """Copy a file or directory in NextCloud.

        Raises ``ToolError`` when ``EXCLUDED_TAGS`` is configured and either
        the source or destination path (or one of their ancestor folders)
        carries an excluded system tag.

        Args:
            source_path: Full path of the file or directory to copy
            destination_path: Destination path for the copy
            overwrite: Whether to overwrite the destination if it exists (default: False)
            if_destination_match: Optional ETag of the destination (from
                nc_webdav_read_file or nc_webdav_write_file). The copy then
                replaces the destination only if it is still that exact version,
                so ``overwrite=True`` cannot clobber a file someone else changed
                in the meantime. Requires ``overwrite=True``. ``"*"`` is not
                accepted. Files only — a directory destination always fails the
                check with 412.

        Returns:
            ``CopyResourceResponse``. ``success`` is
            False for the known conflicts — 404 when the source does not
            exist, 412 when the destination exists and ``overwrite`` is
            False, 409 for a missing parent — with ``message`` explaining
            which. Other failures raise.
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

        result = await client.webdav.copy_resource(
            source_path,
            destination_path,
            overwrite,
            if_destination_match=if_destination_match,
        )
        status_code = result.get("status_code")
        return CopyResourceResponse(
            success=status_code not in _WEBDAV_CONFLICT_STATUSES,
            status_code=status_code,
            message=result.get("message"),
            source_path=source_path,
            destination_path=destination_path,
            overwrite=overwrite,
        )

    @mcp.tool(
        title="Search Files",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @with_links
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
            conditions.append(like_predicate("d:displayname", name_pattern))

        if mime_type:
            conditions.append(like_predicate("d:getcontenttype", mime_type))

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
    @with_links
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
    @with_links
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
    @with_links
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

    @mcp.tool(
        title="List File Comments",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_comments(
        path: str, ctx: Context, limit: int = 20, offset: int = 0
    ) -> ListFileCommentsResponse:
        """Read the comments people have left on a file in NextCloud.

        Comments are how a team annotates a file in place -- review requests,
        hand-offs, context that does not belong in the file itself.

        Raises ``ToolError`` when the file does not exist, or when
        ``EXCLUDED_TAGS`` is configured and the file (or an ancestor folder)
        carries an excluded system tag.

        Args:
            path: Full path to the file (e.g. "/Documents/report.pdf")
            limit: Maximum number of comments to return (default: 20)
            offset: How many of the newest comments to skip, for paging

        Returns:
            ListFileCommentsResponse with the comments, newest first.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must not be negative, got {offset}")

        client = await get_client(ctx)
        file_id = await _resolve_commented_file(client, path)

        comments = await client.webdav.list_comments(
            file_id, limit=limit, offset=offset
        )
        return ListFileCommentsResponse(
            results=[FileComment(**comment) for comment in comments],
            count=len(comments),
            path=path,
            file_id=file_id,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(
        title="Comment on File",
        annotations=ToolAnnotations(
            idempotentHint=False,  # Each call adds another comment
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_create_comment(
        path: str, message: str, ctx: Context
    ) -> CreateFileCommentResponse:
        """Post a comment on a file in NextCloud.

        To notify someone, mention them by Nextcloud user ID: ``@username``, or
        ``@"user id with spaces"``. Nextcloud parses the mention out of the
        message when it stores the comment and sends the notification itself --
        nothing else is needed here.

        Raises ``ToolError`` when the file does not exist, or when
        ``EXCLUDED_TAGS`` is configured and the file (or an ancestor folder)
        carries an excluded system tag. Raises ``ValueError`` for a blank
        message, or one over Nextcloud's limit -- nothing is posted in either
        case.

        Args:
            path: Full path to the file to comment on
            message: The comment text (max 1000 characters, measured after
                trimming whitespace, counting Unicode code points)

        Returns:
            CreateFileCommentResponse with the new comment's ID.
        """
        if is_blank_comment(message):
            raise ValueError("Comment message must not be empty or whitespace-only")
        length = measured_length(message)
        if length > COMMENT_MAX_LENGTH:
            raise ValueError(
                f"Comment message is {length} characters; Nextcloud's limit is "
                f"{COMMENT_MAX_LENGTH} (measured after trimming whitespace, "
                f"counting Unicode code points). It is "
                f"{length - COMMENT_MAX_LENGTH} characters over. Nothing was "
                f"posted -- shorten it, or put the content in the file itself "
                f"and leave a short pointer comment."
            )

        client = await get_client(ctx)
        file_id = await _resolve_commented_file(client, path)

        comment_id = await client.webdav.create_comment(file_id, message)
        return CreateFileCommentResponse(
            path=path,
            file_id=file_id,
            comment_id=comment_id,
            message=message,
        )

    @mcp.tool(
        title="List Trash",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_trash(ctx: Context) -> ListTrashResponse:
        """List deleted files in the user's trash bin.

        Each entry carries the id needed to restore it, the location it was
        deleted from, and when it was deleted.
        """
        client = await get_client(ctx)
        items = await client.webdav.list_trash()

        # An excluded file stays excluded after deletion: the original
        # location would otherwise leak through the trash listing.
        excluded = await get_excluded_file_paths(client.webdav)
        if excluded:
            items = [
                item
                for item in items
                if not is_path_excluded(item.get("original_location") or "", excluded)
            ]

        entries = [
            TrashEntry(
                id=item["id"],
                name=item.get("trashbin_filename"),
                original_location=item.get("original_location"),
                deleted_at=_as_int(item.get("deleted_at")),
                size=_as_int(item.get("size")),
                href=item.get("href"),
            )
            for item in items
        ]
        return ListTrashResponse(items=entries, total_count=len(entries))

    @mcp.tool(
        title="Restore From Trash",
        annotations=ToolAnnotations(
            destructiveHint=False,  # Puts a file back; nothing is overwritten
            idempotentHint=False,  # The entry is gone from the trash afterwards
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_restore_from_trash(
        entry_id: str, ctx: Context
    ) -> RestoreFromTrashResponse:
        """Restore a deleted file from the trash bin to its original location.

        Args:
            entry_id: The id from nc_webdav_list_trash (not the file name).
        """
        client = await get_client(ctx)

        # Resolve the entry first so an excluded original location cannot be
        # restored -- and so a bad id fails as a refusal, not a DAV error.
        entries = await client.webdav.list_trash()
        match = next((e for e in entries if e.get("id") == entry_id), None)
        if match is None:
            raise ToolError(f"No trash entry with id {entry_id!r}")

        excluded = await get_excluded_file_paths(client.webdav)
        original = match.get("original_location") or ""
        if is_path_excluded(original, excluded):
            raise ToolError(
                f"Access denied: {original!r} is tagged with an excluded tag"
            )

        await client.webdav.restore_from_trash(entry_id)
        return RestoreFromTrashResponse(entry_id=entry_id)

    @mcp.tool(
        title="List File Versions",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_webdav_list_versions(path: str, ctx: Context) -> ListVersionsResponse:
        """List stored previous versions of a file.

        Args:
            path: Path to the file, relative to the user's files root.
        """
        client = await get_client(ctx)
        # Resolving through the shared helper applies the excluded-tag guard
        # and turns a missing file into a refusal rather than a ValueError
        # surfacing from the client layer. Passing the id through spares
        # list_versions a second get_fileid round-trip for the same path.
        file_id = await _resolve_commented_file(client, path)

        data = await client.webdav.list_versions(path, file_id=file_id)
        versions = [
            FileVersion(
                version_id=v["version_id"],
                size=_as_int(v.get("size")),
                modified=v.get("modified"),
                label=v.get("label"),
            )
            for v in data.get("versions", [])
        ]
        return ListVersionsResponse(
            path=data["path"],
            file_id=str(data["file_id"]),
            versions=versions,
            total_count=len(versions),
        )

    @mcp.tool(
        title="Restore File Version",
        annotations=ToolAnnotations(
            destructiveHint=False,  # The current content is kept as a version
            # Not idempotent: each restore stores the then-current content as
            # a further version, so repeating it keeps adding side effects.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @require_scopes("files.write")
    @instrument_tool
    async def nc_webdav_restore_version(
        path: str, version_id: str, ctx: Context
    ) -> RestoreVersionResponse:
        """Roll a file back to an earlier version.

        The current content is not lost: Nextcloud stores it as a version in
        turn, so the rollback itself can be undone.

        Args:
            path: Path to the file, relative to the user's files root.
            version_id: The version_id from nc_webdav_list_versions.
        """
        client = await get_client(ctx)
        # See nc_webdav_list_versions: passing the id through spares
        # restore_version a second get_fileid round-trip.
        file_id = await _resolve_commented_file(client, path)

        await client.webdav.restore_version(path, version_id, file_id=file_id)
        return RestoreVersionResponse(path=path, restored_version=version_id)
