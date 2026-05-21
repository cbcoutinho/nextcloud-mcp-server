"""Unit tests for webhook preset filtering."""

import pytest

from nextcloud_mcp_server.server.webhook_presets import (
    filter_presets_by_installed_apps,
    get_preset,
    list_presets,
)


@pytest.mark.unit
def test_list_all_presets():
    """Test listing all presets returns the full preset catalogue."""
    presets = list_presets()
    assert len(presets) == 6
    preset_ids = [preset_id for preset_id, _ in presets]
    assert "notes_sync" in preset_ids
    assert "calendar_sync" in preset_ids
    assert "tables_sync" in preset_ids
    assert "forms_sync" in preset_ids
    assert "files_sync" in preset_ids
    assert "deck_sync" in preset_ids


@pytest.mark.unit
def test_get_preset_existing():
    """Test getting an existing preset."""
    preset = get_preset("notes_sync")
    assert preset is not None
    assert preset["name"] == "Notes Sync"
    assert preset["app"] == "notes"
    assert len(preset["events"]) == 3


@pytest.mark.unit
def test_get_preset_nonexistent():
    """Test getting a nonexistent preset returns None."""
    preset = get_preset("nonexistent_sync")
    assert preset is None


@pytest.mark.unit
def test_filter_presets_all_apps_installed():
    """Test filtering when all apps are installed."""
    installed_apps = ["notes", "calendar", "tables", "forms", "deck"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    assert len(filtered) == 6  # All 6 presets (files is always included)
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "notes_sync" in preset_ids
    assert "calendar_sync" in preset_ids
    assert "tables_sync" in preset_ids
    assert "forms_sync" in preset_ids
    assert "files_sync" in preset_ids
    assert "deck_sync" in preset_ids


@pytest.mark.unit
def test_filter_presets_deck_included_when_installed():
    """Test that the deck preset is included when the Deck app is installed."""
    installed_apps = ["deck"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "deck_sync" in preset_ids
    assert len(filtered) == 2  # deck + files


@pytest.mark.unit
def test_filter_presets_deck_excluded_when_not_installed():
    """Test that the deck preset is excluded when the Deck app is not installed."""
    installed_apps = ["notes", "calendar", "tables", "forms"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "deck_sync" not in preset_ids


@pytest.mark.unit
def test_get_deck_preset():
    """Test getting the deck_sync preset returns the expected shape."""
    preset = get_preset("deck_sync")
    assert preset is not None
    assert preset["app"] == "deck"
    assert preset["name"] == "Deck Sync"
    assert len(preset["events"]) == 4
    event_classes = [e["event"] for e in preset["events"]]
    assert "OCA\\Deck\\Event\\CardCreatedEvent" in event_classes
    assert "OCA\\Deck\\Event\\CardUpdatedEvent" in event_classes
    assert "OCA\\Deck\\Event\\CardDeletedEvent" in event_classes
    assert "OCA\\Deck\\Event\\BoardUpdatedEvent" in event_classes


@pytest.mark.unit
def test_filter_presets_subset_installed():
    """Test filtering when only some apps are installed."""
    installed_apps = ["notes", "calendar"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    assert len(filtered) == 3  # notes, calendar, files
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "notes_sync" in preset_ids
    assert "calendar_sync" in preset_ids
    assert "files_sync" in preset_ids
    assert "tables_sync" not in preset_ids
    assert "forms_sync" not in preset_ids


@pytest.mark.unit
def test_filter_presets_no_apps_installed():
    """Test filtering when no optional apps are installed."""
    installed_apps = []
    filtered = filter_presets_by_installed_apps(installed_apps)
    assert len(filtered) == 1  # Only files
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "files_sync" in preset_ids
    assert "notes_sync" not in preset_ids
    assert "calendar_sync" not in preset_ids


@pytest.mark.unit
def test_filter_presets_files_always_included():
    """Test that files preset is always included regardless of installed apps."""
    # Empty list
    filtered = filter_presets_by_installed_apps([])
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "files_sync" in preset_ids

    # List with other apps but not explicitly "files"
    filtered = filter_presets_by_installed_apps(["notes", "calendar"])
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "files_sync" in preset_ids


@pytest.mark.unit
def test_filter_presets_forms_included_when_installed():
    """Test that forms preset is included when Forms app is installed."""
    installed_apps = ["forms"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "forms_sync" in preset_ids
    assert len(filtered) == 2  # forms + files


@pytest.mark.unit
def test_filter_presets_forms_excluded_when_not_installed():
    """Test that forms preset is excluded when Forms app is not installed."""
    installed_apps = ["notes", "calendar", "tables"]
    filtered = filter_presets_by_installed_apps(installed_apps)
    preset_ids = [preset_id for preset_id, _ in filtered]
    assert "forms_sync" not in preset_ids
