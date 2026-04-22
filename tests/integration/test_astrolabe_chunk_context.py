"""Integration test for the chunk-context HTTP path in multi-user BasicAuth mode.

Cross-system interface test: Tests the MCP server's /api/v1/chunk-context
handler via the Astrolabe PHP app (/apps/astrolabe/api/chunk-context). Astrolabe
source lives in ./third_party/astrolabe (submodule) and is installed during
test setup by app-hooks/post-installation/20-install-astrolabe-app.sh.

Regression test: Prior to this test, `get_chunk_context` in
nextcloud_mcp_server/api/visualization.py forwarded the OAuth bearer token
directly to Nextcloud via NextcloudClient.from_token(...). In multi-user
BasicAuth deployments, Nextcloud doesn't validate that bearer on the Notes
API, so the handler returned 404 (wrapped by Astrolabe as 500). This test
exercises the full chain:

    browser session → astrolabe → MCP server (OAuth bearer) →
        get_user_client_basic_auth → Nextcloud (app password BasicAuth) → note

so a regression to from_token-style auth would surface as a 500/404 from
Astrolabe instead of a 200 with chunk_text.
"""

import base64
import json
import logging
import re
import uuid

import httpx
import pytest

from tests.conftest import create_mcp_client_session
from tests.integration.test_astrolabe_multi_user_background_sync import (
    complete_astrolabe_authorization,
    login_to_nextcloud,
)
from tests.integration.test_astrolabe_plotly_visualization import wait_for_vector_sync

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.multi_user_basic]


def _build_basic_auth_header(username: str, password: str) -> str:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode("utf-8")
    return f"Basic {credentials}"


