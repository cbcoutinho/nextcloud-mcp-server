"""Regression test for the lifespan context `task_producer` exposure (Deck #183).

`nc_get_vector_sync_status` reads `lifespan_ctx.task_producer` for postgres-backend
job counts. It was previously a snapshot dataclass field the per-session yields
forgot to populate, so the tool always reported `pending=0` on the postgres
backend. It is now a `@property` that reads the module singleton live (like
`eviction_task_group`); these tests pin that contract.
"""

from typing import cast

import pytest

import nextcloud_mcp_server.app as app_module
from nextcloud_mcp_server.app import AppContext, OAuthAppContext
from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.unit


def test_app_context_task_producer_reads_vector_sync_state(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(app_module._vector_sync_state, "task_producer", sentinel)
    ctx = AppContext(client=cast(NextcloudClient, None))
    assert ctx.task_producer is sentinel


def test_oauth_app_context_task_producer_reads_vector_sync_state(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(app_module._vector_sync_state, "task_producer", sentinel)
    ctx = OAuthAppContext(
        nextcloud_host="https://example.test", token_verifier=object()
    )
    assert ctx.task_producer is sentinel
