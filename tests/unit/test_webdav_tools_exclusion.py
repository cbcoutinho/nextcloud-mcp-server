"""Server-layer regression tests for tag-based file exclusion (issue #710).

These tests register the WebDAV tools on a fresh ``MCPServer`` instance and
invoke each tool's underlying function directly via the tool registry.
Their purpose is **not** to re-test the path-matching logic (covered in
``test_tag_exclusion.py``) but to catch wiring regressions: that each
tool actually consults ``get_excluded_file_paths`` / ``is_path_excluded``
at the right point and raises / filters as expected.

The decorators on each tool (``@require_scopes``, ``@instrument_tool``) are
made transparent by the ``basicauth_mode`` fixture below, which pins the
deployment mode so these tests exercise path exclusion rather than auth.
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.models.webdav import WriteFileResponse
from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    These tests invoke tool functions directly, with no transport and so no
    verified token. Under any OAuth-style mode the decorator now (correctly)
    denies such a call, so the mode has to be explicit — otherwise the result
    depends on ambient environment: ``enable_login_flow`` is derived from
    ``MCP_DEPLOYMENT_MODE`` and defaults to **True** when no Nextcloud
    credentials are set, so these tests passed on a developer machine with
    ``NEXTCLOUD_USERNAME``/``PASSWORD`` exported and failed in CI without them.

    Patched narrowly on the scope_authorization module so the WebDAV tools'
    own ``get_settings()`` calls (size caps, exclusion tags) are untouched.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def webdav_tools() -> dict:
    """Register the WebDAV tools on a fresh MCPServer and return them by name."""
    mcp = MCPServer(name="test-webdav-tools")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def _mock_ctx(client) -> SimpleNamespace:
    """Build a minimal Context-shaped object for the tool decorators.

    With no auth contextvar set and OAuth mode off, ``require_scopes`` takes
    the BasicAuth pass-through branch (see scope_authorization.py). Note the
    token is read from the SDK ``auth_context`` contextvar, never from
    ``request_context`` — setting an ``access_token`` attribute here would
    have no effect on the decorator.
    """
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    ctx._client = client  # only used by tools that fetch via get_client(ctx)
    return ctx


@pytest.fixture
def patch_get_client(mocker):
    """Replace ``get_client`` in the webdav server module with a mock."""

    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_client",
            side_effect=fake_get_client,
        )

    return _install


@pytest.fixture
def patch_excluded(mocker):
    """Replace ``get_excluded_file_paths`` with a fixed return value."""

    def _install(excluded: set[str]):
        async def fake(*_, **__):
            return excluded

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
            side_effect=fake,
        )

    return _install


@pytest.fixture
def fake_client():
    """A NextcloudClient-shaped mock with an AsyncMock webdav attribute."""
    client = SimpleNamespace()
    client.webdav = AsyncMock()
    return client


def _spool(client, body: bytes, content_type: str, etag: str | None = None):
    """Make ``client.webdav.stream_to_file`` deliver ``body``.

    ``nc_webdav_read_file`` streams the document to a spool file rather than
    buffering it, so the fake has to write to the destination the tool chose and
    return the transport triple.
    """

    async def _stream_to_file(path, dest, *, max_bytes=None):
        await anyio.lowlevel.checkpoint()
        dest.write_bytes(body)
        return len(body), content_type, etag

    client.webdav.stream_to_file = AsyncMock(side_effect=_stream_to_file)


def _settings(**overrides) -> SimpleNamespace:
    """Settings the read path touches, with production defaults."""
    values = {
        "document_read_timeout_seconds": None,
        "document_spool_dir": None,
        "document_max_pdf_size_mb": 50.0,
        "document_markdown_max_pages": 150,
        "webdav_write_max_mb": 50.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(
    text="parsed text", metadata=None, processor="pypdfium2_fast", success=True
):
    from nextcloud_mcp_server.document_processors import ProcessingResult

    return ProcessingResult(
        text=text,
        metadata=metadata if metadata is not None else {},
        processor=processor,
        success=success,
    )


@pytest.fixture
def parsing(mocker):
    """Drive the read tool's parse branch with a canned ProcessingResult.

    Returns the ``parse_document_source`` mock so a test can assert on how the
    pipeline was invoked (e.g. that ``prefer_markdown`` was threaded through).
    """

    def _install(result=None, *, parseable=True, side_effect=None, settings=None):
        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_settings",
            return_value=settings or _settings(),
        )
        mocker.patch(
            "nextcloud_mcp_server.utils.document_parser.is_parseable_document",
            return_value=parseable,
        )
        return mocker.patch(
            "nextcloud_mcp_server.utils.document_parser.parse_document_source",
            side_effect=side_effect,
            return_value=result,
        )

    return _install


# ── Read / mutate guards ────────────────────────────────────────────────


async def test_read_file_raises_when_path_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_read_file"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Secret.txt", ctx=_mock_ctx(fake_client))

    fake_client.webdav.stream_to_file.assert_not_called()


async def test_read_file_passes_through_when_not_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    parsing(parseable=False)
    _spool(fake_client, b"hello", "text/plain", "abc123")

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/Public/notes.md", ctx=_mock_ctx(fake_client))

    assert result.content == "hello"
    assert result.parse_status == "not_applicable"
    assert fake_client.webdav.stream_to_file.await_args.args[0] == "/Public/notes.md"


async def test_write_file_raises_when_path_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_write_file"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(
            path="/Private/note.md",
            content="hi",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.write_file.assert_not_called()


async def test_delete_resource_raises_when_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_delete_resource"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Secret.txt", ctx=_mock_ctx(fake_client))

    fake_client.webdav.delete_resource.assert_not_called()


async def test_create_directory_raises_when_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_create_directory"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(path="/Private/sub", ctx=_mock_ctx(fake_client))

    fake_client.webdav.create_directory.assert_not_called()


async def test_move_resource_blocks_excluded_source(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_move_resource"].fn
    with pytest.raises(ToolError, match="source"):
        await fn(
            source_path="/Secret.txt",
            destination_path="/Public/x.txt",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.move_resource.assert_not_called()


async def test_move_resource_blocks_excluded_destination_exact_match(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """Destination check must trip on an *exact* match, not just a prefix.

    Regression guard for review #764: previously the message said "is
    inside" but is_path_excluded also matches exact paths.
    """
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_move_resource"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(
            source_path="/Public/x.txt",
            destination_path="/Private",
            ctx=_mock_ctx(fake_client),
        )


async def test_copy_resource_blocks_excluded_source(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_copy_resource"].fn
    with pytest.raises(ToolError, match="source"):
        await fn(
            source_path="/Secret.txt",
            destination_path="/Public/copy.txt",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.copy_resource.assert_not_called()


async def test_copy_resource_blocks_excluded_destination_descendant(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_copy_resource"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(
            source_path="/Public/x.txt",
            destination_path="/Private/copy.txt",
            ctx=_mock_ctx(fake_client),
        )


# ── Listing / search filtering ──────────────────────────────────────────


async def test_list_directory_filters_excluded_children(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Public/Secret.txt"})
    fake_client.webdav.list_directory = AsyncMock(
        return_value=[
            {
                "path": "/Public/Secret.txt",
                "name": "Secret.txt",
                "is_directory": False,
            },
            {
                "path": "/Public/visible.md",
                "name": "visible.md",
                "is_directory": False,
            },
        ]
    )

    fn = webdav_tools["nc_webdav_list_directory"].fn
    result = await fn(path="/Public", ctx=_mock_ctx(fake_client))

    assert [f.path for f in result.files] == ["/Public/visible.md"]


async def test_list_directory_surfaces_etag_on_fileinfo(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """The etag the client parses from PROPFIND must reach the MCP tool's
    FileInfo, so a caller can obtain one for write_file's if_match from a
    listing (wire-through of the client dict into FileInfo(**result))."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.list_directory = AsyncMock(
        return_value=[
            {
                "path": "/Public/notes.md",
                "name": "notes.md",
                "is_directory": False,
                "etag": "abc123",
            },
        ]
    )

    fn = webdav_tools["nc_webdav_list_directory"].fn
    result = await fn(path="/Public", ctx=_mock_ctx(fake_client))

    assert result.files[0].etag == "abc123"


