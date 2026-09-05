"""The one structured log line per MCP tool call.

Tool name and arguments also reach Tempo as span attributes, but traces are
sampled and short-retention. These lines are what answers "which tools ran,
with what inputs" over a month, so they are asserted field by field: a Loki
query and a Grafana panel break silently when a field is renamed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.observability.metrics import (
    _sanitize_tool_args,
    instrument_call_tool_outcomes,
)
from nextcloud_mcp_server.server import AVAILABLE_APPS, configure_app_tools
from nextcloud_mcp_server.server.auth_tools import register_auth_tools
from nextcloud_mcp_server.server.oauth_tools import register_oauth_tools

pytestmark = pytest.mark.unit

LOGGER = "nextcloud_mcp_server.observability.metrics"


def _wrap(inner, registered=("nc_semantic_search",)):
    """Drive the middleware with ``inner`` as the rest of the chain.

    ``registered`` stands in for the tool registry. mcp 2.x has no
    ``request_handlers`` dict to patch, so the instrumentation is a middleware
    and the test calls it the way the dispatcher would.
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


def _request(name: str = "nc_semantic_search", **arguments) -> ServerRequestContext:
    return ServerRequestContext(
        # client_params=None is the "before initialize" shape; it makes
        # record_client_session bow out without touching a real ServerSession.
        session=SimpleNamespace(client_params=None),
        lifespan_context=None,
        protocol_version=types.LATEST_PROTOCOL_VERSION,
        method="tools/call",
        params={"name": name, "arguments": arguments},
        request_id=1,
    )


async def _ok(_ctx):
    return types.CallToolResult(content=[], is_error=False)


def _line(caplog):
    """The single tool-call record, or an assertion failure saying so."""
    records = [r for r in caplog.records if getattr(r, "mcp_tool", None)]
    assert len(records) == 1, f"expected one tool-call line, got {len(records)}"
    return records[0]


class TestToolCallLine:
    async def test_success_logs_name_args_duration(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _wrap(_ok)(_request(query="safeguarding policy", limit=3))

        record = _line(caplog)
        assert record.levelno == logging.INFO
        assert record.mcp_tool == "nc_semantic_search"
        assert record.outcome == "success"
        assert isinstance(record.duration_ms, int)
        assert "'query': 'safeguarding policy'" in record.mcp_tool_args
        assert "'limit': 3" in record.mcp_tool_args
        # No unregistered-name noise on the normal path
        assert not hasattr(record, "mcp_tool_requested")

    async def test_tool_error_logs_at_warning(self, caplog):
        async def inner(_ctx):
            return types.CallToolResult(content=[], is_error=True)

        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _wrap(inner)(_request())

        record = _line(caplog)
        assert record.outcome == "tool_error"
        assert record.levelno == logging.WARNING

    async def test_protocol_error_logs_and_still_raises(self, caplog):
        async def inner(_ctx):
            raise MCPError(code=types.INTERNAL_ERROR, message="boom")

        handler = _wrap(inner)
        request = _request()

        with caplog.at_level(logging.INFO, logger=LOGGER):
            with pytest.raises(MCPError):
                await handler(request)

        record = _line(caplog)
        assert record.outcome == "protocol_error"
        assert record.levelno == logging.WARNING

    async def test_no_arguments_omits_the_args_field(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _wrap(_ok)(_request())

        assert not hasattr(_line(caplog), "mcp_tool_args")

    async def test_unregistered_name_is_logged_but_not_labelled(self, caplog):
        """The metric label collapses to "unknown"; the line keeps the typo."""
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _wrap(_ok)(_request(name="nc_semantic_serach"))

        record = _line(caplog)
        assert record.mcp_tool == "unknown"
        assert record.mcp_tool_requested == "nc_semantic_serach"

    async def test_identity_absent_without_a_token(self, caplog):
        """BasicAuth / single-user / stdio: no OAuth identity, and no crash."""
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await _wrap(_ok)(_request(query="x"))

        record = _line(caplog)
        assert not hasattr(record, "mcp_user")
        assert not hasattr(record, "mcp_client_id")

    async def test_identity_from_access_token(self, caplog):
        token = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="opaque",
                    client_id="claude-ai",
                    scopes=["openid"],
                    # This codebase stores the username in `resource`
                    # (unified_verifier.py), not in `subject`.
                    resource="alice",
                )
            )
        )
        try:
            with caplog.at_level(logging.INFO, logger=LOGGER):
                await _wrap(_ok)(_request(query="x"))
        finally:
            auth_context_var.reset(token)

        record = _line(caplog)
        assert record.mcp_user == "alice"
        assert record.mcp_client_id == "claude-ai"


def test_every_tool_function_is_named_after_the_tool_it_registers():
    """Keeps the per-tool metrics joinable with the per-call ones.

    ``mcp_tool_calls_total`` / ``mcp_tool_duration_seconds`` label with
    ``func.__name__`` (from ``@instrument_tool``), while
    ``mcp_tool_outcomes_total`` and the log line above use the *registered* MCP
    name. The four OAuth tools used to disagree — ``tool_provision_access`` vs
    ``provision_nextcloud_access`` — which silently split every dashboard that
    joined them. A mismatch is invisible at runtime, so it needs a test.
    """
    mcp = MCPServer(name="test-tool-names")
    for app_name in AVAILABLE_APPS:
        configure_app_tools(mcp, app_name)
    register_auth_tools(mcp)
    register_oauth_tools(mcp)

    mismatched = {
        tool.name: tool.fn.__name__
        for tool in mcp._tool_manager.list_tools()
        if tool.fn.__name__ != tool.name
    }

    assert not mismatched, (
        "these tools register under a name that differs from their function "
        f"name, so their metrics and logs disagree: {mismatched}. Rename the "
        "function to match the registered name."
    )


class TestSanitizeToolArgs:
    """Arguments here are raw client input — validate_input=False upstream — so
    redaction cannot assume the keys a tool actually declares."""

    def test_redacts_by_substring_not_exact_match(self):
        rendered = _sanitize_tool_args(
            {
                "query": "notes",
                "access_token": "eyJ...",
                "clientSecret": "shh",
                "refresh_token": "r",
                "password": "hunter2",
            }
        )

        for secret in ("eyJ...", "shh", "hunter2"):
            assert secret not in rendered
        assert rendered.count("[redacted]") == 4
        assert "'query': 'notes'" in rendered

    def test_redacts_a_secret_the_tool_never_declared(self):
        """A caller can send any key to any tool; the key still gets caught."""
        assert "hunter2" not in _sanitize_tool_args({"password": "hunter2"})

    def test_long_value_capped_without_hiding_later_arguments(self):
        rendered = _sanitize_tool_args({"content": "x" * 5000, "path": "/notes/a.md"})

        assert "x" * 150 in rendered
        assert "x" * 300 not in rendered
        assert "'path': '/notes/a.md'" in rendered

    def test_many_arguments_capped_overall(self):
        rendered = _sanitize_tool_args({f"k{i}": "y" * 150 for i in range(50)})

        assert len(rendered) == 1000

    def test_nothing_worth_logging_returns_none(self):
        assert _sanitize_tool_args({}) is None
        assert _sanitize_tool_args(None) is None
        # ctx and etag are dropped as noise, leaving nothing
        assert _sanitize_tool_args({"ctx": object(), "etag": "abc"}) is None
