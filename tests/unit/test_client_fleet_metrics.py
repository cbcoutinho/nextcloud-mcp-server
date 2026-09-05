"""Unit tests for MCP client-fleet observability.

These metrics exist to make the mcp python-sdk 1.x -> 2.x (protocol 2026-07-28)
upgrade's *silent* behaviour changes visible. Two things are pinned here:

1. ``record_client_session`` records who is connecting, with what capabilities,
   at which protocol version — deduplicated per session and with every
   client-supplied label clamped.
2. ``instrument_call_tool_outcomes`` distinguishes a tool error the model can
   see (``CallToolResult.is_error``) from a protocol error it cannot (a JSON-RPC
   error). On mcp 1.x the latter is 0 by construction, which is precisely what
   makes it a usable regression alarm after the upgrade.

Counters live in the process-global ``REGISTRY`` and never reset between tests,
so every assertion here is on a *delta*.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.observability import metrics as metrics_module
from nextcloud_mcp_server.observability.metrics import (
    _MAX_TRACKED_CLIENTS,
    instrument_call_tool_outcomes,
    record_client_session,
    record_oauth_token_validation,
)

pytestmark = pytest.mark.unit

# ``metric_sample`` is provided as a shared fixture in tests/unit/conftest.py.

SESSIONS = "mcp_client_sessions_total"
CAPABILITY = "mcp_client_capability"
OUTCOMES = "mcp_tool_outcomes_total"


@pytest.fixture(autouse=True)
def _isolate_module_globals():
    """Snapshot/restore *every* process-global this module mutates.

    Three of them now: the two cardinality budgets and the warned-cause set.
    All are process-global and never expire, so a test that fills one leaves it
    filled for whatever runs next — the capacity test mints ~70 identities, and
    the diagnostics tests mark both causes as already-warned. Either would
    silently turn a later assertion into its opposite (a real ``client_name``
    reading as ``"_other"``; an expected warning not firing).

    Deliberately covers all three rather than naming them, because the previous
    version covered two and a third was added without noticing the asymmetry.
    Anything added to the tuple below gets the same treatment for free.
    """
    names = ("_seen_client_names", "_seen_client_ids", "_warned_causes")
    saved = {n: set(getattr(metrics_module, n)) for n in names}
    # Cleared, not just restored: the diagnostics tests need an unwarned slate
    # to observe the once-per-process behaviour at all.
    getattr(metrics_module, "_warned_causes").clear()
    try:
        yield
    finally:
        for n in names:
            current = getattr(metrics_module, n)
            current.clear()
            current.update(saved[n])


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


def _ctx(session: _FakeSession, protocol: str = "2025-06-18") -> ServerRequestContext:
    """The request context the SDK hands a handler, carrying ``session``.

    ``protocol_version`` is the *negotiated* version and lives on the context in
    mcp 2.x, not on ``client_params`` — so it is populated even when the params
    shape is one the metric does not recognise.
    """
    return ServerRequestContext(
        session=session,
        lifespan_context=None,
        protocol_version=protocol,
        method="tools/call",
        params={"name": "nc_notes_create_note", "arguments": {}},
        request_id=1,
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

        record_client_session(metric_session)

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

        record_client_session(metric_session)
        record_client_session(metric_session)
        record_client_session(metric_session)

        assert metric_sample(SESSIONS, labels) - before == 1

    def test_new_session_records_again(self, metric_session, metric_sample):
        labels = {
            "client_name": "claude-code",
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session(metric_session)
        record_client_session(_ctx(_FakeSession(_client_params())))

        assert metric_sample(SESSIONS, labels) - before == 2

    def test_no_request_context_is_a_noop(self, metric_sample):
        """Outside a request there is no context to pass — must not raise."""
        labels = {
            "client_name": "claude-code",
            "client_version": "2.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session(None)

        assert metric_sample(SESSIONS, labels) == before

    def test_uninitialized_session_is_a_noop(self, metric_sample):
        """A session that has not completed initialize has no client_params."""
        labels = {
            "client_name": "unknown",
            "client_version": "unknown",
            "protocol_version": "unknown",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session(_ctx(_FakeSession(None)))

        assert metric_sample(SESSIONS, labels) == before

    def test_client_labels_are_clamped(self, metric_sample):
        """clientInfo is peer-supplied; unbounded labels would be a memory DoS."""
        record_client_session(
            _ctx(_FakeSession(_client_params(name="x" * 200, version="1.2.3")))
        )

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
        does not cover, the metric keeps recording but every client label reads
        "unknown". That is the signal to watch for after the upgrade; it is
        alerted on in docs/observability.md rather than logged.

        ``protocol_version`` is exempt: mcp 2.x carries the negotiated version
        on the request context, not on ``client_params``, so it survives a
        params-shape change the client labels do not.
        """

        class _UnrecognisedParams:
            """Params object exposing none of the names we know about."""

        labels = {
            "client_name": "unknown",
            "client_version": "unknown",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, labels)

        record_client_session(_ctx(_FakeSession(_UnrecognisedParams())))

        assert metric_sample(SESSIONS, labels) - before == 1


