"""Unit tests for the OCR tier processor + backend selection."""

from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from nextcloud_mcp_server.document_processors import ocr
from nextcloud_mcp_server.document_processors.base import ProcessorError
from nextcloud_mcp_server.embedding.gateway_batch_client import BatchPollResult
from nextcloud_mcp_server.vector import batch_ocr_store as _bos

pytestmark = pytest.mark.unit


def _settings(**kw) -> Any:  # a Settings stand-in (only the read fields matter)
    base = dict(
        document_ocr_provider="auto",
        document_ocr_model="mistral/mistral-ocr-latest",
        document_ocr_timeout_seconds=180.0,
        document_ocr_mode="sync",
        document_ocr_batch_poll_seconds=120,
        document_ocr_batch_max_wait_seconds=86400,
        embedding_gateway_url=None,
        embedding_gateway_client_id=None,
        embedding_gateway_client_secret=None,
        embedding_gateway_token_url=None,
        embedding_gateway_scope=None,
        mistral_api_key=None,
        mistral_base_url=None,
        docling_api_url=None,
        docling_ocr_lang="en,de",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- _pages_to_text ----------------------------------------------------------


def test_pages_to_text_orders_and_exact_boundaries():
    text, boundaries, block_spans = ocr._pages_to_text(
        [(1, "B"), (0, "A")]
    )  # unordered
    assert text == "A\n\nB"
    assert boundaries[0] == {"page": 1, "start_offset": 0, "end_offset": 1}
    assert boundaries[1]["page"] == 2
    # contiguous + offsets index exactly into the text
    assert boundaries[0]["end_offset"] <= boundaries[1]["start_offset"]
    assert boundaries[-1]["end_offset"] == len(text)
    # No layout blocks supplied -> no block spans.
    assert block_spans == []


def test_pages_to_text_computes_block_spans_from_blocks():
    """With surya-style blocks, each block's stripped-html text is located in the
    page markdown and paired with its normalized bbox as a doc-absolute char span."""
    pages = [
        (
            0,
            "Invoice Summary\n\nTotal due: 42.00 USD",
            [
                {"html": "<h1>Invoice Summary</h1>", "bbox": [0.1, 0.1, 0.4, 0.13]},
                {
                    "html": "<p>Total due: 42.00 USD</p>",
                    "bbox": [0.1, 0.16, 0.31, 0.18],
                },
            ],
        ),
        (
            1,
            "Terms",
            [{"html": "<h1>Terms</h1>", "bbox": [0.1, 0.1, 0.2, 0.12]}],
        ),
    ]
    text, boundaries, spans = ocr._pages_to_text(pages)
    assert len(spans) == 3
    # First block span maps exactly onto "Invoice Summary" at the doc start.
    s0 = spans[0]
    assert s0["page"] == 1 and s0["bbox"] == [0.1, 0.1, 0.4, 0.13]
    assert text[s0["start_offset"] : s0["end_offset"]] == "Invoice Summary"
    # Second block onto the page-1 body text.
    s1 = spans[1]
    assert text[s1["start_offset"] : s1["end_offset"]] == "Total due: 42.00 USD"
    # Page-2 block span lands on page 2's text.
    s2 = spans[2]
    assert s2["page"] == 2 and text[s2["start_offset"] : s2["end_offset"]] == "Terms"


def test_pages_to_text_matches_blocks_ignoring_whitespace():
    """A block whose tag-stripped text fuses tokens across ``<br/>`` (no separator),
    while the page markdown renders those breaks as spaces, is still located and
    given a span — a verbatim find would drop it. This is the Student 147 page-15
    regression (the FLASHMAN'S passage), where ~40% of blocks lost their bbox and
    highlights came back disconnected.

    The span must cover the full sentence region in the markdown."""
    markdown = (
        "YEAR 11 LEADERS AWARD\n\n"
        'I am making a recommendation that Louis receives the "FLASHMAN\'S AWARD".'
    )
    body_html = (
        "<p>I am making a recommendation<br/>that Louis receives the<br/>"
        '"FLASHMAN\'S AWARD".</p>'
    )
    # Precondition: the fused stripped text is NOT a verbatim substring (the bug).
    assert ocr._strip_html(body_html) not in markdown
    pages = [
        (
            0,
            markdown,
            [
                {
                    "html": "<h2>YEAR 11 LEADERS AWARD</h2>",
                    "bbox": [0.1, 0.02, 0.9, 0.07],
                },
                {"html": body_html, "bbox": [0.1, 0.1, 1.0, 0.24]},
            ],
        )
    ]
    text, _b, spans = ocr._pages_to_text(pages)
    # Both blocks now get spans (pre-fix: only the heading matched verbatim).
    assert len(spans) == 2
    assert (
        text[spans[0]["start_offset"] : spans[0]["end_offset"]]
        == "YEAR 11 LEADERS AWARD"
    )
    # The body span maps back onto the real (whitespace-bearing) markdown sentence.
    assert text[spans[1]["start_offset"] : spans[1]["end_offset"]] == (
        'I am making a recommendation that Louis receives the "FLASHMAN\'S AWARD".'
    )
    assert spans[1]["bbox"] == [0.1, 0.1, 1.0, 0.24]


def test_ws_index_map_strips_whitespace_and_maps_indices():
    """The projection drops whitespace; the index list maps each kept char back to
    its original position, so a normalized hit resolves to a real offset."""
    s, idx = ocr._ws_index_map("hello world")
    assert s == "helloworld"
    assert idx == [0, 1, 2, 3, 4, 6, 7, 8, 9, 10]
    # Leading/trailing spaces, tabs and newlines are all dropped; indices stay
    # aligned so idx can be used to slice the original string back out.
    original = "  a\tb\nc "
    s2, idx2 = ocr._ws_index_map(original)
    assert s2 == "abc"
    assert idx2 == [2, 4, 6]
    assert "".join(original[i] for i in idx2) == "abc"


def test_pages_to_text_skips_blocks_without_bbox_or_unmatched_text():
    """A block with no bbox, or whose text isn't in the markdown, is skipped
    (that region falls back to pymupdf) rather than guessed."""
    pages = [
        (
            0,
            "real text here",
            [
                {"html": "<p>real text here</p>"},  # no bbox -> skip
                {
                    "html": "<p>not in markdown</p>",
                    "bbox": [0.1, 0.1, 0.2, 0.2],
                },  # unmatched
            ],
        )
    ]
    _text, _b, spans = ocr._pages_to_text(pages)
    assert spans == []


def test_normalize_bbox_shape_and_range():
    """Valid normalized bbox passes; wrong arity, non-numbers, bools, and
    out-of-range (unnormalized/pixel) coords all degrade to None (pymupdf fallback)."""
    assert ocr._normalize_bbox([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]
    assert ocr._normalize_bbox([0.0, 0.0, 1.0, 1.0]) == [0.0, 0.0, 1.0, 1.0]
    assert ocr._normalize_bbox([0.1, 0.2, 0.3]) is None  # wrong arity
    assert ocr._normalize_bbox([0.1, 0.2, 0.3, "x"]) is None  # non-number
    assert ocr._normalize_bbox([True, 0.2, 0.3, 0.4]) is None  # bool excluded
    # Unnormalized pixel coords (gateway contract drift) are dropped, not stored.
    assert ocr._normalize_bbox([0.0, 800.0, 1200.0, 1600.0]) is None
    assert ocr._normalize_bbox([-0.1, 0.2, 0.3, 0.4]) is None
    # Degenerate zero/negative-area boxes (invisible highlight) are dropped.
    assert ocr._normalize_bbox([0.0, 0.0, 0.0, 0.0]) is None
    assert ocr._normalize_bbox([0.5, 0.5, 0.5, 0.6]) is None  # zero width
    assert ocr._normalize_bbox([0.4, 0.5, 0.3, 0.6]) is None  # x1 < x0


@pytest.mark.parametrize(
    "html, expected",
    [
        ("<h1>Heading</h1>", "Heading"),
        ("<p>Total due: 42.00 USD</p>", "Total due: 42.00 USD"),
        ("<p>a <b>bold</b> word</p>", "a bold word"),  # nested tags stripped
        ("AT&amp;T &lt;x&gt;", "AT&T <x>"),  # entities unescaped
        ("  <p>  trim  </p>  ", "trim"),  # outer whitespace stripped
        ("", ""),
    ],
)
def test_strip_html(html, expected):
    assert ocr._strip_html(html) == expected


def test_pages_to_text_drops_unnormalized_block_bbox():
    """A block whose bbox is out of [0,1] (unnormalized) yields no span — the
    page falls back to pymupdf rather than storing off-page geometry."""
    pages = [(0, "Heading", [{"html": "<h1>Heading</h1>", "bbox": [0, 100, 500, 200]}])]
    _text, _b, spans = ocr._pages_to_text(pages)
    assert spans == []


# --- backend selection -------------------------------------------------------


def test_build_backend_none():
    assert ocr.build_ocr_backend(_settings(document_ocr_provider="none")) is None


def test_build_backend_gateway():
    b = ocr.build_ocr_backend(
        _settings(document_ocr_provider="gateway", embedding_gateway_url="https://gw")
    )
    assert isinstance(b, ocr._GatewayOcrBackend)


def test_build_backend_mistral():
    b = ocr.build_ocr_backend(
        _settings(document_ocr_provider="mistral", mistral_api_key="k")
    )
    assert isinstance(b, ocr._MistralOcrBackend)


def test_build_backend_auto_prefers_gateway():
    b = ocr.build_ocr_backend(
        _settings(embedding_gateway_url="https://gw", mistral_api_key="k")
    )
    assert isinstance(b, ocr._GatewayOcrBackend)


def test_build_backend_auto_none_configured():
    assert ocr.build_ocr_backend(_settings()) is None


def test_build_backend_docling():
    b = ocr.build_ocr_backend(
        _settings(
            document_ocr_provider="docling", docling_api_url="https://docling:5001"
        )
    )
    assert isinstance(b, ocr._DoclingServeBackend)
    assert b._api_url == "https://docling:5001"
    assert b._ocr_lang == ["en", "de"]


def test_build_backend_docling_missing_url():
    # Explicit docling without a URL -> None (warned), never a live backend.
    assert ocr.build_ocr_backend(_settings(document_ocr_provider="docling")) is None


def test_build_backend_auto_never_selects_docling():
    # "auto" must never pick docling even with a URL present -- docling needs an
    # explicit DOCUMENT_OCR_PROVIDER=docling (a self-hosted URL auto can't presume).
    assert (
        ocr.build_ocr_backend(
            _settings(
                document_ocr_provider="auto", docling_api_url="https://docling:5001"
            )
        )
        is None
    )


async def test_docling_backend_builds_page_boundaries(monkeypatch):
    """The docling OCR backend groups DoclingDocument.texts by page provenance into
    contiguous page_boundaries that index exactly into the returned text."""
    from nextcloud_mcp_server.document_processors import docling_serve

    async def _fake_convert(api_url, content, content_type, **kw):
        assert kw["to_formats"] == ["md", "json"]
        return {
            "md_content": "ignored-flat",
            "json_content": {
                "texts": [
                    {"text": "Page one", "prov": [{"page_no": 1}]},
                    {"text": "Page two", "prov": [{"page_no": 2}]},
                ]
            },
        }

    monkeypatch.setattr(docling_serve, "convert_file", _fake_convert)
    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())

    backend = ocr._DoclingServeBackend("https://docling:5001", ["en", "de"])
    text, boundaries, spans = await backend.ocr(b"%PDF-1.7", "application/pdf")
    assert text == "Page one\n\nPage two"
    assert [b["page"] for b in boundaries] == [1, 2]
    assert boundaries[-1]["end_offset"] == len(text)
    # docling has no normalized [0,1] block bbox contract -> no block spans.
    assert spans == []


async def test_docling_backend_single_page_fallback(monkeypatch):
    """With no per-page provenance, the backend falls back to one whole-text page
    (still satisfying end_offset == len(text))."""
    from nextcloud_mcp_server.document_processors import docling_serve

    async def _fake_convert(api_url, content, content_type, **kw):
        return {"md_content": "whole doc text", "json_content": {"texts": []}}

    monkeypatch.setattr(docling_serve, "convert_file", _fake_convert)
    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())

    backend = ocr._DoclingServeBackend("https://docling:5001", None)
    text, boundaries, spans = await backend.ocr(b"%PDF", "application/pdf")
    assert text == "whole doc text"
    assert len(boundaries) == 1
    assert boundaries[0]["end_offset"] == len(text)
    assert spans == []


