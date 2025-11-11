"""Webhook preset configurations for common sync scenarios.

This module defines pre-configured webhook bundles that simplify
webhook setup for common use cases like Notes sync, Calendar sync, etc.
"""

from typing import Any, Dict, List, TypedDict


class WebhookEventConfig(TypedDict):
    """Configuration for a single webhook event."""

    event: str  # Fully qualified event class name
    filter: Dict[str, Any]  # Event filter (optional)


class WebhookPreset(TypedDict):
    """Definition of a webhook preset."""

    name: str  # Display name
    description: str  # User-friendly description
    events: List[WebhookEventConfig]  # List of events to register
    app: str  # Nextcloud app this preset is for


# File/Notes webhook events
FILE_EVENT_CREATED = "OCP\\Files\\Events\\Node\\NodeCreatedEvent"
FILE_EVENT_WRITTEN = "OCP\\Files\\Events\\Node\\NodeWrittenEvent"
# Use BeforeNodeDeletedEvent instead of NodeDeletedEvent to get node.id
# See: https://github.com/nextcloud/server/issues/56371
FILE_EVENT_DELETED = "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent"

# Calendar webhook events
CALENDAR_EVENT_CREATED = "OCP\\Calendar\\Events\\CalendarObjectCreatedEvent"
CALENDAR_EVENT_UPDATED = "OCP\\Calendar\\Events\\CalendarObjectUpdatedEvent"
CALENDAR_EVENT_DELETED = "OCP\\Calendar\\Events\\CalendarObjectDeletedEvent"

# Tables webhook events (Nextcloud 30+)
TABLES_EVENT_ROW_ADDED = "OCA\\Tables\\Event\\RowAddedEvent"
TABLES_EVENT_ROW_UPDATED = "OCA\\Tables\\Event\\RowUpdatedEvent"
TABLES_EVENT_ROW_DELETED = "OCA\\Tables\\Event\\RowDeletedEvent"

# Forms webhook events (Nextcloud 30+)
FORMS_EVENT_FORM_SUBMITTED = "OCA\\Forms\\Events\\FormSubmittedEvent"

# NOTE: Deck and Contacts do NOT support webhooks
# Their event classes do not implement IWebhookCompatibleEvent interface.
# Alternative sync strategies:
# - Deck: Use polling with ETag-based change detection
# - Contacts: Use CardDAV sync-token mechanism for efficient syncing


WEBHOOK_PRESETS: Dict[str, WebhookPreset] = {
    "notes_sync": {
        "name": "Notes Sync",
        "description": "Real-time synchronization for Notes app (create, update, delete)",
        "app": "notes",
        "events": [
            {
                "event": FILE_EVENT_CREATED,
                "filter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"},
            },
            {
                "event": FILE_EVENT_WRITTEN,
                "filter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"},
            },
            {
                "event": FILE_EVENT_DELETED,
                "filter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"},
            },
        ],
    },
    "calendar_sync": {
        "name": "Calendar Sync",
        "description": "Real-time synchronization for Calendar events (create, update, delete)",
        "app": "calendar",
        "events": [
            {
                "event": CALENDAR_EVENT_CREATED,
                "filter": {},
            },
            {
                "event": CALENDAR_EVENT_UPDATED,
                "filter": {},
            },
            {
                "event": CALENDAR_EVENT_DELETED,
                "filter": {},
            },
        ],
    },
    "tables_sync": {
        "name": "Tables Sync",
        "description": "Real-time synchronization for Tables rows (add, update, delete)",
        "app": "tables",
        "events": [
            {
                "event": TABLES_EVENT_ROW_ADDED,
                "filter": {},
            },
            {
                "event": TABLES_EVENT_ROW_UPDATED,
                "filter": {},
            },
            {
                "event": TABLES_EVENT_ROW_DELETED,
                "filter": {},
            },
        ],
    },
    "forms_sync": {
        "name": "Forms Sync",
        "description": "Real-time synchronization for Forms submissions",
        "app": "forms",
        "events": [
            {
                "event": FORMS_EVENT_FORM_SUBMITTED,
                "filter": {},
            },
        ],
    },
    "files_sync": {
        "name": "All Files Sync",
        "description": "Real-time synchronization for all file operations (create, update, delete)",
        "app": "files",
        "events": [
            {
                "event": FILE_EVENT_CREATED,
                "filter": {},
            },
            {
                "event": FILE_EVENT_WRITTEN,
                "filter": {},
            },
            {
                "event": FILE_EVENT_DELETED,
                "filter": {},
            },
        ],
    },
}


def get_preset(preset_id: str) -> WebhookPreset | None:
    """Get a webhook preset by ID.

    Args:
        preset_id: Preset identifier (e.g., "notes_sync", "calendar_sync")

    Returns:
        Webhook preset configuration or None if not found
    """
    return WEBHOOK_PRESETS.get(preset_id)


def list_presets() -> List[tuple[str, WebhookPreset]]:
    """Get all available webhook presets.

    Returns:
        List of (preset_id, preset_config) tuples
    """
    return list(WEBHOOK_PRESETS.items())


def get_preset_events(preset_id: str) -> List[str]:
    """Get list of event class names for a preset.

    Args:
        preset_id: Preset identifier

    Returns:
        List of fully qualified event class names
    """
    preset = get_preset(preset_id)
    if not preset:
        return []
    return [event_config["event"] for event_config in preset["events"]]


def filter_presets_by_installed_apps(
    installed_apps: list[str],
) -> List[tuple[str, WebhookPreset]]:
    """Filter webhook presets to only show those for installed apps.

    Args:
        installed_apps: List of installed app names (e.g., ["notes", "calendar", "forms"])

    Returns:
        List of (preset_id, preset_config) tuples for presets whose apps are installed
    """
    filtered = []
    for preset_id, preset in WEBHOOK_PRESETS.items():
        app_name = preset["app"]
        # "files" is always available (core functionality)
        if app_name == "files" or app_name in installed_apps:
            filtered.append((preset_id, preset))
    return filtered
