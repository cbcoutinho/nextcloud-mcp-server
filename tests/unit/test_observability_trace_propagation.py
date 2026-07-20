"""Inbound trace-context propagation and SERVER-span shape.

Astrolabe runs on a separate host and reaches this server only over HTTP, so a
W3C ``traceparent`` header is the only thing that can stitch a user-facing
request to the work done here. Before this, every request started its own root
trace: an Astrolabe call and the server work it triggered appeared as unrelated
traces, and a mid-request pod death showed up as ``<root span not yet
received>`` with nothing pointing at the caller.

The spans were also INTERNAL and carried no ``http.route``, so
``{kind=server}`` matched nothing and RED metrics by route were impossible.
"""

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.observability import tracing
from nextcloud_mcp_server.observability.middleware import ObservabilityMiddleware

pytestmark = pytest.mark.unit

# A well-formed W3C traceparent: version-traceid-spanid-flags (sampled).
UPSTREAM_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
UPSTREAM_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{UPSTREAM_TRACE_ID}-{UPSTREAM_SPAN_ID}-01"


@pytest.fixture
def exporter(monkeypatch):
    """Install a real in-memory tracer so spans can be asserted on."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The module-level tracer is what get_tracer() returns; patch it rather
    # than calling setup_tracing(), which would install a global OTLP exporter.
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer(__name__))
    return exporter


@pytest.fixture
def client(exporter):
    def ok(request):
        return JSONResponse({"ok": True})

    def boom(request):
        raise RuntimeError("kaboom")

    def caught_500(request):
        # Mirrors every /api/v1 handler: catches its own exception and returns
        # a 500 rather than letting it propagate.
        return JSONResponse({"error": "internal"}, status_code=500)

    app = Starlette(
        routes=[
            Route("/api/v1/status", ok, methods=["GET"]),
            Route("/health/live", ok, methods=["GET"]),
            Route("/api/v1/boom", boom, methods=["GET"]),
            Route("/api/v1/caught", caught_500, methods=["GET"]),
        ]
    )
    app.add_middleware(ObservabilityMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def _finished_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected exactly one span, got {len(spans)}"
    return spans[0]


def test_inbound_traceparent_becomes_the_parent(client, exporter):
    """A caller's traceparent must adopt our span into the caller's trace."""
    client.get("/api/v1/status", headers={"traceparent": TRACEPARENT})

    span = _finished_span(exporter)
    ctx = span.get_span_context()

    assert format(ctx.trace_id, "032x") == UPSTREAM_TRACE_ID
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == UPSTREAM_SPAN_ID


def test_request_without_traceparent_starts_its_own_trace(client, exporter):
    """An uninstrumented caller must degrade, not break."""
    client.get("/api/v1/status")

    span = _finished_span(exporter)

    assert span.parent is None
    assert span.get_span_context().trace_id != int(UPSTREAM_TRACE_ID, 16)


def test_malformed_traceparent_is_ignored(client, exporter):
    """A garbage header must not fail the request or the span."""
    response = client.get(
        "/api/v1/status", headers={"traceparent": "not-a-traceparent"}
    )

    assert response.status_code == 200
    span = _finished_span(exporter)
    assert span.parent is None


def test_span_is_server_kind_with_route(client, exporter):
    """kind=server + http.route are what make trace search usable."""
    client.get("/api/v1/status")

    span = _finished_span(exporter)

    assert span.kind is SpanKind.SERVER
    assert span.attributes["http.route"] == "/api/v1/status"
    assert span.attributes["http.method"] == "GET"


def test_tenant_id_is_attached_when_configured(client, exporter, monkeypatch):
    """One observability stack aggregates every tenant, so spans carry theirs."""
    from nextcloud_mcp_server.config import get_settings

    monkeypatch.setattr(
        type(get_settings()), "tenant_id", property(lambda self: "tenant-abc")
    )

    client.get("/api/v1/status")

    assert _finished_span(exporter).attributes["tenant.id"] == "tenant-abc"


def test_handler_exception_marks_the_span_and_is_logged(client, exporter, caplog):
    """A 500 must leave evidence: an error span and a log record.

    Both halves are asserted. Silencing the middleware's log line while
    keeping the span (or vice versa) is exactly the kind of half-regression
    that made the original OOMKill so hard to diagnose.
    """
    with caplog.at_level(
        logging.ERROR, logger="nextcloud_mcp_server.observability.middleware"
    ):
        response = client.get("/api/v1/boom")

    assert response.status_code == 500

    span = _finished_span(exporter)
    assert span.status.status_code is trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)

    failures = [
        r
        for r in caplog.records
        if r.name == "nextcloud_mcp_server.observability.middleware"
        and "Request failed" in r.getMessage()
    ]
    assert failures, "middleware must log the failing request"
    assert "/api/v1/boom" in failures[0].getMessage()
    # The traceback is the whole point on this path: it is the only record of an
    # exception no handler caught. logger.error() would drop it silently.
    assert failures[0].exc_info is not None, "crash log must carry the traceback"
    assert failures[0].exc_info[0] is RuntimeError


def test_handler_caught_500_still_marks_the_span_as_error(client, exporter):
    """A 500 returned rather than raised must still be an error span.

    Every /api/v1 handler catches its own exception and returns a 500, so
    nothing propagates to the middleware and the span would finish OK — making
    the failure invisible to a `status=error` trace query, which is precisely
    the blind spot this work exists to remove.
    """
    response = client.get("/api/v1/caught")

    assert response.status_code == 500

    span = _finished_span(exporter)
    assert span.attributes["http.status_code"] == 500
    assert span.status.status_code is trace.StatusCode.ERROR


def test_successful_response_leaves_the_span_ok(client, exporter):
    """...and a 2xx must not be mislabelled as an error."""
    client.get("/api/v1/status")

    assert _finished_span(exporter).status.status_code is not trace.StatusCode.ERROR


class TestCorrelationHeaders:
    """Astrolabe cannot emit spans, so it forwards identifiers instead.

    X-Request-Id is Nextcloud's reqId — the value prefixing every line that
    request writes to nextcloud.log — so recording it on the span is what makes
    a user-visible failure traceable from the Nextcloud log through to the
    backend work, with no spans on the Astrolabe side at all.
    """

    def test_request_id_is_recorded_on_the_span(self, client, exporter):
        client.get("/api/v1/status", headers={"X-Request-Id": "aBcD1234efGh5678ijKl"})

        span = _finished_span(exporter)
        assert span.attributes["client.request.id"] == "aBcD1234efGh5678ijKl"

    def test_absent_header_adds_no_attribute(self, client, exporter):
        """An uninstrumented caller must not gain an empty attribute."""
        client.get("/api/v1/status")

        assert "client.request.id" not in _finished_span(exporter).attributes

    def test_oversized_request_id_is_truncated(self, client, exporter):
        """A caller-controlled header must not inflate every span it touches."""
        client.get("/api/v1/status", headers={"X-Request-Id": "x" * 500})

        recorded = _finished_span(exporter).attributes["client.request.id"]
        assert len(recorded) == 128

    def test_failure_log_carries_the_request_id(self, client, exporter, caplog):
        """So a crash stays correlatable even if its trace was sampled out."""
        with caplog.at_level(
            logging.ERROR, logger="nextcloud_mcp_server.observability.middleware"
        ):
            client.get("/api/v1/boom", headers={"X-Request-Id": "req-42"})

        failures = [r for r in caplog.records if "Request failed" in r.getMessage()]
        assert failures
        assert getattr(failures[0], "client.request.id", None) == "req-42"

    def test_traceparent_still_wins_when_both_are_present(self, client, exporter):
        """The header is a fallback, not a replacement for real trace context."""
        client.get(
            "/api/v1/status",
            headers={"traceparent": TRACEPARENT, "X-Request-Id": "req-7"},
        )

        span = _finished_span(exporter)
        assert format(span.get_span_context().trace_id, "032x") == UPSTREAM_TRACE_ID
        assert span.attributes["client.request.id"] == "req-7"


def test_health_endpoints_are_not_traced(client, exporter):
    """Polling endpoints stay out of traces to keep the signal readable."""
    client.get("/health/live")

    assert exporter.get_finished_spans() == ()


class TestOtlpTransportSelection:
    """The endpoint scheme, not a side flag, must decide TLS vs plaintext.

    This previously passed ``insecure=not verify_ssl`` unconditionally, which
    overrode the exporter's own spec-compliant scheme handling
    (``insecure = parsed_url.scheme == "http"``). Since the flag defaulted to
    False, an ``https://`` collector was dialled in plaintext and every export
    failed with ``StatusCode.UNAVAILABLE`` — silently, because the failure only
    appears in the exporter's own logs.
    """

    @staticmethod
    def _insecure_arg_for(monkeypatch, endpoint, verify_ssl):
        captured = {}

        class FakeExporter:
            def __init__(self, endpoint, insecure=None):
                captured["endpoint"] = endpoint
                captured["insecure"] = insecure

        monkeypatch.setattr(tracing, "OTLPSpanExporter", FakeExporter)
        monkeypatch.setattr(tracing, "BatchSpanProcessor", lambda exporter: object())
        monkeypatch.setattr(
            tracing.TracerProvider, "add_span_processor", lambda self, p: None
        )
        tracing.setup_tracing(otlp_endpoint=endpoint, otlp_verify_ssl=verify_ssl)
        return captured

    def test_unset_defers_to_the_exporter(self, monkeypatch):
        """None must reach the exporter so it can read the scheme itself."""
        captured = self._insecure_arg_for(
            monkeypatch, "https://collector.example:4317", None
        )
        assert captured["insecure"] is None

    def test_explicit_true_forces_tls(self, monkeypatch):
        """An operator can still force TLS for a scheme-less endpoint."""
        captured = self._insecure_arg_for(monkeypatch, "collector.example:443", True)
        assert captured["insecure"] is False

    def test_explicit_false_forces_plaintext(self, monkeypatch):
        """...and still opt into plaintext behind a TLS-terminating sidecar."""
        captured = self._insecure_arg_for(
            monkeypatch, "http://collector.example:4317", False
        )
        assert captured["insecure"] is True
