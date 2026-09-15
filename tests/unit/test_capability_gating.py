"""Unit tests for gating MCP tools on upstream app capabilities.

Covers the three questions the gate has to get right:

* does ``unmet_capability`` close only when it actually knows better (a missing
  app, a too-old version) and stay open on every unknown, and
* does ``NextcloudMCPServer`` hide exactly the unmet tools from ``tools/list``
  while refusing them in ``tools/call``, and
* does an ungated tool set cost zero OCS round-trips.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from packaging.version import InvalidVersion

import nextcloud_mcp_server.capabilities as cap
from nextcloud_mcp_server.capabilities import (
    clear_cache,
    get_required_capability,
    require_capability,
    stamp_required_capability,
    unmet_capability,
)
from nextcloud_mcp_server.config import _DEFAULTS, _reload_config, set_override
from nextcloud_mcp_server.errors import NextcloudMCPServer

pytestmark = pytest.mark.unit

_GATING_KEY = "MCP_DISABLE_CAPABILITY_GATING"


def _payload(**apps) -> dict:
    """An OCS capabilities envelope advertising ``{app: block}``."""
    return {"ocs": {"meta": {"status": "ok"}, "data": {"capabilities": dict(apps)}}}


def _client(payload=None, raises: Exception | None = None) -> AsyncMock:
    m = AsyncMock()
    m.username = "alice"
    if raises is not None:
        m.capabilities.side_effect = raises
    else:
        m.capabilities.return_value = payload
    return m


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def set_gating_flag():
    """Set MCP_DISABLE_CAPABILITY_GATING to a raw value, then restore it.

    ``_reload_config`` only drops the memoisation cache, so the override has to
    be written back explicitly or it leaks into the next test.
    """

    def _apply(value) -> None:
        set_override(_GATING_KEY, value)
        _reload_config()

    yield _apply
    set_override(_GATING_KEY, _DEFAULTS[_GATING_KEY.lower()])
    _reload_config()


@pytest.fixture
def disable_gating(set_gating_flag):
    set_gating_flag(True)


# ---------------------------------------------------------------------------
# require_capability metadata
# ---------------------------------------------------------------------------


def test_require_capability_stamps_metadata():
    @require_capability("deck", min_version="1.18.0")
    def tool():
        pass

    assert get_required_capability(tool) == ("deck", "1.18.0", None)


def test_require_capability_rejects_unparseable_floor():
    # A typo'd floor must fail at import time, not silently gate at runtime.
    with pytest.raises(InvalidVersion):
        require_capability("deck", min_version="one point eight")


def test_ungated_function_has_no_gate():
    def tool():
        pass

    assert get_required_capability(tool) is None


def test_stamp_does_not_override_an_explicit_gate():
    @require_capability("deck", min_version="1.18.0")
    def tool():
        pass

    stamp_required_capability(tool, "deck")

    assert get_required_capability(tool) == ("deck", "1.18.0", None)


def test_stamp_applies_presence_gate_when_undeclared():
    def tool():
        pass

    stamp_required_capability(tool, "spreed")

    assert get_required_capability(tool) == ("spreed", None, None)


# ---------------------------------------------------------------------------
# unmet_capability
# ---------------------------------------------------------------------------


async def test_version_at_floor_is_allowed():
    client = _client(_payload(deck={"version": "1.18.0"}))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_version_above_floor_is_allowed():
    client = _client(_payload(deck={"version": "1.18.3"}))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_version_below_floor_is_refused_with_both_versions():
    client = _client(_payload(deck={"version": "1.17.2"}))

    reason = await unmet_capability(client, "alice", "deck", "1.18.0")

    assert reason is not None
    assert "1.18.0" in reason and "1.17.2" in reason


async def test_prerelease_below_the_release_is_refused():
    # PEP 440: 1.18.0-beta.3 < 1.18.0. Deliberate — a beta is where the feature
    # is still moving, so it stays gated out.
    client = _client(_payload(deck={"version": "1.18.0-beta.3"}))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is not None


async def test_absent_app_is_refused_even_without_a_floor():
    client = _client(_payload(notes={"version": "4.9.0"}))

    reason = await unmet_capability(client, "alice", "deck", None)

    assert reason is not None
    assert "deck" in reason


async def test_present_app_without_floor_is_allowed():
    client = _client(_payload(deck={"version": "1.17.2"}))
    assert await unmet_capability(client, "alice", "deck", None) is None


async def test_capabilities_failure_fails_open():
    client = _client(raises=RuntimeError("ocs down"))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_unreadable_payload_fails_open():
    client = _client({"unexpected": "shape"})
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_missing_version_field_fails_open():
    # The app is installed but says nothing about its version — don't guess.
    client = _client(_payload(deck={"canCreateBoards": True}))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_unparseable_advertised_version_fails_open():
    client = _client(_payload(deck={"version": "nightly"}))
    assert await unmet_capability(client, "alice", "deck", "1.18.0") is None


async def test_capabilities_are_fetched_once_per_user():
    client = _client(_payload(deck={"version": "1.18.0"}))

    await unmet_capability(client, "alice", "deck", "1.18.0")
    await unmet_capability(client, "alice", "deck", None)

    assert client.capabilities.await_count == 1


# ---------------------------------------------------------------------------
# NextcloudMCPServer integration: tools/list + tools/call
# ---------------------------------------------------------------------------


def _server(payload=None, raises: Exception | None = None) -> tuple:
    """A server with one gated + one ungated tool, and its fake OCS client."""
    mcp = NextcloudMCPServer("test")

    @mcp.tool()
    @require_capability("deck", min_version="1.18.0")
    async def deck_assign_dependent_card() -> str:
        return "assigned"

    @mcp.tool()
    async def nc_notes_create_note() -> str:
        return "created"

    client = _client(payload, raises)
    return mcp, client


def _patch_gate_client(mocker, client) -> None:
    """Serve the gate a client without a live MCP request context."""
    mocker.patch.object(
        cap, "_gate_client", AsyncMock(return_value=(client, client.username))
    )


async def test_list_tools_hides_only_the_unmet_tool(mocker):
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    _patch_gate_client(mocker, client)

    names = {tool.name for tool in await mcp.list_tools()}

    assert names == {"nc_notes_create_note"}


async def test_list_tools_keeps_a_satisfied_tool(mocker):
    mcp, client = _server(_payload(deck={"version": "1.18.0"}))
    _patch_gate_client(mocker, client)

    names = {tool.name for tool in await mcp.list_tools()}

    assert names == {"deck_assign_dependent_card", "nc_notes_create_note"}


async def test_list_tools_fails_open_when_capabilities_error(mocker):
    mcp, client = _server(raises=RuntimeError("ocs down"))
    _patch_gate_client(mocker, client)

    names = {tool.name for tool in await mcp.list_tools()}

    assert "deck_assign_dependent_card" in names


async def test_list_tools_fails_open_when_no_client_is_available(mocker):
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    mocker.patch.object(
        cap, "_gate_client", AsyncMock(side_effect=RuntimeError("not authenticated"))
    )

    names = {tool.name for tool in await mcp.list_tools()}

    assert "deck_assign_dependent_card" in names


async def test_list_tools_without_gates_makes_no_ocs_call(mocker):
    mcp = NextcloudMCPServer("test")

    @mcp.tool()
    async def nc_notes_create_note() -> str:
        return "created"

    client = _client(_payload(deck={"version": "1.18.0"}))
    _patch_gate_client(mocker, client)

    await mcp.list_tools()

    assert client.capabilities.await_count == 0


async def test_call_tool_refuses_an_unmet_gate(mocker):
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    _patch_gate_client(mocker, client)

    with pytest.raises(ToolError) as excinfo:
        await mcp.call_tool("deck_assign_dependent_card", {})

    message = str(excinfo.value)
    assert "deck" in message and "1.18.0" in message and "1.17.2" in message


async def test_call_tool_runs_when_the_gate_is_met(mocker):
    mcp, client = _server(_payload(deck={"version": "1.18.0"}))
    _patch_gate_client(mocker, client)

    result = await mcp.call_tool("deck_assign_dependent_card", {})

    assert "assigned" in str(result)


async def test_call_tool_of_an_ungated_tool_makes_no_ocs_call(mocker):
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    _patch_gate_client(mocker, client)

    await mcp.call_tool("nc_notes_create_note", {})

    assert client.capabilities.await_count == 0


async def test_escape_hatch_disables_hiding_and_refusal(mocker, disable_gating):
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    _patch_gate_client(mocker, client)

    names = {tool.name for tool in await mcp.list_tools()}
    result = await mcp.call_tool("deck_assign_dependent_card", {})

    assert "deck_assign_dependent_card" in names
    assert "assigned" in str(result)
    assert client.capabilities.await_count == 0


@pytest.mark.parametrize(
    "raw,expected_disabled",
    [
        # Dynaconf casts env vars with TOML syntax, which only recognises
        # lowercase true/false — so a capitalised or numeric value arrives as a
        # string, and `bool("False")` would invert the operator's intent.
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("", False),
    ],
)
async def test_escape_hatch_reads_env_var_spellings(
    mocker, set_gating_flag, raw, expected_disabled
):
    set_gating_flag(raw)
    mcp, client = _server(_payload(deck={"version": "1.17.2"}))
    _patch_gate_client(mocker, client)

    names = {tool.name for tool in await mcp.list_tools()}

    assert ("deck_assign_dependent_card" in names) is expected_disabled


# Feature-flag gating
# ---------------------------------------------------------------------------


async def test_feature_gate_refuses_a_feature_the_app_does_not_advertise():
    """A declared feature the app omits closes the gate.

    Preferred over a version floor where the app publishes flags: it states
    what the tool needs, checked against what the instance says about itself.
    """
    client = _client(
        _payload(spreed={"version": "22.0.17", "features": ["chat-v2", "favorites"]})
    )

    reason = await unmet_capability(client, "alice", "spreed", None, "reactions")

    assert reason is not None
    assert "reactions" in reason


async def test_feature_gate_allows_a_feature_the_app_advertises():
    client = _client(
        _payload(spreed={"version": "22.0.17", "features": ["chat-v2", "reactions"]})
    )

    assert await unmet_capability(client, "alice", "spreed", None, "reactions") is None


@pytest.mark.parametrize("features", [None, "not-a-list", 42])
async def test_feature_gate_fails_open_when_features_are_unreadable(features):
    """An app saying nothing usable about its features must not be gated.

    Same rule the version check follows: close the gate only on a positive
    statement that the instance cannot serve the tool, never on a hunch.
    """
    block: dict = {"version": "22.0.17"}
    if features is not None:
        block["features"] = features
    client = _client(_payload(spreed=block))

    assert await unmet_capability(client, "alice", "spreed", None, "reactions") is None


async def test_feature_and_version_gates_compose():
    """Both conditions apply; failing either closes the gate."""
    client = _client(_payload(spreed={"version": "10.0.0", "features": ["reactions"]}))

    reason = await unmet_capability(client, "alice", "spreed", "13.0.0", "reactions")

    assert reason is not None
    assert "13.0.0" in reason


# The Talk reaction tools' own gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name", ["talk_list_reactions", "talk_react", "talk_remove_reaction"]
)
def test_reaction_tools_declare_the_spreed_reactions_gate(tool_name):
    """The real tools carry the exact gate, not just a gate.

    The machinery is tested above against synthetic functions, and the
    integration suite proves these tools work on an instance that *does*
    advertise `reactions` -- but nothing tied those two together, so "an old
    Talk hides these three tools" was inferred across two files rather than
    asserted. A typo in the app key or the flag would have passed both.
    """
    from mcp.server.mcpserver import MCPServer

    from nextcloud_mcp_server.server.talk import configure_talk_tools

    mcp = MCPServer("test")
    configure_talk_tools(mcp)

    tool = mcp._tool_manager.get_tool(tool_name)

    assert get_required_capability(tool.fn) == ("spreed", None, "reactions")


def test_non_reaction_talk_tools_are_not_feature_gated():
    """Only the reaction tools carry the flag.

    Gating the read/send tools on `reactions` would hide working tools on an
    older Talk -- the failure mode the fail-open contract exists to avoid.
    """
    from mcp.server.mcpserver import MCPServer

    from nextcloud_mcp_server.server.talk import configure_talk_tools

    mcp = MCPServer("test")
    configure_talk_tools(mcp)

    for tool_name in ("talk_list_conversations", "talk_send_message"):
        gate = get_required_capability(mcp._tool_manager.get_tool(tool_name).fn)
        # Either ungated here, or carrying only the module-wide presence gate.
        assert gate is None or gate[2] is None, f"{tool_name} is feature-gated"


async def test_both_conditions_failing_still_closes_the_gate():
    """When feature and version both fail, the gate closes on the feature.

    Precedence is worth pinning rather than leaving to reading order: a caller
    that switched on the wording would otherwise break silently if the checks
    were ever reordered.
    """
    client = _client(_payload(spreed={"version": "10.0.0", "features": ["chat-v2"]}))

    reason = await unmet_capability(client, "alice", "spreed", "13.0.0", "reactions")

    assert reason is not None
    assert "reactions" in reason