@pytest.mark.parametrize(
    "kw, expect_client",
    [
        # No gateway URL -> no batch path from the pod.
        (dict(document_ocr_provider="mistral", mistral_api_key="k"), False),
        (dict(document_ocr_provider="gateway"), False),
        # Gateway configured -> a batch client regardless of the sync provider:
        # batch routes through the gateway even for the direct mistral backend
        # (we leverage the gateway to batch for backends without native batch).
        (
            dict(document_ocr_provider="gateway", embedding_gateway_url="https://gw"),
            True,
        ),
        (dict(document_ocr_provider="auto", embedding_gateway_url="https://gw"), True),
        (
            dict(
                document_ocr_provider="mistral",
                mistral_api_key="k",
                embedding_gateway_url="https://gw",
            ),
            True,
        ),
        # OCR disabled entirely -> never a batch client.
        (dict(document_ocr_provider="none", embedding_gateway_url="https://gw"), False),
    ],
)
def test_build_gateway_batch_client(kw, expect_client):
    client = ocr.build_gateway_batch_client(_settings(**kw))
    assert (client is not None) is expect_client


# --- model id handling (provider-namespaced, configurable) ---------------------


def test_build_backend_empty_model_does_not_fall_back():
    """An empty model string must NOT silently fall back to the configured default
    (an `or` would); the backend keeps the empty id it was handed."""
    b = ocr.build_ocr_backend(
        _settings(document_ocr_provider="gateway", embedding_gateway_url="https://gw"),
        model="",
    )
    assert isinstance(b, ocr._GatewayOcrBackend)
    assert b._model == ""


