from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.capabilities import stamp_required_capability

from .calendar import configure_calendar_tools
from .collectives import configure_collectives_tools
from .contacts import configure_contacts_tools
from .cookbook import configure_cookbook_tools
from .deck import configure_deck_tools
from .mail import configure_mail_tools
from .news import configure_news_tools
from .notes import configure_notes_tools
from .semantic import configure_semantic_tools
from .sharing import configure_sharing_tools
from .tables import configure_tables_tools
from .talk import configure_talk_tools
from .webdav import configure_webdav_tools

# Canonical mapping of app name → tool registration function.
# Used by app.py (HTTP), stdio.py (stdio), and cli.py (--enable-app choices).
# Semantic search is excluded here because it is a cross-app feature gated
# by VECTOR_SYNC_ENABLED, not an individual Nextcloud app.
AVAILABLE_APPS: dict[str, Callable[[MCPServer], None]] = {
    "notes": configure_notes_tools,
    "tables": configure_tables_tools,
    "webdav": configure_webdav_tools,
    "sharing": configure_sharing_tools,
    "calendar": configure_calendar_tools,
    "collectives": configure_collectives_tools,
    "contacts": configure_contacts_tools,
    "cookbook": configure_cookbook_tools,
    "deck": configure_deck_tools,
    "news": configure_news_tools,
    "mail": configure_mail_tools,
    "talk": configure_talk_tools,
}

# App name → the key it publishes on /ocs/v2.php/cloud/capabilities, for apps
# whose absence should hide their tools. Verified against each app's
# Capabilities.php; an app missing from this map is NEVER gated, because
# absence of a key is what closes the gate and most apps publish nothing:
#
#   notes / tables / deck / cookbook → own key, carries `version`
#   talk                             → `spreed`; the whole block is omitted for
#                                      a user Talk is disabled for, which is
#                                      exactly when its tools should vanish
#   calendar (`calendar`, no version) and contacts (nested under
#     `client_integration`) are deliberately absent: those tools speak
#     CalDAV/CardDAV and keep working with the web app uninstalled
#   collectives / news / mail        → publish no capability block at all
#   webdav / sharing                 → core `files`/`files_sharing`, always there
APP_CAPABILITY_KEY: dict[str, str] = {
    "notes": "notes",
    "tables": "tables",
    "deck": "deck",
    "cookbook": "cookbook",
    "talk": "spreed",
}


def configure_app_tools(mcp: MCPServer, app_name: str) -> None:
    """Register one app's tools, gated on the app being installed for the user.

    Used by both transports (app.py, stdio.py) so their tool sets cannot drift.
    Tools that declare their own ``@require_capability`` (e.g. a version floor)
    keep it — see ``stamp_required_capability``.
    """
    before = {tool.name for tool in mcp._tool_manager.list_tools()}
    AVAILABLE_APPS[app_name](mcp)

    capability = APP_CAPABILITY_KEY.get(app_name)
    if capability is None:
        return
    for tool in mcp._tool_manager.list_tools():
        if tool.name not in before:
            stamp_required_capability(tool.fn, capability)


__all__ = [
    "APP_CAPABILITY_KEY",
    "AVAILABLE_APPS",
    "configure_app_tools",
    "configure_calendar_tools",
    "configure_collectives_tools",
    "configure_contacts_tools",
    "configure_cookbook_tools",
    "configure_deck_tools",
    "configure_mail_tools",
    "configure_news_tools",
    "configure_notes_tools",
    "configure_semantic_tools",
    "configure_sharing_tools",
    "configure_tables_tools",
    "configure_talk_tools",
    "configure_webdav_tools",
]