@pytest.mark.timeout(300)
async def test_chunk_context_endpoint_uses_app_password(
    browser,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Astrolabe /api/chunk-context must return 200 with populated chunk_text.

    Covers the regression where the MCP handler used BearerAuth against
    Nextcloud instead of BasicAuth with the stored app password.
    """
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    username = "alice"
    password = test_users_setup[username]["password"]
    note_id = None
    unique_term = f"chunk_ctx_test_{uuid.uuid4().hex[:8]}"

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await login_to_nextcloud(page, username, password)
        auth_result = await complete_astrolabe_authorization(page, username, password)
        assert auth_result["step1"], "OAuth authorization did not complete"
        assert auth_result["step2"], "App password provisioning did not complete"

        auth_header = _build_basic_auth_header(username, password)
        async with create_mcp_client_session(
            url="http://localhost:8003/mcp",
            headers={"Authorization": auth_header},
            client_name="Alice Chunk Context Test",
        ) as mcp_client:
            initial_sync = await mcp_client.call_tool("nc_get_vector_sync_status", {})
            if initial_sync.isError:
                pytest.skip("Vector sync not enabled on mcp-multi-user-basic")
            initial_count = json.loads(initial_sync.content[0].text).get(
                "indexed_count", 0
            )

            note_body = (
                f"# Chunk Context Regression Test\n\n"
                f"This document exists to verify that the chunk-context HTTP "
                f"endpoint can re-fetch it after indexing. "
                f"Unique marker: {unique_term}.\n\n"
                f"Paragraph two exists so there is a plausible surrounding "
                f"context to slice. Lorem ipsum dolor sit amet, consectetur "
                f"adipiscing elit."
            )
            note_response = await mcp_client.call_tool(
                "nc_notes_create_note",
                {
                    "title": f"Chunk Context Test {unique_term}",
                    "content": note_body,
                    "category": "Test",
                },
            )
            assert not note_response.isError, f"Create note failed: {note_response}"
            note_id = json.loads(note_response.content[0].text).get("id")
            assert note_id is not None

            sync_complete, status = await wait_for_vector_sync(
                mcp_client, initial_count, timeout_seconds=90
            )
            assert sync_complete, f"Vector sync did not complete: {status}"

        # Use the browser's session to drive Astrolabe end-to-end, the way a
        # real user would: this exercises astrolabe's OAuth token retrieval
        # and the MCP server's handler under a real bearer. Nextcloud's
        # controllers require a CSRF token (`requesttoken` header) that the
        # rendered SPA picks up from `OC.requestToken`; direct page.request
        # calls don't get it for free, so we load an Astrolabe page and
        # forward the token on each subsequent API call.
        await page.goto(
            "http://localhost:8080/apps/astrolabe/", wait_until="networkidle"
        )
        request_token = await page.evaluate("window.OC && OC.requestToken")
        assert request_token, (
            "Could not read OC.requestToken from Astrolabe page — is the user logged in?"
        )
        csrf_headers = {"requesttoken": request_token}

        search_resp = await page.request.get(
            f"http://localhost:8080/apps/astrolabe/api/search"
            f"?query={unique_term}&algorithm=hybrid&limit=5&include_pca=false",
            headers=csrf_headers,
        )
        assert search_resp.ok, (
            f"Astrolabe search failed: {search_resp.status} {await search_resp.text()}"
        )
        search_data = await search_resp.json()
        assert search_data.get("success"), f"Search returned error: {search_data}"
        results = search_data.get("results") or []
        note_result = next(
            (
                r
                for r in results
                if r.get("doc_type") == "note" and str(r.get("id")) == str(note_id)
            ),
            None,
        )
        assert note_result is not None, (
            f"Note {note_id} with term '{unique_term}' not found in search results: "
            f"{results}"
        )
        start = note_result.get("chunk_start_offset")
        end = note_result.get("chunk_end_offset")
        assert start is not None and end is not None, (
            f"Search result missing chunk offsets: {note_result}"
        )

        chunk_resp = await page.request.get(
            "http://localhost:8080/apps/astrolabe/api/chunk-context",
            params={
                "doc_type": "note",
                "doc_id": str(note_id),
                "start": str(start),
                "end": str(end),
            },
            headers=csrf_headers,
        )
        assert chunk_resp.status == 200, (
            f"chunk-context returned {chunk_resp.status}, body: "
            f"{await chunk_resp.text()}"
        )
        chunk_data = await chunk_resp.json()
        assert chunk_data.get("success") is True, f"Response: {chunk_data}"
        chunk_text = chunk_data.get("chunk_text") or ""
        assert chunk_text, f"Empty chunk_text in response: {chunk_data}"
        # The unique marker is in the indexed body, so it must appear in the
        # chunk text or in the surrounding context.
        combined = (
            chunk_text
            + chunk_data.get("before_context", "")
            + chunk_data.get("after_context", "")
        )
        assert re.search(re.escape(unique_term), combined), (
            f"Unique term {unique_term} missing from chunk+context: "
            f"chunk_text={chunk_text!r}, before={chunk_data.get('before_context')!r}, "
            f"after={chunk_data.get('after_context')!r}"
        )
    finally:
        try:
            if note_id is not None:
                auth_header = _build_basic_auth_header(username, password)
                async with create_mcp_client_session(
                    url="http://localhost:8003/mcp",
                    headers={"Authorization": auth_header},
                    client_name="Alice Chunk Context Cleanup",
                ) as mcp_client:
                    await mcp_client.call_tool(
                        "nc_notes_delete_note", {"note_id": note_id}
                    )
        except Exception as cleanup_err:
            logger.warning(f"Cleanup failed for note {note_id}: {cleanup_err}")
        await context.close()


@pytest.mark.timeout(60)
async def test_chunk_context_endpoint_requires_authentication():
    """Direct HTTP hit at /api/v1/chunk-context without a bearer must 401."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "http://localhost:8003/api/v1/chunk-context",
            params={
                "doc_type": "note",
                "doc_id": "1",
                "start": "0",
                "end": "10",
            },
        )
        assert response.status_code == 401, (
            f"Expected 401 without auth, got {response.status_code}: {response.text}"
        )


@pytest.mark.timeout(60)
async def test_chunk_context_endpoint_rejects_invalid_bearer():
    """A syntactically-valid-but-unverifiable bearer must not 500.

    The NotProvisionedError path (handler reached but no stored app password)
    is covered by the corresponding unit test. This check guards the other
    rejection path: token validation fails upfront at
    `validate_token_and_get_user`, which must turn into a clean 401/404 from
    the HTTP layer, not an opaque 500.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "http://localhost:8003/api/v1/chunk-context",
            params={
                "doc_type": "note",
                "doc_id": "1",
                "start": "0",
                "end": "10",
            },
            headers={"Authorization": "Bearer invalid.token.value"},
        )
        assert response.status_code in (401, 404), (
            f"Expected 401/404 for invalid bearer, got {response.status_code}: "
            f"{response.text}"
        )