def test_build_backend_model_override_is_not_hardcoded():
    """The OCR model is whatever config passes -- mistral/surya by default, but
    fully swappable (e.g. lightonocr) with no code change."""
    surya = ocr.build_ocr_backend(
        _settings(document_ocr_provider="gateway", embedding_gateway_url="https://gw"),
        model="surya/surya-ocr-2",
    )
    assert isinstance(surya, ocr._GatewayOcrBackend)
    assert surya._model == "surya/surya-ocr-2"
    lit = ocr.build_ocr_backend(
        _settings(document_ocr_provider="gateway", embedding_gateway_url="https://gw"),
        model="lightonocr/lightonocr-1b",
    )
    assert isinstance(lit, ocr._GatewayOcrBackend)
    assert lit._model == "lightonocr/lightonocr-1b"  # config-driven, not hardcoded


def test_no_surya_string_literal_in_document_processors():
    """surya must be a CONFIG default only -- never a hard-coded behavioural literal
    in the worker (it's swappable, e.g. lightonocr). Comments may mention it; code
    string literals may not (the default lives in config.py, a different module)."""
    import pathlib  # noqa: PLC0415

    assert ocr.__file__ is not None
    pkg = pathlib.Path(ocr.__file__).parent
    offenders = [
        f"{p.name}: {ln.strip()}"
        for p in pkg.rglob("*.py")  # recurse: future backends/ subdirs too
        for ln in p.read_text().splitlines()
        if '"surya' in ln.split("#", 1)[0] or "'surya" in ln.split("#", 1)[0]
    ]
    assert not offenders, offenders