async def test_list_directory_raises_when_listed_path_itself_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """The early guard prevents the round-trip to Nextcloud and signals
    the access denial, instead of silently returning an empty listing
    (review #764)."""
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_list_directory"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.list_directory.assert_not_called()


async def test_search_files_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.search_files = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/notes.md", "name": "notes.md", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_search_files"].fn
    result = await fn(ctx=_mock_ctx(fake_client), name_pattern="%.%")

    assert [r.path for r in result.results] == ["/notes.md"]


async def test_find_by_name_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.find_by_name = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_find_by_name"].fn
    result = await fn(pattern="%.txt", ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


async def test_find_by_type_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.find_by_type = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_find_by_type"].fn
    result = await fn(mime_type="text/plain", ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


async def test_list_favorites_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.list_favorites = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_list_favorites"].fn
    result = await fn(ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


# ── Search-tool scope guards (review #764) ──────────────────────────────


async def test_search_files_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """Mirror the ``list_directory`` early guard so the four search tools
    cannot silently return an empty result for an excluded ``scope``."""
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_search_files"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(ctx=_mock_ctx(fake_client), scope="/Private", name_pattern="%.txt")

    fake_client.webdav.search_files.assert_not_called()


async def test_find_by_name_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_find_by_name"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(pattern="%.txt", scope="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.find_by_name.assert_not_called()


async def test_find_by_type_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_find_by_type"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(mime_type="text/plain", scope="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.find_by_type.assert_not_called()


async def test_list_favorites_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_list_favorites"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(ctx=_mock_ctx(fake_client), scope="/Private")

    fake_client.webdav.list_favorites.assert_not_called()


# ── Document reads: parse_document + honest degradation (Deck #894) ─────


def _read_ctx(fake_client) -> SimpleNamespace:
    ctx = _mock_ctx(fake_client)
    ctx.report_progress = AsyncMock()
    return ctx


async def test_read_file_interactive_cap_returns_the_raw_file(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """With DOCUMENT_READ_TIMEOUT_SECONDS set, a slow synchronous parse is aborted
    at the cap and the tool returns the file fast instead of blocking past the MCP
    client's own timeout (ADR-032) -- saying so, rather than silently."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"\x89PNG", "image/png")

    async def slow_parse(*_a, **_k):
        await anyio.sleep(5)  # far beyond the 0.05s cap; fail_after cancels it

    parsing(
        side_effect=slow_parse,
        settings=_settings(document_read_timeout_seconds=0.05),
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/scan.png", ctx=_read_ctx(fake_client))

    assert result.encoding == "base64"
    assert result.content == base64.b64encode(b"\x89PNG").decode("ascii")
    assert result.parsed is False
    assert result.parse_status == "failed"
    assert any("DOCUMENT_READ_TIMEOUT_SECONDS" in n for n in result.parse_notes)


async def test_read_file_defaults_to_parsing(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """The default reads a document as text; the caller does not have to ask."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf", "etag-1")
    parse = parsing(
        _result(metadata={"pipeline_tier": "fast", "parse_mode": "text_only"})
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/doc.pdf", ctx=_read_ctx(fake_client))

    parse.assert_awaited_once()
    assert result.parsed is True
    assert result.parse_status == "parsed"
    assert result.content == "parsed text"
    assert result.parse_tier == "fast"
    assert result.parse_processor == "pypdfium2_fast"
    assert result.content_format == "text"
    assert result.parse_notes == []
    assert result.etag == "etag-1"


async def test_read_file_raw_skips_the_parse_entirely(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """``parse_document="raw"`` must reach the bytes without touching the
    pipeline -- and must not be shadowed by the same-named lazy import."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parse = parsing(_result())

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/doc.pdf", ctx=_read_ctx(fake_client), parse_document="raw")

    parse.assert_not_called()
    assert result.parse_status == "skipped"
    assert result.parsed is False
    assert result.encoding == "base64"
    assert result.content_format == "base64"


async def test_read_file_markdown_asks_the_pipeline_for_structure(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parse = parsing(
        _result(
            text="# Heading",
            metadata={"pipeline_tier": "structured", "parse_mode": "markdown"},
            processor="pymupdf",
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(
        path="/doc.pdf", ctx=_read_ctx(fake_client), parse_document="markdown"
    )

    assert parse.await_args.kwargs["prefer_markdown"] is True
    assert result.content_format == "markdown"
    assert result.parse_tier == "structured"


async def test_read_file_reports_markdown_page_ceiling(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """A document past the ceiling gets text, and is told it got text."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(
        _result(
            metadata={
                "pipeline_tier": "fast",
                "parse_mode": "text_only",
                "markdown_skipped_reason": "page_ceiling",
                "page_count": 412,
            }
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(
        path="/big.pdf", ctx=_read_ctx(fake_client), parse_document="markdown"
    )

    assert result.content_format == "text"
    note = " ".join(result.parse_notes)
    assert "412 pages" in note
    assert "DOCUMENT_MARKDOWN_MAX_PAGES=150" in note


async def test_read_file_auto_mode_says_nothing_about_markdown(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """The same metadata as the test above, but the caller asked for text.

    The structured tier stamps ``markdown_skipped_reason`` whenever it runs past
    the ceiling -- including when it was reached to recover a corrupt text layer
    in auto mode. Reporting that would be a false alarm, and would invite a
    pointless re-request for markdown that hits the identical ceiling.
    """
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(
        _result(
            metadata={
                "pipeline_tier": "structured",
                "parse_mode": "text_only",
                "markdown_skipped_reason": "page_ceiling",
                "page_count": 412,
            },
            processor="pymupdf",
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/big.pdf", ctx=_read_ctx(fake_client))

    assert result.parse_status == "parsed"
    assert result.content_format == "text"
    assert result.parse_notes == []


async def test_read_file_reports_ocr_unavailable(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """A scan read on a tenant without OCR must not look like an empty document."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(
        _result(
            text="",
            metadata={
                "pipeline_tier": "fast",
                "parse_mode": "text_only",
                "ocr_escalation_skipped": "disabled",
                "ocr_recommended_reason": "empty_text",
            },
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/scan.pdf", ctx=_read_ctx(fake_client))

    assert result.parse_status == "parsed"
    note = " ".join(result.parse_notes)
    assert "DOCUMENT_OCR_ENABLED" in note
    assert "0 characters" in note


async def test_read_file_failed_parse_is_not_reported_as_parsed(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """Regression: a failed parse used to return empty content with parsed=True."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(
        _result(
            text="",
            metadata={"pipeline_tier": "structured", "parse_failed_reason": "timeout"},
            processor="pymupdf",
            success=False,
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/doc.pdf", ctx=_read_ctx(fake_client))

    assert result.parsed is False
    assert result.parse_status == "failed"
    assert result.parse_tier == "structured"
    assert any("timeout" in n for n in result.parse_notes)
    # The file itself is still available to the caller.
    assert result.encoding == "base64"


async def test_read_file_reports_the_size_cap(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(
        _result(
            text="",
            metadata={"parse_failed_reason": "oversize"},
            processor="size_guard",
            success=False,
        )
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/huge.pdf", ctx=_read_ctx(fake_client))

    assert result.parse_status == "failed"
    assert result.parse_tier is None
    assert any("DOCUMENT_MAX_PDF_SIZE_MB" in n for n in result.parse_notes)


async def test_read_file_will_not_inline_a_huge_unparsed_file(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing, mocker
):
    """Base64 of a large binary is unusable by the time it reaches a model, so the
    read reports the file rather than burying the response in it."""
    patch_get_client(fake_client)
    patch_excluded(set())
    parsing(parseable=False)
    mocker.patch("nextcloud_mcp_server.server.webdav.RAW_CONTENT_MAX_BYTES", 16)
    _spool(fake_client, b"x" * 64, "application/zip")

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/big.zip", ctx=_read_ctx(fake_client))

    assert result.content == ""
    assert result.size == 64
    assert any("too large to return inline" in n for n in result.parse_notes)


async def test_read_file_rejects_a_download_past_the_transfer_ceiling(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """An aborted transfer leaves nothing to describe, so it is an error rather
    than an empty response that reads like an empty document."""
    from nextcloud_mcp_server.client.webdav import OversizeDownload

    patch_get_client(fake_client)
    patch_excluded(set())
    parsing(parseable=False)
    fake_client.webdav.stream_to_file = AsyncMock(
        side_effect=OversizeDownload("aborted after 104857601")
    )

    fn = webdav_tools["nc_webdav_read_file"].fn
    ctx = _read_ctx(fake_client)
    with pytest.raises(ToolError, match="transfer ceiling"):
        await fn(path="/enormous.pdf", ctx=ctx)


async def test_read_file_schema_replaces_force_processor(webdav_tools):
    """The breaking change, in machine-checkable form (Deck #894)."""
    properties = webdav_tools["nc_webdav_read_file"].parameters["properties"]

    assert "parse_document" in properties
    assert "force_processor" not in properties


async def test_read_file_streams_instead_of_buffering(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    """The API role cannot hold a large document in memory, so the read must go
    through the streaming spool -- never the buffered ``read_file``."""
    patch_get_client(fake_client)
    patch_excluded(set())
    _spool(fake_client, b"%PDF-1.7", "application/pdf")
    parsing(_result(metadata={"pipeline_tier": "fast"}))

    fn = webdav_tools["nc_webdav_read_file"].fn
    await fn(path="/doc.pdf", ctx=_read_ctx(fake_client))

    fake_client.webdav.read_file.assert_not_called()
    # Bounded by the same ceiling ingest streams under (2x the parse cap).
    assert fake_client.webdav.stream_to_file.await_args.kwargs["max_bytes"] == int(
        50.0 * 1024 * 1024 * 2
    )


# ── Write conflict handling (etag / lock) and size gate ─────────────────


async def test_read_file_includes_etag_in_response(
    webdav_tools, fake_client, patch_get_client, patch_excluded, parsing
):
    patch_get_client(fake_client)
    patch_excluded(set())
    parsing(parseable=False)
    _spool(fake_client, b"hello", "text/plain", "abc123")

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/Public/notes.md", ctx=_read_ctx(fake_client))

    assert result.etag == "abc123"


async def test_write_file_passes_if_match_through_to_client(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(return_value={"status_code": 204})
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    result = await fn(
        path="/Public/notes.md",
        content="hi",
        ctx=_mock_ctx(fake_client),
        if_match="abc123",
    )

    fake_client.webdav.write_file.assert_awaited_once_with(
        "/Public/notes.md", b"hi", None, if_match="abc123"
    )
    # A 204 (overwrite of an existing file) is reported as created=False on the
    # typed WriteFileResponse; size is the decoded byte count.
    assert isinstance(result, WriteFileResponse)
    assert result.status_code == 204
    assert result.created is False
    assert result.size == 2
    assert result.success is True


async def test_write_file_create_only_success_returns_created(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """A create-only write (no if_match) that the server answers 201 is reported
    as created=True on the WriteFileResponse -- the overwrite path (204) above
    is created=False."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(return_value={"status_code": 201})
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    result = await fn(path="/Public/new.md", content="hi", ctx=_mock_ctx(fake_client))

    assert isinstance(result, WriteFileResponse)
    assert result.status_code == 201
    assert result.created is True
    assert result.path == "/Public/new.md"


async def test_write_file_passes_force_star_through_to_client(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """if_match='*' (explicit force-overwrite) reaches the client unchanged so
    it becomes a bare If-Match: * PUT."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(return_value={"status_code": 204})
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    await fn(
        path="/Public/notes.md",
        content="hi",
        ctx=_mock_ctx(fake_client),
        if_match="*",
    )

    fake_client.webdav.write_file.assert_awaited_once_with(
        "/Public/notes.md", b"hi", None, if_match="*"
    )


async def test_write_file_raises_toolerror_when_file_already_exists(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """The fail-closed default: an if_match-less write over an existing file
    (client returns the 'already exists' 412) surfaces as an actionable
    ToolError telling the caller to read first or pass if_match='*'."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(
        return_value={
            "status_code": 412,
            "message": "File already exists — read it first to get its etag and "
            "pass if_match to overwrite safely, or pass if_match='*' to "
            "overwrite deliberately",
        }
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    ctx = _mock_ctx(fake_client)
    with pytest.raises(ToolError, match="already exists"):
        await fn(path="/Public/notes.md", content="hi", ctx=ctx)


async def test_write_file_raises_toolerror_on_precondition_failed(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """A 412 from the client (concurrent edit since if_match was read) must
    surface as a clear, actionable ToolError -- not a silently-returned dict
    a caller might not check, and not a raw transport exception."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(
        return_value={
            "status_code": 412,
            "message": "File was modified since the given etag was read",
        }
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    ctx = _mock_ctx(fake_client)
    with pytest.raises(ToolError, match="modified since"):
        await fn(
            path="/Public/notes.md",
            content="hi",
            ctx=ctx,
            if_match="stale",
        )


async def test_write_file_raises_toolerror_on_locked(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """A 423 (locked, e.g. open in the Nextcloud web editor) must surface as a
    ToolError so the caller stops and reports it rather than retrying."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(
        return_value={
            "status_code": 423,
            "message": "File is locked by another client",
        }
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=50.0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    ctx = _mock_ctx(fake_client)
    with pytest.raises(ToolError, match="locked"):
        await fn(path="/Public/notes.md", content="hi", ctx=ctx)


async def test_write_file_raises_toolerror_when_content_exceeds_configured_max_size(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """A pre-flight size gate fails fast with a clear error instead of
    attempting a single-shot PUT that risks a timeout or OOM."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(return_value={"status_code": 204})
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=0.000001),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    ctx = _mock_ctx(fake_client)
    with pytest.raises(ToolError, match="WEBDAV_WRITE_MAX_MB"):
        await fn(path="/Public/notes.md", content="hi", ctx=ctx)

    fake_client.webdav.write_file.assert_not_called()


async def test_write_file_size_gate_disabled_when_max_mb_is_zero(
    webdav_tools, fake_client, patch_get_client, patch_excluded, mocker
):
    """0 (falsy) disables the guard entirely, matching document_max_pdf_size_mb's
    existing "0 disables" convention."""
    patch_get_client(fake_client)
    patch_excluded(set())
    fake_client.webdav.write_file = AsyncMock(return_value={"status_code": 204})
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=0),
    )

    fn = webdav_tools["nc_webdav_write_file"].fn
    await fn(path="/Public/notes.md", content="hi", ctx=_mock_ctx(fake_client))

    fake_client.webdav.write_file.assert_awaited_once()


# ============= Typed responses for move/copy =============
#
# Both tools previously returned the client's raw dict with no return
# annotation, which the CLAUDE.md response-pattern gate treats as a defect: raw
# dicts bypass the success/timestamp envelope every other tool provides.


@pytest.mark.parametrize(
    "tool_name,verb",
    [("nc_webdav_move_resource", "move"), ("nc_webdav_copy_resource", "copy")],
)
async def test_move_copy_return_typed_success(
    webdav_tools, fake_client, patch_get_client, patch_excluded, tool_name, verb
):
    patch_get_client(fake_client)
    patch_excluded(set())
    getattr(fake_client.webdav, f"{verb}_resource").return_value = {"status_code": 201}

    result = await webdav_tools[tool_name].fn(
        source_path="/a.txt",
        destination_path="/b.txt",
        ctx=_mock_ctx(fake_client),
        overwrite=False,
    )

    assert result.success is True
    assert result.status_code == 201
    assert result.source_path == "/a.txt"
    assert result.destination_path == "/b.txt"
    assert result.overwrite is False


@pytest.mark.parametrize(
    "tool_name,verb",
    [("nc_webdav_move_resource", "move"), ("nc_webdav_copy_resource", "copy")],
)
@pytest.mark.parametrize("status", [404, 409, 412])
async def test_move_copy_report_conflicts_as_unsuccessful(
    webdav_tools, fake_client, patch_get_client, patch_excluded, tool_name, verb, status
):
    """The client returns rather than raises on these, so the typed response has
    to carry the failure — otherwise a 412 would arrive inside a success
    envelope."""
    patch_get_client(fake_client)
    patch_excluded(set())
    getattr(fake_client.webdav, f"{verb}_resource").return_value = {
        "status_code": status,
        "message": "nope",
    }

    result = await webdav_tools[tool_name].fn(
        source_path="/a.txt",
        destination_path="/b.txt",
        ctx=_mock_ctx(fake_client),
        overwrite=False,
    )

    assert result.success is False
    assert result.status_code == status
    assert result.message == "nope"
