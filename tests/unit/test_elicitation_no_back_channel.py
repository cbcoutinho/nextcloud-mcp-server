"""Progressive consent on a 2026-07-28 connection, verified rather than assumed.

Deck card 946 asks for exactly this: *"Progressive-consent behaviour under a
2026-07-28 client is documented and deliberate... **verify the degradation
empirically, don't assume it.**"*

The 2026-07-28 protocol has no server-initiated requests, so ``ctx.elicit()``
cannot reach the client at all — the SDK raises ``NoBackChannelError`` instead of
sending. ``_run_elicit`` catches that and falls back to ``message_only``, which
means the interactive consent prompt is **gone** for modern clients.

Nothing here mocks the SDK: the tool runs behind a real ``Client(server)``
connection, which negotiates 2026-07-28 by default. That is the point — a mocked
``ctx.elicit`` would prove only that our ``except`` clause works, not that the
SDK actually refuses on this era.

Scope: this pins the *degradation*. Getting the login URL in front of the user
afterwards is the caller's contract, not this helper's — ``present_login_url``
returns only an outcome, and ``server/auth_tools.py`` is what builds the
URL-bearing message on every non-accepted branch. Asserting that here would mean
asserting a string this file wrote itself.
"""

import pytest
from mcp.client import Client
from mcp.server.mcpserver import Context

from nextcloud_mcp_server.auth.elicitation import present_login_url
from nextcloud_mcp_server.errors import NextcloudMCPServer

pytestmark = pytest.mark.unit

LOGIN_URL = "https://nextcloud.example.com/index.php/login/flow"
ELICITATIONS = "mcp_elicitation_total"
NO_BACK_CHANNEL = {
    "prompt": "login_flow",
    "outcome": "message_only",
    "reason": "no_back_channel",
}


def _server() -> NextcloudMCPServer:
    mcp = NextcloudMCPServer("elicitation-era-test")

    @mcp.tool()
    async def start_login(ctx: Context) -> str:
        """Drives progressive consent exactly as the auth path does."""
        return await present_login_url(ctx, LOGIN_URL)

    return mcp


async def test_2026_connection_degrades_to_message_only(metric_sample):
    """``ctx.elicit()`` is refused by the SDK, and the tool still succeeds."""
    before = metric_sample(ELICITATIONS, NO_BACK_CHANNEL)

    async with Client(_server()) as client:
        assert client.protocol_version >= "2026-07-28", (
            f"this test is meaningless on {client.protocol_version}"
        )
        result = await client.call_tool("start_login", {})

    # Degrades rather than failing: a NoBackChannelError escaping the tool would
    # reach the client as a JSON-RPC error and break provisioning outright.
    assert result.is_error is False, result.content
    assert "message_only" in str(result.content), (
        f"expected the no-back-channel fallback, got: {result.content}"
    )

    # The counter is the only production signal that interactive consent has
    # stopped happening, so the specific reason label matters, not just the
    # outcome — "error" here would mean we are guessing at the cause.
    assert metric_sample(ELICITATIONS, NO_BACK_CHANNEL) - before == 1, (
        "the fallback must be attributed to no_back_channel"
    )


async def test_elicitation_callback_does_not_rescue_it():
    """Registering a callback changes nothing: no request ever reaches it.

    Worth pinning, because "set an elicitation_callback" is the obvious wrong
    fix — on 2026-07-28 the server never gets to send, so the callback is dead
    code rather than a workaround.
    """
    called = False

    async def elicitation_callback(context, params):  # pragma: no cover - never runs
        nonlocal called
        called = True
        raise AssertionError("a 2026-07-28 server must not reach the client")

    async with Client(_server(), elicitation_callback=elicitation_callback) as client:
        result = await client.call_tool("start_login", {})

    assert called is False
    assert "message_only" in str(result.content)
