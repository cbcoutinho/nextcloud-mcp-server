"""
Unit tests for Webhook Storage functionality.

Tests the webhook tracking methods in RefreshTokenStorage without
requiring real database connections or network calls.
"""

import tempfile
import time
from pathlib import Path

import pytest

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


@pytest.fixture
async def temp_storage():
    """Create temporary storage instance for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_webhooks.db"
        # No encryption key needed for webhook tracking
        storage = RefreshTokenStorage(db_path=str(db_path), encryption_key=None)
        await storage.initialize()
        yield storage


async def test_store_webhook(temp_storage):
    """Test storing a webhook."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")

    webhooks = await temp_storage.list_all_webhooks()
    assert len(webhooks) == 1
    assert webhooks[0]["webhook_id"] == 123
    assert webhooks[0]["preset_id"] == "notes_sync"
    assert "created_at" in webhooks[0]


async def test_store_webhook_duplicate(temp_storage):
    """Test storing duplicate webhook replaces existing."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=123, preset_id="calendar_sync")

    webhooks = await temp_storage.list_all_webhooks()
    # Should only have one entry due to UNIQUE constraint
    assert len(webhooks) == 1
    assert webhooks[0]["preset_id"] == "calendar_sync"


async def test_get_webhooks_by_preset(temp_storage):
    """Test retrieving webhooks by preset."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=456, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=789, preset_id="calendar_sync")

    notes_webhooks = await temp_storage.get_webhooks_by_preset("notes_sync")
    assert len(notes_webhooks) == 2
    assert 123 in notes_webhooks
    assert 456 in notes_webhooks

    calendar_webhooks = await temp_storage.get_webhooks_by_preset("calendar_sync")
    assert len(calendar_webhooks) == 1
    assert 789 in calendar_webhooks


async def test_get_webhooks_by_preset_empty(temp_storage):
    """Test retrieving webhooks for non-existent preset."""
    webhooks = await temp_storage.get_webhooks_by_preset("nonexistent")
    assert len(webhooks) == 0


async def test_delete_webhook(temp_storage):
    """Test deleting a webhook."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=456, preset_id="notes_sync")

    deleted = await temp_storage.delete_webhook(webhook_id=123)
    assert deleted is True

    webhooks = await temp_storage.get_webhooks_by_preset("notes_sync")
    assert len(webhooks) == 1
    assert 456 in webhooks


async def test_delete_webhook_nonexistent(temp_storage):
    """Test deleting non-existent webhook."""
    deleted = await temp_storage.delete_webhook(webhook_id=999)
    assert deleted is False


async def test_list_all_webhooks(temp_storage):
    """Test listing all webhooks."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=456, preset_id="calendar_sync")
    await temp_storage.store_webhook(webhook_id=789, preset_id="notes_sync")

    webhooks = await temp_storage.list_all_webhooks()
    assert len(webhooks) == 3

    # Verify all expected fields present
    for webhook in webhooks:
        assert "webhook_id" in webhook
        assert "preset_id" in webhook
        assert "created_at" in webhook

    # Verify webhook IDs
    webhook_ids = [w["webhook_id"] for w in webhooks]
    assert 123 in webhook_ids
    assert 456 in webhook_ids
    assert 789 in webhook_ids


async def test_list_all_webhooks_empty(temp_storage):
    """Test listing webhooks when none exist."""
    webhooks = await temp_storage.list_all_webhooks()
    assert len(webhooks) == 0


async def test_clear_preset_webhooks(temp_storage):
    """Test clearing all webhooks for a preset."""
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=456, preset_id="notes_sync")
    await temp_storage.store_webhook(webhook_id=789, preset_id="calendar_sync")

    deleted_count = await temp_storage.clear_preset_webhooks("notes_sync")
    assert deleted_count == 2

    # Verify notes_sync webhooks are gone
    notes_webhooks = await temp_storage.get_webhooks_by_preset("notes_sync")
    assert len(notes_webhooks) == 0

    # Verify calendar_sync webhook still exists
    calendar_webhooks = await temp_storage.get_webhooks_by_preset("calendar_sync")
    assert len(calendar_webhooks) == 1
    assert 789 in calendar_webhooks


async def test_clear_preset_webhooks_nonexistent(temp_storage):
    """Test clearing webhooks for non-existent preset."""
    deleted_count = await temp_storage.clear_preset_webhooks("nonexistent")
    assert deleted_count == 0


async def test_webhook_timestamps(temp_storage):
    """Test that webhook timestamps are properly stored."""
    start_time = time.time()
    await temp_storage.store_webhook(webhook_id=123, preset_id="notes_sync")
    end_time = time.time()

    webhooks = await temp_storage.list_all_webhooks()
    assert len(webhooks) == 1

    created_at = webhooks[0]["created_at"]
    assert start_time <= created_at <= end_time


async def test_storage_without_encryption_key():
    """Test that storage can be initialized without encryption key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_no_encryption.db"
        storage = RefreshTokenStorage(db_path=str(db_path), encryption_key=None)
        await storage.initialize()

        # Webhook operations should work without encryption key
        await storage.store_webhook(webhook_id=123, preset_id="notes_sync")
        webhooks = await storage.get_webhooks_by_preset("notes_sync")
        assert len(webhooks) == 1
        assert 123 in webhooks


async def test_multiple_presets_independence(temp_storage):
    """Test that different presets maintain independent webhook lists."""
    presets = ["notes_sync", "calendar_sync", "deck_sync", "files_sync"]

    # Store webhooks for each preset
    for i, preset in enumerate(presets):
        webhook_id = 100 + i
        await temp_storage.store_webhook(webhook_id=webhook_id, preset_id=preset)

    # Verify each preset has exactly one webhook
    for i, preset in enumerate(presets):
        webhooks = await temp_storage.get_webhooks_by_preset(preset)
        assert len(webhooks) == 1
        assert (100 + i) in webhooks

    # Clear one preset
    deleted = await temp_storage.clear_preset_webhooks("notes_sync")
    assert deleted == 1

    # Verify other presets unchanged
    for preset in ["calendar_sync", "deck_sync", "files_sync"]:
        webhooks = await temp_storage.get_webhooks_by_preset(preset)
        assert len(webhooks) == 1