def test_build_backend_gateway_missing_m2m_raises():
    # client_id set but token_url/secret missing -> explicit ValueError (not a
    # stripped assert), surfaced on backend resolution.
    with pytest.raises(ValueError, match="EMBEDDING_GATEWAY_TOKEN_URL"):
        ocr.build_ocr_backend(
            _settings(
                document_ocr_provider="gateway",
                embedding_gateway_url="https://gw",
                embedding_gateway_client_id="cid",
            )
        )


def test_gateway_backend_url_normalization():
    b = ocr._GatewayOcrBackend("https://gw", "mistral/mistral-ocr-latest")
    assert b._url == "https://gw/v1/ocr"
    b2 = ocr._GatewayOcrBackend("https://gw/v1/", "m")
    assert b2._url == "https://gw/v1/ocr"


# --- OcrProcessor ------------------------------------------------------------


async def test_processor_unsupported_when_no_backend(monkeypatch):
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_provider="none")
    )
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: None)
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "unsupported"


async def test_processor_success(monkeypatch):
    class _FakeBackend:
        async def ocr(self, content, mime_type):
            return (
                "hello world",
                [{"page": 1, "start_offset": 0, "end_offset": 11}],
                [],
            )

    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _FakeBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is True
    assert r.text == "hello world"
    assert r.metadata["page_count"] == 1
    assert r.processor == "ocr"
    # Empty block_spans -> the OCR_BLOCK_SPANS_KEY metadata is omitted entirely.
    assert ocr.OCR_BLOCK_SPANS_KEY not in r.metadata


