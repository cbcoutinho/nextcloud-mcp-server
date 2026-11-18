"""``nextcloud.api.*`` spans carry the stable ``http.response.status_code`` key.

Before Deck #1511 these spans had no status code at all, so a TraceQL query on
``span.http.response.status_code`` could not find failed Nextcloud calls.
"""

import httpx2
import pytest
from nextcloud_client.notes import NotesClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import nextcloud_mcp_server.observability.metrics  # noqa: F401  installs the hooks
from nextcloud_mcp_server.observability import tracing

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", [200, 503])
async def test_api_span_records_response_status_code(monkeypatch, status):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer(__name__))

    transport = httpx2.MockTransport(lambda request: httpx2.Response(status))
    async with httpx2.AsyncClient(
        transport=transport, base_url="https://nc.example"
    ) as http:
        client = NotesClient(http, "alice")
        if status >= 400:
            with pytest.raises(httpx2.HTTPStatusError):
                await client._make_request("GET", "/apps/notes/api/v1/notes")
        else:
            await client._make_request("GET", "/apps/notes/api/v1/notes")

    (span,) = [
        s for s in exporter.get_finished_spans() if s.name.startswith("nextcloud.api.")
    ]
    assert span.attributes["http.response.status_code"] == status
    assert span.attributes["http.request.method"] == "GET"
    expected = StatusCode.ERROR if status >= 500 else StatusCode.OK
    assert span.status.status_code is expected
