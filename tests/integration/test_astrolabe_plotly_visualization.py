"""Integration test for Astrolabe Plotly 3D visualization with multi-user BasicAuth mode.

Cross-system interface test: Tests the MCP server's integration with the
Astrolabe Nextcloud app, which is installed from the Nextcloud app store via
app-hooks/post-installation/20-install-astrolabe-app.sh. Astrolabe source
lives in a separate repository (https://github.com/cbcoutinho/astrolabe).

This test verifies that:
1. User can provision background sync access via app password
2. Content created via MCP tools is indexed by vector sync
3. Semantic search via Astrolabe UI returns results
4. Plotly 3D visualization container renders correctly

Requires:
- docker-compose up -d app db mcp-multi-user-basic
- ENABLE_SEMANTIC_SEARCH=true on the mcp-multi-user-basic container
"""

import base64
import json
import logging
import re
import uuid

import anyio
import pytest
from playwright.async_api import Page

# Import helper functions from existing test
from tests.conftest import create_mcp_client_session
from tests.integration.test_astrolabe_multi_user_background_sync import (
    complete_astrolabe_authorization,
    login_to_nextcloud,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.multi_user_basic]


async def wait_for_vector_sync(
    mcp_client, initial_indexed_count: int, timeout_seconds: int = 60
) -> tuple[bool, dict | None]:
    """Wait for vector sync to index new content.

    Args:
        mcp_client: MCP client session
        initial_indexed_count: Initial indexed document count before creating content
        timeout_seconds: Maximum time to wait for sync

    Returns:
        Tuple of (success, status_data)
    """
    wait_interval = 2
    waited = 0
    status_data = None

    while waited < timeout_seconds:
        sync_status = await mcp_client.call_tool("nc_get_vector_sync_status", {})
        if sync_status.isError:
            logger.warning("Vector sync status error: %s", sync_status)
            return False, None

        status_data = json.loads(sync_status.content[0].text)
        indexed_count = status_data.get("indexed_count", 0)
        pending_count = status_data.get("pending_count", 1)

        logger.info(
            "Sync status at %ss: indexed=%s, pending=%s, status=%s",
            waited,
            indexed_count,
            pending_count,
            status_data.get("status"),
        )

        if indexed_count > initial_indexed_count and pending_count == 0:
            logger.info(
                "✓ Sync complete: %s documents indexed (was %s)",
                indexed_count,
                initial_indexed_count,
            )
            return True, status_data

        await anyio.sleep(wait_interval)
        waited += wait_interval

    return False, status_data


async def navigate_to_astrolabe_main(page: Page):
    """Navigate to Astrolabe main app page (Semantic Search section).

    Args:
        page: Playwright page instance (must be authenticated)
    """
    nextcloud_url = "http://localhost:8080"

    logger.info("Navigating to Astrolabe main app...")
    await page.goto(f"{nextcloud_url}/apps/astrolabe", wait_until="networkidle")

    # Wait for the app to load
    await anyio.sleep(1)

    logger.info("✓ Successfully loaded Astrolabe main app")


