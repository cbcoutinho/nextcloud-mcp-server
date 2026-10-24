"""Deck < 1.18 silently ignores card startdate/color, so the card tools refuse
them up front instead of reporting a write that did nothing."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server import deck as deck_server
from nextcloud_mcp_server.server.deck import _require_card_fields_supported

pytestmark = pytest.mark.unit

TOO_OLD = "it needs the Nextcloud 'deck' app >= 1.18.0, but this instance has 1.17.8"


def _patch_gate(mocker, reason):
    return mocker.patch.object(
        deck_server, "unmet_capability", mocker.AsyncMock(return_value=reason)
    )


async def test_rejects_new_fields_on_old_deck(mocker):
    _patch_gate(mocker, TOO_OLD)
    client = mocker.Mock(username="alice")

    with pytest.raises(ToolError, match=r"startdate and color cannot be set.*1\.17\.8"):
        await _require_card_fields_supported(client, "2030-01-01T00:00:00Z", "FF0000")


async def test_clearing_on_old_deck_is_also_rejected(mocker):
    """``""`` is still an explicit request for the field."""
    _patch_gate(mocker, TOO_OLD)
    client = mocker.Mock(username="alice")

    with pytest.raises(ToolError, match=r"^color cannot be set"):
        await _require_card_fields_supported(client, None, "")


async def test_allows_when_supported_or_unknown(mocker):
    """``unmet_capability`` returns None both when Deck is new enough and when
    capabilities are unavailable (fail-open)."""
    gate = _patch_gate(mocker, None)
    client = mocker.Mock(username="alice")

    await _require_card_fields_supported(client, "2030-01-01T00:00:00Z", None)

    gate.assert_awaited_once_with(client, "alice", "deck", "1.18.0")


async def test_skips_the_lookup_when_no_new_field_is_passed(mocker):
    gate = _patch_gate(mocker, TOO_OLD)

    await _require_card_fields_supported(mocker.Mock(username="alice"), None, None)

    gate.assert_not_awaited()
