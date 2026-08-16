"""Integration tests for the ``/webhooks/nextcloud`` ingress on a running server.

Astrolabe is the only producer of these envelopes (it subscribes to Nextcloud's
change events and POSTs them here), so this exercises the deployed route the way
Astrolabe drives it: bearer secret, real envelope, real routing — rather than the
in-process Starlette app the unit tests build.

Requires the docker compose ``mcp`` service (``docker compose up -d mcp``), which
sets ``WEBHOOK_SECRET`` so the route is mounted at all.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

_MCP_URL = os.getenv("MCP_HTTP_URL", "http://127.0.0.1:8000")
_SECRET = os.getenv("WEBHOOK_SECRET", "dev-webhook-secret-change-me")
_INGRESS = f"{_MCP_URL}/webhooks/nextcloud"


def _envelope(event_class: str, path: str, node_id: int = 987654321) -> dict:
    return {
        "user": {"uid": "admin", "displayName": "admin"},
        "time": 1762850245,
        "event": {
            "class": event_class,
            "node": {"id": node_id, "path": path},
        },
    }


async def test_pdf_write_is_queued():
    """A file event for an indexable file is accepted and queued as a
    ``doc_type="file"`` reconcile task (the processor decides whether the file
    is actually tagged for indexing)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _INGRESS,
            json=_envelope(
                "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
                "/admin/files/Documents/webhook-ingress.pdf",
            ),
            headers={"Authorization": f"Bearer {_SECRET}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["doc_type"] == "file"
    assert body["operation"] == "index"


async def test_note_write_is_queued_as_note():
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _INGRESS,
            json=_envelope(
                "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
                "/admin/files/Notes/webhook-ingress.md",
            ),
            headers={"Authorization": f"Bearer {_SECRET}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["doc_type"] == "note"


async def test_unindexable_file_is_ignored():
    """Vector sync indexes neither images nor arbitrary file types, so those
    events must not enqueue work."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _INGRESS,
            json=_envelope(
                "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
                "/admin/files/Photos/holiday.jpg",
            ),
            headers={"Authorization": f"Bearer {_SECRET}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"


async def test_wrong_secret_is_rejected():
    """GHSA-8vh3-g2qg-2h2c: the ingress trusts ``user.uid``, so an unauthenticated
    caller must never reach the parser."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _INGRESS,
            json=_envelope(
                "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
                "/admin/files/Documents/webhook-ingress.pdf",
            ),
            headers={"Authorization": "Bearer not-the-secret"},
        )

    assert response.status_code == 401