async def test_processor_success_with_blocks_sets_block_spans(monkeypatch):
    """When the backend returns non-empty block spans (surya layout), they're
    surfaced under OCR_BLOCK_SPANS_KEY for generate_highlights to attribute."""
    spans = [
        {"page": 1, "bbox": [0.1, 0.1, 0.4, 0.2], "start_offset": 0, "end_offset": 5}
    ]

    class _FakeBackend:
        async def ocr(self, content, mime_type):
            return ("hello", [{"page": 1, "start_offset": 0, "end_offset": 5}], spans)

    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _FakeBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is True
    assert r.metadata[ocr.OCR_BLOCK_SPANS_KEY] == spans


async def test_processor_backend_error_returns_success_false(monkeypatch):
    class _BoomBackend:
        async def ocr(self, content, mime_type):
            raise RuntimeError("api down")

    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _BoomBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "error"


async def test_processor_timeout_returns_timeout_reason(monkeypatch):
    """A backend TimeoutError gets its own reason bucket (not 'error')."""

    class _TimeoutBackend:
        async def ocr(self, content, mime_type):
            raise TimeoutError

    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=5.0)
    )
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _TimeoutBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert "timed out" in (r.error or "")


async def test_gateway_httpx_timeout_maps_to_timeout_reason(monkeypatch):
    """A gateway httpx.ReadTimeout (not a builtin TimeoutError) must still map to
    parse_failed_reason='timeout', not 'error'."""
    import httpx

    class _HttpxTimeoutBackend:
        async def ocr(self, content, mime_type):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=5.0)
    )
    monkeypatch.setattr(
        ocr, "build_ocr_backend", lambda s, **kw: _HttpxTimeoutBackend()
    )
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert "timed out" in (r.error or "")


async def test_gateway_backend_uses_configured_timeout(mocker, monkeypatch):
    """The gateway OCR call must use DOCUMENT_OCR_TIMEOUT_SECONDS (resolved per
    call), not the old hardcoded 180s constant."""
    resp = mocker.Mock()
    resp.raise_for_status = mocker.Mock()
    resp.json = mocker.Mock(return_value={"pages": [{"index": 0, "markdown": "ok"}]})

    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    client.post = mocker.AsyncMock(return_value=resp)

    captured: dict[str, Any] = {}

    def _make_client(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return client

    monkeypatch.setattr(ocr.httpx, "AsyncClient", _make_client)
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=42.0)
    )

    backend = ocr._GatewayOcrBackend("https://gw", "mistral/mistral-ocr-latest")
    await backend.ocr(b"%PDF-1.7", "application/pdf")

    # httpx.Timeout(42.0, connect=10.0): the read/overall budget is the setting.
    assert captured["timeout"].read == pytest.approx(42.0)
    assert captured["timeout"].connect == pytest.approx(10.0)


