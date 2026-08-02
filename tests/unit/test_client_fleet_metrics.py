"""Unit tests for MCP client-fleet observability.

These metrics exist to make the mcp python-sdk 1.x -> 2.x (protocol 2026-07-28)
upgrade's *silent* behaviour changes visible. Two things are pinned here:

1. ``record_client_session`` records who is connecting, with what capabilities,
   at which protocol version — deduplicated per session and with every
   client-supplied label clamped.
2. ``instrument_call_tool_outcomes`` distinguishes a tool error the model can
   see (``CallToolResult.isError``) from a protocol error it cannot (a JSON-RPC
   error). On mcp 1.x the latter is 0 by construction, which is precisely what
   makes it a usable regression alarm after the upgrade.

Counters live in the process-global ``REGISTRY`` and never reset between tests,
so every assertion here is on a *delta*.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.exceptions import McpError

from nextcloud_mcp_server.observability.metrics import (
    instrument_call_tool_outcomes,
    record_client_session,
)

pytestmark = pytest.mark.unit

# ``metric_sample`` is provided as a shared fixture in tests/unit/conftest.py.

SESSIONS = "mcp_client_sessions_total"
CAPABILITY = "mcp_client_capability"
OUTCOMES = "mcp_tool_outcomes_total"


def _client_params(
    *,
    name: str = "claude-code",
    version: str = "2.0.13",
    protocol: str = "2025-06-18",
    elicitation: bool = True,
) -> types.InitializeRequestParams:
    return types.InitializeRequestParams(
        protocolVersion=protocol,
        capabilities=types.ClientCapabilities(
            elicitation=types.ElicitationCapability() if elicitation else None,
        ),
        clientInfo=types.Implementation(name=name, version=version),
    )


class _FakeSession:
    """Minimal stand-in for ServerSession — only ``client_params`` is read."""

    def __init__(self, params: types.InitializeRequestParams | None) -> None:
        self.client_params = params


def _activate(session: _FakeSession):
    """Install a request context carrying ``session``, as the SDK does."""
    return request_ctx.set(
        SimpleNamespace(request_id=1, meta=None, session=session, lifespan_context=None)
    )


class TestRecordClientSession:
    def test_records_identity_and_capabilities(self, metric_session, metric_sample):
        labels = {
            "client_name": "claude-code",
            # "2.0.13" truncated to major.minor: a new series per patch release
            # would make the fleet view unreadable within weeks.
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session()

        assert metric_sample(SESSIONS, labels) - before == 1
        assert (
            metric_sample(
                CAPABILITY, {"client_name": "claude-code", "capability": "elicitation"}
            )
            == 1
        )
        # Explicitly zeroed rather than left absent, so a client that *stops*
        # declaring a capability reads as a drop instead of a stale 1.
        assert (
            metric_sample(
                CAPABILITY, {"client_name": "claude-code", "capability": "sampling"}
            )
            == 0
        )

    def test_dedups_per_session(self, metric_session, metric_sample):
        labels = {
            "client_name": "claude-code",
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session()
        record_client_session()
        record_client_session()

        assert metric_sample(SESSIONS, labels) - before == 1

    def test_new_session_records_again(self, metric_session, metric_sample):
        labels = {
            "client_name": "claude-code",
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session()
        token = _activate(_FakeSession(_client_params()))
        try:
            record_client_session()
        finally:
            request_ctx.reset(token)

        assert metric_sample(SESSIONS, labels) - before == 2

    def test_no_request_context_is_a_noop(self, metric_sample):
        """Outside a request the contextvar is unset — must not raise."""
        labels = {
            "client_name": "claude-code",
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session()

        assert metric_sample(SESSIONS, labels) == before

    def test_uninitialized_session_is_a_noop(self, metric_sample):
        """A session that has not completed initialize has no client_params."""
        labels = {
            "client_name": "unknown",
            "client_version": "unknown",
            "protocol_version": "unknown",
        }
        before = metric_sample(SESSIONS, labels)

        token = _activate(_FakeSession(None))
        try:
            record_client_session()
        finally:
            request_ctx.reset(token)

        assert metric_sample(SESSIONS, labels) == before

    def test_client_labels_are_clamped(self, metric_sample):
        """clientInfo is peer-supplied; unbounded labels would be a memory DoS."""
        token = _activate(_FakeSession(_client_params(name="x" * 200, version="1.2.3")))
        try:
            record_client_session()
        finally:
            request_ctx.reset(token)

        assert (
            metric_sample(
                SESSIONS,
                {
                    "client_name": "x" * 64,
                    "client_version": "1.2",
                    "protocol_version": "2025-06-18",
                },
            )
            == 1
        )

    def test_unrecognised_params_shape_degrades_to_unknown(self, metric_sample):
        """An SDK field rename surfaces as client_name="unknown", not an error.

        The accessors use ``getattr(..., None)``, which swallows AttributeError
        by design — instrumentation must never fail a tool call. The consequence
        is that if mcp 2.x renames these fields in a way the dual-spelling block
        does not cover, the metric keeps recording but every label reads
        "unknown". That is the signal to watch for after the upgrade; it is
        alerted on in docs/observability.md rather than logged.
        """

        class _UnrecognisedParams:
            """Params object exposing none of the names we know about."""

        labels = {
            "client_name": "unknown",
            "client_version": "unknown",
            "protocol_version": "unknown",
        }
        before = metric_sample(SESSIONS, labels)

        token = _activate(_FakeSession(_UnrecognisedParams()))
        try:
            record_client_session()
        finally:
            request_ctx.reset(token)

        assert metric_sample(SESSIONS, labels) - before == 1


class TestCallToolOutcomes:
    """The tool_error vs protocol_error split — the core upgrade tripwire."""

    @staticmethod
    def _wrap(inner):
        mcp = SimpleNamespace(
            _mcp_server=SimpleNamespace(request_handlers={types.CallToolRequest: inner})
        )
        instrument_call_tool_outcomes(mcp)
        return mcp._mcp_server.request_handlers[types.CallToolRequest]

    @staticmethod
    def _request(name: str = "nc_notes_create_note") -> types.CallToolRequest:
        return types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments={}),
        )

    async def test_success(self, metric_sample):
        labels = {"tool_name": "nc_notes_create_note", "outcome": "success"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_req):
            return types.ServerResult(types.CallToolResult(content=[], isError=False))

        await self._wrap(inner)(self._request())

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_tool_error(self, metric_sample):
        """isError=True — the model sees the message and can react."""
        labels = {"tool_name": "nc_notes_create_note", "outcome": "tool_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_req):
            return types.ServerResult(types.CallToolResult(content=[], isError=True))

        await self._wrap(inner)(self._request())

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_protocol_error_and_exception_propagates(self, metric_sample):
        """An escaping exception becomes a JSON-RPC error; the model sees nothing.

        This is 0 on mcp 1.x by construction — the SDK converts every exception
        except UrlElicitationRequiredError into isError=True — so any non-zero
        reading after the 2.x upgrade is the MCPError semantics flip.
        """
        labels = {"tool_name": "nc_notes_create_note", "outcome": "protocol_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_req):
            raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message="boom"))

        handler = self._wrap(inner)
        request = self._request()

        with pytest.raises(McpError):
            await handler(request)

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_cancellation_is_not_a_protocol_error(self, metric_sample):
        """anyio cancellation is a BaseException and must not poison the signal."""
        labels = {"tool_name": "nc_notes_create_note", "outcome": "protocol_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_req):
            raise KeyboardInterrupt

        handler = self._wrap(inner)
        request = self._request()

        with pytest.raises(KeyboardInterrupt):
            await handler(request)

        assert metric_sample(OUTCOMES, labels) == before


@pytest.fixture
def metric_session():
    """Activate a request context with an initialized client session."""
    token = _activate(_FakeSession(_client_params()))
    try:
        yield
    finally:
        request_ctx.reset(token)