class TestAbsentIdentityLabelling:
    """ "Absent" must have exactly one spelling."""

    def test_empty_client_id_records_as_unknown(self, metric_sample):
        """`AccessToken.client_id` defaults to "" when the payload has no claim.

        Recording that verbatim gives an empty label — a second spelling of
        "we don't know" that splits the series in two and reads as a rendering
        bug on a dashboard. CI caught this the moment client_id started being
        passed on the acceptance path: two tests expecting "unknown" began
        seeing "" instead.
        """
        unknown = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        empty = {**unknown, "client_id": ""}
        before_unknown = metric_sample("mcp_oauth_token_validations_total", unknown)

        record_oauth_token_validation("jwt", "valid", "none", "")

        assert (
            metric_sample("mcp_oauth_token_validations_total", unknown) - before_unknown
            == 1
        )
        assert metric_sample("mcp_oauth_token_validations_total", empty) == 0


class TestSilentFailureDiagnostics:
    """The instrumentation must be able to report on its own failure.

    0.162.0 shipped with `mcp_client_sessions_total` recording nothing in
    production while `mcp_tool_outcomes_total` — incremented two lines later in
    the same handler — worked. Both early-returns in `record_client_session`
    were silent at every log level, so the metric could not say why it was
    empty. That is the exact failure this whole metric family exists to remove,
    rebuilt inside the thing removing it.
    """

    def test_missing_request_context_says_so(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="nextcloud_mcp_server.observability.metrics"
        ):
            record_client_session(None)

        assert any("no request context" in r.message for r in caplog.records)

    def test_missing_client_params_says_so(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="nextcloud_mcp_server.observability.metrics"
        ):
            record_client_session(_ctx(_FakeSession(None)))

        assert any("no client_params" in r.message for r in caplog.records)

    def test_warns_once_per_process_not_per_request(self, caplog):
        """These fire on the tool-call hot path — one line, not one per call."""
        ctx = _ctx(_FakeSession(None))
        with caplog.at_level(
            logging.WARNING, logger="nextcloud_mcp_server.observability.metrics"
        ):
            for _ in range(25):
                record_client_session(ctx)

        assert len([r for r in caplog.records if "no client_params" in r.message]) == 1