async def test_mistral_backend_applies_timeout(mocker, monkeypatch):
    """The Mistral backend wraps process_async in DOCUMENT_OCR_TIMEOUT_SECONDS,
    so a slow OCR call fails fast instead of hanging on the SDK default."""
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=0.01)
    )

    # Bypass the SDK constructor; only the two attributes ocr() reads matter.
    backend = ocr._MistralOcrBackend.__new__(ocr._MistralOcrBackend)
    backend._model = "mistral-ocr-latest"

    async def _slow(*args, **kwargs):
        await anyio.sleep(1.0)

    backend._client = mocker.MagicMock()
    backend._client.ocr.process_async = _slow

    with pytest.raises(TimeoutError):
        await backend.ocr(b"%PDF-1.7", "application/pdf")


# --- batch mode (Deck #332) --------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        None,
        {},
        {"doc_id": "d", "doc_type": "file"},  # missing user_id
        {"user_id": "u", "doc_type": "file"},  # missing doc_id
        {"user_id": "u", "doc_id": "d"},  # missing doc_type
        {"user_id": "u", "doc_id": "d", "doc_type": ""},  # empty doc_type
    ],
)
def test_batch_identity_returns_none_without_full_identity(options):
    assert ocr._batch_identity(options) is None


def test_batch_identity_extracts_tuple_and_defaults_etag():
    assert ocr._batch_identity(
        {"user_id": "u", "doc_id": "d", "doc_type": "file", "etag": "v1"}
    ) == ("u", "d", "file", "v1")
    # etag may be absent/empty -> normalised to "".
    assert ocr._batch_identity({"user_id": "u", "doc_id": "d", "doc_type": "file"}) == (
        "u",
        "d",
        "file",
        "",
    )


_IDENTITY = {"user_id": "u1", "doc_id": "d1", "doc_type": "file", "etag": "v1"}


class _FakeStore:
    """In-memory stand-in for BatchOcrJobStore keyed like the real table."""

    def __init__(self, preset=None):
        self.rows: dict[tuple, Any] = {}
        self.deleted: list[tuple] = []
        self.stale_swept: list[tuple] = []
        if preset is not None:
            self.rows[("u1", "d1", "file", "v1")] = preset

    async def get(self, *, user_id, doc_id, doc_type, etag):
        return self.rows.get((user_id, doc_id, doc_type, etag))

    async def insert_pending(
        self, *, user_id, doc_id, doc_type, etag, job_id, submitted_at=None
    ):
        self.rows[(user_id, doc_id, doc_type, etag)] = SimpleNamespace(
            job_id=job_id, submitted_at=submitted_at or 1000
        )

    async def delete(self, *, user_id, doc_id, doc_type, etag):
        self.deleted.append((user_id, doc_id, doc_type, etag))
        self.rows.pop((user_id, doc_id, doc_type, etag), None)

    async def delete_stale_for_doc(self, *, user_id, doc_id, doc_type, keep_etag):
        self.stale_swept.append((user_id, doc_id, doc_type, keep_etag))


class _FakeBatchClient:
    def __init__(self, *, submit_job="mistral/job-1", poll=None):
        self._submit_job = submit_job
        self._poll = poll or BatchPollResult(status="pending", pages=[])
        self.submitted: list[tuple] = []
        self.polled: list[str] = []

    async def submit(self, content, mime_type, custom_id):
        self.submitted.append((content, mime_type, custom_id))
        return self._submit_job

    async def poll(self, job_id):
        self.polled.append(job_id)
        return self._poll


def _wire_batch(monkeypatch, *, client, store, settings=None):
    settings = settings or _settings(
        document_ocr_mode="batch",
        document_ocr_provider="gateway",
        embedding_gateway_url="https://gw",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s, **kw: client)

    async def _shared(cls):
        return store

    monkeypatch.setattr(_bos.BatchOcrJobStore, "shared", classmethod(_shared))


