from typing import Callable

from .calendar import configure_calendar_tools
from .collectives import configure_collectives_tools
from .contacts import configure_contacts_tools
from .cookbook import configure_cookbook_tools
from .deck import configure_deck_tools
from .news import configure_news_tools
from .notes import configure_notes_tools
from .semantic import configure_semantic_tools
from .sharing import configure_sharing_tools
from .tables import configure_tables_tools
from .webdav import configure_webdav_tools

# Canonical mapping of app name → tool registration function.
# Used by app.py (HTTP), stdio.py (stdio), and cli.py (--enable-app choices).
# Semantic search is excluded here because it is a cross-app feature gated
# by VECTOR_SYNC_ENABLED, not an individual Nextcloud app.
AVAILABLE_APPS: dict[str, Callable] = {
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
}

__all__ = [
    "AVAILABLE_APPS",
    "configure_calendar_tools",
    "configure_collectives_tools",
    "configure_contacts_tools",
    "configure_cookbook_tools",
    "configure_deck_tools",
    "configure_news_tools",
    "configure_notes_tools",
    "configure_semantic_tools",
    "configure_sharing_tools",
    "configure_tables_tools",
    "configure_webdav_tools",
]