class TestCallToolOutcomes:
    """The tool_error vs protocol_error split — the core upgrade tripwire."""

    @staticmethod
    def _wrap(inner, registered=("nc_notes_create_note",)):
        """Drive the middleware with ``inner`` as the rest of the chain.

        ``registered`` stands in for the tool registry. mcp 2.x has no
        ``request_handlers`` dict to patch, so the instrumentation is a
        middleware and the test calls it the way the dispatcher would.
        """
        mcp = SimpleNamespace(
            middleware=[],
            _tool_manager=SimpleNamespace(
                get_tool=lambda name: object() if name in registered else None
            ),
        )
        instrument_call_tool_outcomes(mcp)
        (middleware,) = mcp.middleware

        async def call(ctx):
            return await middleware(ctx, inner)

        return call

    @staticmethod
    def _request(name: str = "nc_notes_create_note") -> ServerRequestContext:
        ctx = _ctx(_FakeSession(_client_params()))
        ctx.params = {"name": name, "arguments": {}}
        return ctx

    async def test_success(self, metric_sample):
        labels = {"tool_name": "nc_notes_create_note", "outcome": "success"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_ctx_):
            return types.CallToolResult(content=[], is_error=False)

        await self._wrap(inner)(self._request())

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_tool_error(self, metric_sample):
        """is_error=True — the model sees the message and can react."""
        labels = {"tool_name": "nc_notes_create_note", "outcome": "tool_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_ctx_):
            return types.CallToolResult(content=[], is_error=True)

        await self._wrap(inner)(self._request())

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_protocol_error_and_exception_propagates(self, metric_sample):
        """An escaping exception becomes a JSON-RPC error; the model sees nothing.

        On mcp 2.x an MCPError raised in a tool would reach the client as a
        JSON-RPC error, but NextcloudMCPServer.call_tool maps it back to
        ToolError, so in production this counter should stay at zero. A
        non-zero reading means something raised past that boundary.
        """
        labels = {"tool_name": "nc_notes_create_note", "outcome": "protocol_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_ctx_):
            raise MCPError(code=types.INTERNAL_ERROR, message="boom")

        handler = self._wrap(inner)
        request = self._request()

        with pytest.raises(MCPError):
            await handler(request)

        assert metric_sample(OUTCOMES, labels) - before == 1

    async def test_cancellation_is_not_a_protocol_error(self, metric_sample):
        """anyio cancellation is a BaseException and must not poison the signal."""
        labels = {"tool_name": "nc_notes_create_note", "outcome": "protocol_error"}
        before = metric_sample(OUTCOMES, labels)

        async def inner(_ctx_):
            raise KeyboardInterrupt

        handler = self._wrap(inner)
        request = self._request()

        with pytest.raises(KeyboardInterrupt):
            await handler(request)

        assert metric_sample(OUTCOMES, labels) == before


@pytest.fixture
def metric_session() -> ServerRequestContext:
    """A request context carrying an initialized client session."""
    return _ctx(_FakeSession(_client_params()))


class TestLabelCardinality:
    """Both halves of the cardinality bound: value size AND value count.

    Prometheus entries never expire, so a peer that can influence a label value
    can grow the process-global registry without limit. `clientInfo.name` and
    the `tools/call` `name` are both caller-chosen, so both are bounded — the
    former by a cap on distinct values, the latter by the tool registry.
    """

    async def test_unregistered_tool_name_collapses_to_unknown(self, metric_sample):
        """A bogus tools/call name must not mint a series of its own.

        Left raw this would also pollute the protocol_error tripwire with
        caller-chosen labels that say nothing about the SDK upgrade.
        """
        bogus = "../../etc/passwd" + "A" * 500
        bogus_labels = {"tool_name": bogus, "outcome": "tool_error"}
        unknown_labels = {"tool_name": "unknown", "outcome": "tool_error"}
        before_unknown = metric_sample(OUTCOMES, unknown_labels)

        async def inner(_ctx_):
            return types.CallToolResult(content=[], is_error=True)

        handler = TestCallToolOutcomes._wrap(inner)
        await handler(TestCallToolOutcomes._request(bogus))

        assert metric_sample(OUTCOMES, bogus_labels) == 0
        assert metric_sample(OUTCOMES, unknown_labels) - before_unknown == 1

    def test_distinct_client_names_are_capped(self, metric_sample):
        """A client varying clientInfo.name per session cannot grow series forever.

        Length-clamping alone does not close this: it bounds how big one value
        gets, not how many distinct ones a peer can mint.
        """
        overflow_labels = {
            "client_name": "_other",
            "client_version": "1.0",
            "protocol_version": "2025-06-18",
        }
        before = metric_sample(SESSIONS, overflow_labels)

        # Well past the cap, each session declaring a fresh identity.
        for i in range(_MAX_TRACKED_CLIENTS + 20):
            record_client_session(
                _ctx(
                    _FakeSession(_client_params(name=f"rotating-{i}", version="1.0.0"))
                )
            )

        assert metric_sample(SESSIONS, overflow_labels) - before >= 20

    def test_client_id_budget_is_separate_from_client_name(self, metric_sample):
        """The two untrusted labels must not share a cap.

        `clientInfo.name` (MCP handshake) and `client_id` (unverified rejected
        token) are both peer-supplied. A shared budget would let a peer exhaust
        one dimension by flooding the other — and the flood would come from the
        *rejection* path, which is exactly where identifying the client matters
        most.
        """
        overflow = {
            "method": "jwt",
            "result": "invalid",
            "reason": "expired",
            "client_id": "_other",
        }
        before = metric_sample("mcp_oauth_token_validations_total", overflow)

        for i in range(_MAX_TRACKED_CLIENTS + 10):
            record_oauth_token_validation(
                "jwt", "invalid", "expired", f"rotating-client-{i}"
            )

        assert (
            metric_sample("mcp_oauth_token_validations_total", overflow) - before >= 10
        )
        # The client_name budget is untouched by that flood.
        assert len(metrics_module._seen_client_names) < _MAX_TRACKED_CLIENTS
