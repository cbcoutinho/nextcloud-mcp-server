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
    async def ok(request):
        return JSONResponse({"ok": True})

    async def boom(request):
        raise RuntimeError("kaboom")

    app = Starlette(
        routes=[
            Route("/api/v1/status", ok, methods=["GET"]),
            Route("/health/live", ok, methods=["GET"]),
            Route("/api/v1/boom", boom, methods=["GET"]),
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
    """A 500 must leave evidence: an error span and a logged traceback."""
    with caplog.at_level(logging.ERROR, logger="nextcloud_mcp_server.observability"):
        response = client.get("/api/v1/boom")

    assert response.status_code == 500

    span = _finished_span(exporter)
    assert span.status.status_code is trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_health_endpoints_are_not_traced(client, exporter):
    """Polling endpoints stay out of traces to keep the signal readable."""
    client.get("/health/live")

    assert exporter.get_finished_spans() == ()