async def test_batch_first_run_submits_and_returns_pending_sentinel(monkeypatch):
    client = _FakeBatchClient()
    store = _FakeStore()
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF-1.7", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata[ocr.OCR_BATCH_PENDING_KEY] is True
    assert r.metadata[ocr.OCR_BATCH_RETRY_IN_KEY] == 120
    # submitted with the doc id as custom_id, recorded a pending row, swept stale
    assert client.submitted and client.submitted[0][2] == "d1"
    assert store.rows[("u1", "d1", "file", "v1")].job_id == "mistral/job-1"
    assert store.stale_swept == [("u1", "d1", "file", "v1")]


async def test_batch_existing_pending_polls_and_defers(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    client = _FakeBatchClient(poll=BatchPollResult(status="pending", pages=[]))
    store = _FakeStore(preset=preset)
    # submitted just now -> deadline not reached
    monkeypatch.setattr(ocr.time, "time", lambda: 1000.0)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert client.polled == ["mistral/j"]
    assert r.metadata[ocr.OCR_BATCH_PENDING_KEY] is True
    assert client.submitted == []  # did NOT resubmit


async def test_batch_poll_404_drops_row_and_resubmits(monkeypatch):
    # Incident 2026-07-03: a 404 on poll means the gateway lost the job (row purged
    # by retention or orphaned by a pod move). The processor must DROP its tracking
    # row and return the pending sentinel so the NEXT cycle re-submits a fresh job —
    # NOT re-poll the dead id forever (which flapped the burst GPU).
    from nextcloud_mcp_server.embedding.gateway_batch_client import OcrBatchJobNotFound

    preset = SimpleNamespace(job_id="surya/dead", submitted_at=1000)
    client = _FakeBatchClient()

    async def _poll_404(job_id):
        client.polled.append(job_id)
        raise OcrBatchJobNotFound(job_id)

    client.poll = _poll_404  # type: ignore[method-assign]
    store = _FakeStore(preset=preset)
    monkeypatch.setattr(ocr.time, "time", lambda: 1000.0)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert client.polled == ["surya/dead"]
    # tracking row dropped -> store.get is None next cycle -> fresh submit
    assert ("u1", "d1", "file", "v1") in store.deleted
    # returned the pending sentinel (re-poll → resubmit); did NOT resubmit inline
    assert r.metadata[ocr.OCR_BATCH_PENDING_KEY] is True
    assert client.submitted == []


async def test_batch_succeeded_returns_indexed_result(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    client = _FakeBatchClient(
        poll=BatchPollResult(
            status="succeeded", pages=[(0, "# One", None), (1, "## Two", None)]
        )
    )
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is True
    assert r.text == "# One\n\n## Two"
    assert r.metadata["page_count"] == 2
    assert ("u1", "d1", "file", "v1") in store.deleted  # row cleaned up


async def test_batch_succeeded_with_blocks_sets_block_spans(monkeypatch):
    """A layout-aware batch backend (surya) returns 3-tuple pages with blocks; the
    OCR processor must thread them into OCR_BLOCK_SPANS_KEY (the batch->bbox path,
    distinct from the sync path) so a scanned PDF gets pre-computed highlights."""
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    pages = [
        (0, "Heading", [{"html": "<h1>Heading</h1>", "bbox": [0.1, 0.1, 0.4, 0.2]}])
    ]
    client = _FakeBatchClient(poll=BatchPollResult(status="succeeded", pages=pages))
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is True and r.text == "Heading"
    spans = r.metadata[ocr.OCR_BLOCK_SPANS_KEY]
    assert len(spans) == 1
    assert spans[0]["bbox"] == [0.1, 0.1, 0.4, 0.2]
    assert r.text[spans[0]["start_offset"] : spans[0]["end_offset"]] == "Heading"


async def test_batch_failed_marks_parse_error(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    client = _FakeBatchClient(
        poll=BatchPollResult(status="failed", pages=[], error="x")
    )
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "error"
    assert ("u1", "d1", "file", "v1") in store.deleted


async def test_batch_unexpected_status_marks_failed_not_empty_success(monkeypatch):
    # A terminal status that isn't succeeded/failed (gateway skew) must NOT
    # produce a 0-chunk "success" that silently indexes empty text + loops.
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    client = _FakeBatchClient(poll=BatchPollResult(status="cancelled", pages=[]))
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "error"
    assert "cancelled" in (r.error or "")
    assert ("u1", "d1", "file", "v1") in store.deleted


async def test_batch_deadline_exceeded_marks_timeout(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", submitted_at=1000)
    client = _FakeBatchClient(poll=BatchPollResult(status="pending", pages=[]))
    store = _FakeStore(preset=preset)
    # now far past submitted_at + max_wait (86400)
    monkeypatch.setattr(ocr.time, "time", lambda: 1000.0 + 90000)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert ("u1", "d1", "file", "v1") in store.deleted


async def test_batch_raises_when_no_gateway(monkeypatch):
    """batch mode with no batch-capable backend (no gateway) must RAISE, never
    silently downgrade to synchronous OCR."""

    class _FakeBackend:
        async def ocr(self, content, mime_type):  # pragma: no cover - must not run
            raise AssertionError("sync backend must not be used in batch mode")

    settings = _settings(document_ocr_mode="batch", document_ocr_provider="mistral")
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s, **kw: None)
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _FakeBackend())

    with pytest.raises(ProcessorError, match="EMBEDDING_GATEWAY_URL"):
        await ocr.OcrProcessor().process(
            b"%PDF", "application/pdf", options=dict(_IDENTITY)
        )


async def test_batch_raises_provider_none_names_the_real_cause(monkeypatch):
    """provider=none in batch mode raises a message about OCR being disabled, not a
    misleading 'gateway missing' error (the startup guard exempts provider=none)."""
    settings = _settings(
        document_ocr_mode="batch",
        document_ocr_provider="none",
        embedding_gateway_url="https://gw",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)

    with pytest.raises(ProcessorError, match="DOCUMENT_OCR_PROVIDER=none"):
        await ocr.OcrProcessor().process(
            b"%PDF", "application/pdf", options=dict(_IDENTITY)
        )


async def test_batch_raises_when_no_identity(monkeypatch):
    """batch mode on the inline path (no per-doc identity) can't defer a poll, so
    it RAISES rather than silently transcribing synchronously."""

    class _FakeBackend:
        async def ocr(self, content, mime_type):  # pragma: no cover - must not run
            raise AssertionError("sync backend must not be used in batch mode")

    client = _FakeBatchClient()
    settings = _settings(
        document_ocr_mode="batch",
        document_ocr_provider="gateway",
        embedding_gateway_url="https://gw",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s, **kw: client)
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s, **kw: _FakeBackend())

    # No options -> inline path -> batch can't defer -> raise (not sync fallback).
    with pytest.raises(ProcessorError, match="worker"):
        await ocr.OcrProcessor().process(b"%PDF", "application/pdf", options=None)
    assert client.submitted == []  # never attempted batch


async def test_batch_submit_transport_error_propagates_not_caught(monkeypatch):
    # Opted into batch: a transport error from submit() must propagate (to
    # procrastinate for a durable retry), NOT be caught by the sync OCR
    # try/except or fall back to a surprise sync transcription. Guards the
    # intentional asymmetry documented in process().
    class _DownClient:
        submitted: list = []

        async def submit(self, content, mime_type, custom_id):
            raise httpx.ConnectError("gateway down")

        async def poll(self, job_id):  # pragma: no cover - not reached
            raise AssertionError("poll should not be called")

    sync_backend_used = False

    def _build_backend(_s):
        nonlocal sync_backend_used
        sync_backend_used = True
        return None

    monkeypatch.setattr(ocr, "build_ocr_backend", _build_backend)
    _wire_batch(monkeypatch, client=_DownClient(), store=_FakeStore())

    with pytest.raises(httpx.ConnectError):
        await ocr.OcrProcessor().process(
            b"%PDF", "application/pdf", options=dict(_IDENTITY)
        )
    assert sync_backend_used is False  # never fell back to the sync path