@pytest.mark.integration
@pytest.mark.multi_user_basic
@pytest.mark.timeout(
    300
)  # 5 minutes - this test involves app-password provisioning + vector sync
async def test_astrolabe_plotly_visualization_with_basic_auth(
    browser,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test Plotly 3D visualization in Astrolabe with multi-user BasicAuth mode.

    This test:
    1. Configures Astrolabe for the mcp-multi-user-basic service
    2. Provisions background sync access for alice via app password
    3. Creates a note with unique searchable content (as alice)
    4. Waits for vector sync to index the note
    5. Performs semantic search in Astrolabe UI
    6. Verifies the Plotly visualization renders and results are displayed
    """
    # Phase 1: Configure Astrolabe for mcp-multi-user-basic
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    username = "alice"
    password = test_users_setup[username]["password"]
    note_id = None
    unique_term = None

    # Create MCP client with alice's credentials for the multi-user BasicAuth server
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode("utf-8")
    auth_header = f"Basic {credentials}"

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Phase 2: Provision background indexing (app-password opt-in; no OAuth)
        await login_to_nextcloud(page, username, password)
        auth_result = await complete_astrolabe_authorization(page, username, password)
        logger.info("Authorization result: %s", auth_result)

        # Create MCP client session as alice - all MCP operations inside this block
        async with create_mcp_client_session(
            url="http://localhost:8003/mcp",
            headers={"Authorization": auth_header},
            client_name="Alice BasicAuth MCP",
        ) as alice_mcp_client:
            # Phase 3: Get initial indexed count
            initial_sync = await alice_mcp_client.call_tool(
                "nc_get_vector_sync_status", {}
            )

            if initial_sync.isError:
                pytest.skip("Vector sync not enabled on mcp-multi-user-basic")

            initial_data = json.loads(initial_sync.content[0].text)
            initial_count = initial_data.get("indexed_count", 0)
            logger.info("Initial indexed count: %s", initial_count)

            # Create note with unique searchable term
            unique_term = f"plotly_viz_test_{uuid.uuid4().hex[:8]}"
            note_response = await alice_mcp_client.call_tool(
                "nc_notes_create_note",
                {
                    "title": f"Visualization Test Note {unique_term}",
                    "content": f"""# Testing Plotly Visualization

This note contains the unique term: {unique_term}

It is used to test the 3D vector space visualization in the Astrolabe app.
The visualization should show this document as a point in PCA-reduced space.

## Key Features
- Semantic search with embeddings
- PCA dimension reduction to 3D
- Interactive Plotly scatter3d plot
""",
                    "category": "Test",
                },
            )

            if note_response.isError:
                pytest.fail(f"Failed to create test note: {note_response}")

            note_data = json.loads(note_response.content[0].text)
            note_id = note_data.get("id")
            logger.info("Created test note ID: %s", note_id)

            # Phase 4: Wait for vector indexing
            sync_complete, status = await wait_for_vector_sync(
                alice_mcp_client, initial_count, timeout_seconds=90
            )
            assert sync_complete, f"Vector sync did not complete in time: {status}"

            # Phase 5: Navigate to Astrolabe and perform search
            await navigate_to_astrolabe_main(page)

            # Fill search query - find the Astrolabe search input specifically
            # The NcTextField component wraps the input in a div with class mcp-search-input
            search_input = page.locator(".mcp-search-input input")
            await search_input.wait_for(timeout=10000, state="visible")
            await search_input.fill(unique_term)
            logger.info("Entered search query: %s", unique_term)

            # Trigger search by pressing Enter on the input field
            # This is wired to performSearch via @keyup.enter in the Vue component
            await search_input.press("Enter")
            logger.info("Pressed Enter to trigger search")

            # Wait for loading to complete - watch for loading indicator to disappear
            loading_indicator = page.locator(".mcp-loading")
            try:
                # If loading indicator appears, wait for it to disappear
                if await loading_indicator.count() > 0:
                    await loading_indicator.wait_for(state="hidden", timeout=30000)
                    logger.info("Loading completed")
            except Exception:
                # Loading might be too fast to catch
                pass

            # Brief wait for UI to settle
            await anyio.sleep(1)

            # Take diagnostic screenshot
            await page.screenshot(path="/tmp/astrolabe_search_after_click.png")
            logger.info(
                "Took diagnostic screenshot: /tmp/astrolabe_search_after_click.png"
            )

            # Wait for search results using text-based detection
            # This is more reliable than class-based selectors
            # The UI shows "N results" when search completes successfully
            results_text_pattern = page.get_by_text(re.compile(r"\d+ results?"))
            no_results_text = page.get_by_text("No results found")
            error_note = page.locator(".mcp-error")

            # Wait for one of: results count, no results message, or error
            try:
                # Poll for results or error states (don't rely on Nextcloud core CSS classes)
                found_state = False
                for attempt in range(60):  # 60 attempts, 500ms each = 30s total
                    if await error_note.count() > 0:
                        error_text = await error_note.text_content()
                        logger.error("Search error: %s", error_text)
                        pytest.fail(f"Search failed with error: {error_text}")

                    if await no_results_text.count() > 0:
                        logger.warning(
                            "No results found - vector sync may not have completed"
                        )
                        await page.screenshot(path="/tmp/astrolabe_no_results.png")
                        pytest.fail(
                            f"Search returned no results for '{unique_term}'. "
                            "Check if vector sync completed for alice's content."
                        )

                    if await results_text_pattern.count() > 0:
                        results_text = await results_text_pattern.first.text_content()
                        logger.info("Found results: %s", results_text)
                        found_state = True
                        break

                    if attempt % 10 == 0:
                        logger.info(
                            "Waiting for results... (attempt %s/60)", attempt + 1
                        )

                    await anyio.sleep(0.5)

                if not found_state:
                    await page.screenshot(path="/tmp/astrolabe_search_timeout.png")
                    page_content = await page.content()
                    logger.error("Search state not resolved. Page URL: %s", page.url)
                    logger.error("Page content snippet: %s", page_content[:2000])
                    raise AssertionError("Search did not complete within timeout")

            except AssertionError:
                raise  # Re-raise AssertionError as-is
            except Exception as e:
                # Take another screenshot and get page content for debugging
                await page.screenshot(path="/tmp/astrolabe_search_timeout.png")
                page_content = await page.content()
                logger.error("Search state not resolved. Page URL: %s", page.url)
                logger.error("Page content snippet: %s", page_content[:2000])
                raise AssertionError(f"Search did not complete: {e}")

            logger.info("Results loaded")

            # Phase 6: Verify visualization
            # Check Plotly container is visible
            viz_plot = page.locator("#viz-plot")
            await viz_plot.wait_for(timeout=15000, state="visible")
            logger.info("Plotly container is visible")

            # Verify Plotly has rendered content (SVG/canvas elements inside)
            has_viz_content = await page.evaluate(
                """
                () => {
                    const plot = document.getElementById('viz-plot');
                    if (!plot) return false;
                    // Plotly creates .plotly class, canvas, or svg elements
                    return plot.children.length > 0 ||
                           plot.querySelector('.plotly, canvas, svg, .main-svg') !== null;
                }
            """
            )
            assert has_viz_content, "Plotly visualization did not render any content"
            logger.info("✓ Plotly visualization rendered content")

            # Verify results are displayed
            result_items = page.locator(".mcp-result-item")
            result_count = await result_items.count()
            assert result_count > 0, "No search results displayed"
            logger.info("✓ Found %s search result(s)", result_count)

            # Verify our note appears in results
            found_note = False
            for i in range(result_count):
                item = result_items.nth(i)
                title_elem = item.locator(".mcp-result-title")
                title_text = await title_elem.text_content()
                if title_text and unique_term in title_text:
                    found_note = True
                    logger.info("✓ Found test note in results: %s", title_text)
                    break

            assert found_note, f"Created note with '{unique_term}' not found in results"

            # Optional: Take screenshot for verification
            await page.screenshot(path="/tmp/astrolabe_plotly_test_success.png")
            logger.info("✓ All Plotly visualization assertions passed")

            # Cleanup: delete the created note (inside the MCP client context)
            if note_id:
                try:
                    delete_response = await alice_mcp_client.call_tool(
                        "nc_notes_delete_note", {"note_id": note_id}
                    )
                    if not delete_response.isError:
                        logger.info("✓ Cleaned up test note %s", note_id)
                        note_id = None  # Mark as cleaned
                    else:
                        logger.warning(
                            "Failed to delete note %s: %s", note_id, delete_response
                        )
                except Exception as e:
                    logger.warning("Cleanup failed for note %s: %s", note_id, e)

    finally:
        # Cleanup note if not already cleaned (create new client for cleanup)
        if note_id:
            try:
                async with create_mcp_client_session(
                    url="http://localhost:8003/mcp",
                    headers={"Authorization": auth_header},
                    client_name="Cleanup MCP",
                ) as cleanup_client:
                    delete_response = await cleanup_client.call_tool(
                        "nc_notes_delete_note", {"note_id": note_id}
                    )
                    if not delete_response.isError:
                        logger.info("✓ Cleaned up test note %s (finally)", note_id)
                    else:
                        logger.warning(
                            "Failed to delete note %s: %s", note_id, delete_response
                        )
            except Exception as e:
                logger.warning("Cleanup failed for note %s: %s", note_id, e)

        # Close browser context
        await context.close()
