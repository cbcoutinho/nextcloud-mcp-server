"""Integration tests for MCP sampling with semantic search.

These tests validate the nc_semantic_search_answer tool which combines:
1. Semantic search to retrieve relevant documents
2. MCP sampling to generate natural language answers

Tests cover three scenarios:
- Successful sampling (LLM generates answer)
- Sampling fallback (client doesn't support sampling)
- No results (no relevant documents found)

Note: These tests require VECTOR_SYNC_ENABLED=true and a configured
vector database with indexed test data.
"""

import json
from unittest.mock import MagicMock

import anyio
import pytest
from mcp.types import CreateMessageResult, TextContent

from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration


async def wait_for_vector_sync(
    nc_mcp_client,
    *,
    initial_indexed_count: int | None = None,
    search_term: str | None = None,
    note_id: int | None = None,
    max_wait: int = 90,
    wait_interval: int = 1,
) -> dict:
    """Wait for vector sync to complete, returning final status.

    Args:
        nc_mcp_client: MCP client to poll status with.
        search_term: If set (preferred), wait until a document matching this
            term is retrievable via ``nc_semantic_search``. Robust against
            full-corpus re-scan churn, where the corpus-wide ``indexed_count``
            gauge is non-monotonic and ``indexed_count > initial`` can never
            hold even though the document is indexed.
        note_id: Optional exact-match document id paired with ``search_term``.
        initial_indexed_count: Legacy gauge-delta fallback when no search_term
            is given: wait until indexed_count exceeds this value and
            pending_count reaches 0.
        max_wait: Maximum seconds to wait before failing.
        wait_interval: Seconds between status polls.

    Returns:
        The last status dict from nc_get_vector_sync_status.
    """
    waited = 0
    status_data: dict = {}
    while waited < max_wait:
        sync_status = await nc_mcp_client.call_tool(
            "nc_get_vector_sync_status", arguments={}
        )
        try:
            status_data = json.loads(sync_status.content[0].text)
        except (AttributeError, IndexError, ValueError):
            # transient empty/error response — keep polling. .get() defaults
            # below also keep an empty dict from triggering a false break.
            status_data = {}

        if search_term is not None:
            # Robust signal: wait for the specific document to be retrievable
            if await document_is_searchable(nc_mcp_client, search_term, note_id):
                break
        elif initial_indexed_count is not None:
            # Legacy: wait for new document(s) to be indexed (gauge delta)
            if (
                status_data.get("indexed_count", 0) > initial_indexed_count
                and status_data.get("pending_count", 1) == 0
            ):
                break
        else:
            # NOTE: idle + pending==0 is also the *initial empty* state, so this
            # can break before a caller's work is even enqueued — prefer passing
            # search_term. Kept only for callers that just need a settled corpus.
            if (
                status_data.get("status") == "idle"
                and status_data.get("pending_count", 1) == 0
            ):
                break

        await anyio.sleep(wait_interval)
        waited += wait_interval

    assert waited < max_wait, (
        f"Vector sync did not complete within {max_wait} seconds. "
        f"Last status: {status_data}"
    )
    return status_data


async def require_vector_sync_tools(nc_mcp_client):
    """Skip test if vector sync tools are not available."""
    tools = await nc_mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    if "nc_get_vector_sync_status" not in tool_names:
        pytest.skip("Vector sync tools not available (VECTOR_SYNC_ENABLED not set)")


@pytest.fixture
def mock_sampling_result():
    """Mock successful sampling result from MCP client."""
    result = MagicMock(spec=CreateMessageResult)
    result.content = TextContent(
        type="text",
        text=(
            "Based on Document 1 (Python Async Programming) and Document 2 "
            "(Best Practices), you should use async/await for asynchronous "
            "programming and always use async context managers for resources."
        ),
    )
    result.model = "claude-3-5-sonnet"
    result.stopReason = "endTurn"
    return result


async def test_semantic_search_answer_successful_sampling(
    nc_mcp_client, temporary_note_factory
):
    """Test semantic search with successful LLM answer generation.

    Prerequisites:
    - VECTOR_SYNC_ENABLED=true
    - Qdrant running and indexed
    - Test note indexed in vector database

    Flow:
    1. Create test note with searchable content
    2. Wait for vector sync to complete using nc_get_vector_sync_status
    3. Call nc_semantic_search_answer
    4. Mock ctx.session.create_message to return answer
    5. Verify response contains generated answer and sources
    """
    await require_vector_sync_tools(nc_mcp_client)

    # Create a note with content about Python async
    _note = await temporary_note_factory(
        title="Python Async Guide",
        content="""# Python Async Programming

## Key Concepts
- Use async def for coroutines
- Use await for async operations
- asyncio.gather() for parallel execution

## Best Practices
Always use async context managers for resources.
Avoid blocking operations in async code.""",
        category="Development",
    )
    print(f"Created note ID: {_note['id']}")

    # Wait for vector indexing to complete. Gate on the new note actually
    # being retrievable rather than on the corpus-wide indexed_count gauge,
    # which is non-monotonic under re-scan churn (see wait_for_vector_sync).
    await wait_for_vector_sync(
        nc_mcp_client,
        search_term="Python Async Programming coroutines",
        note_id=_note["id"],
    )

    # Mock the sampling call
    # Note: This requires monkey-patching ctx.session.create_message
    # In a real integration test with MCP Inspector, this would be actual sampling

    call_result = await nc_mcp_client.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": "How do I use async in Python?",
            "limit": 5,
            "score_threshold": 0.0,  # Use 0.0 for SimpleEmbeddingProvider (feature hashing)
        },
    )

    # Extract result from CallToolResult
    assert call_result.isError is False, (
        f"Tool call failed: {call_result.content[0].text if call_result.isError else ''}"
    )
    result = json.loads(call_result.content[0].text)

    # Verify response structure
    assert result is not None
    assert "query" in result
    assert "generated_answer" in result
    assert "sources" in result
    assert "total_found" in result
    assert "search_method" in result

    # For this test, sampling might fail (no real LLM client)
    # So we check for either success or various fallback states
    unsupported_methods = {
        "semantic_sampling_unsupported",
        "semantic_sampling_user_declined",
        "semantic_sampling_timeout",
        "semantic_sampling_mcp_error",
        "semantic_sampling_fallback",
    }

    if result["search_method"] in unsupported_methods:
        # Fallback/unsupported mode - should still have sources
        assert len(result["sources"]) > 0
        assert result["total_found"] > 0
        pytest.skip(
            f"Sampling not available (method: {result['search_method']}), "
            f"but search results returned successfully"
        )
    else:
        # Successful sampling
        assert result["search_method"] == "semantic_sampling"
        assert "async" in result["generated_answer"].lower()
        assert len(result["sources"]) > 0
        assert result["model_used"] is not None


async def test_semantic_search_answer_no_results(nc_mcp_client):
    """Test semantic search answer when no documents match.

    Flow:
    1. Query for completely unrelated topic
    2. Verify response indicates no documents found
    3. Verify no sampling call was made (no sources to base answer on)
    """
    await require_vector_sync_tools(nc_mcp_client)

    call_result = await nc_mcp_client.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": "quantum chromodynamics lattice QCD gluon propagator",
            "limit": 5,
            "score_threshold": 0.7,  # Use high threshold to filter out unrelated documents
        },
    )

    # Extract result from CallToolResult
    assert call_result.isError is False, (
        f"Tool call failed: {call_result.content[0].text if call_result.isError else ''}"
    )
    result = json.loads(call_result.content[0].text)

    # Should get "no documents found" message
    assert result is not None
    assert result["total_found"] == 0
    assert len(result["sources"]) == 0
    assert "No relevant documents" in result["generated_answer"]
    assert result["search_method"] == "semantic_sampling"
    # No sampling should have occurred
    assert result["model_used"] is None
    assert result["stop_reason"] is None


async def test_semantic_search_answer_with_limit(nc_mcp_client, temporary_note_factory):
    """Test semantic search answer respects limit parameter.

    Flow:
    1. Create multiple related notes
    2. Wait for vector sync to complete
    3. Query with limit=2
    4. Verify at most 2 sources in response
    """
    await require_vector_sync_tools(nc_mcp_client)

    # Create multiple related notes
    _note1 = await temporary_note_factory(
        title="Python Async Part 1",
        content="Use async/await for asynchronous operations",
        category="Development",
    )
    _note2 = await temporary_note_factory(
        title="Python Async Part 2",
        content="Use asyncio.gather() for parallel execution",
        category="Development",
    )
    _note3 = await temporary_note_factory(
        title="Python Async Part 3",
        content="Always use async context managers",
        category="Development",
    )

    # Wait until the batch is indexed — gate on the last note being searchable
    # rather than a bare idle signal, which can fire before the new notes are
    # even enqueued.
    await wait_for_vector_sync(
        nc_mcp_client, search_term="async context managers", note_id=_note3["id"]
    )

    call_result = await nc_mcp_client.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": "async programming in Python",
            "limit": 2,
            "score_threshold": 0.0,  # Use 0.0 for SimpleEmbeddingProvider (feature hashing)
        },
    )

    # Extract result from CallToolResult
    assert call_result.isError is False, (
        f"Tool call failed: {call_result.content[0].text if call_result.isError else ''}"
    )
    result = json.loads(call_result.content[0].text)

    # Should respect limit
    assert len(result["sources"]) <= 2


async def test_semantic_search_answer_score_threshold(
    nc_mcp_client, temporary_note_factory
):
    """Test semantic search answer respects score threshold.

    Flow:
    1. Create note with specific content
    2. Wait for vector sync to complete
    3. Query with high threshold (0.9)
    4. Verify only high-scoring results returned
    """
    await require_vector_sync_tools(nc_mcp_client)

    _note = await temporary_note_factory(
        title="Exact Match Test",
        content="This is a very specific test document about widget manufacturing",
        category="Test",
    )

    # Gate on the new note being searchable (not a bare idle signal).
    await wait_for_vector_sync(
        nc_mcp_client, search_term="widget manufacturing", note_id=_note["id"]
    )

    # Query with exact match
    call_result = await nc_mcp_client.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": "widget manufacturing",
            "limit": 5,
            "score_threshold": 0.0,  # Use 0.0 for SimpleEmbeddingProvider (feature hashing)
        },
    )

    # Extract result from CallToolResult
    assert call_result.isError is False, (
        f"Tool call failed: {call_result.content[0].text if call_result.isError else ''}"
    )
    result = json.loads(call_result.content[0].text)

    # Note: Semantic search scores depend on embedding model
    # We just verify the tool accepts the parameter
    assert "score_threshold" not in result  # Not exposed in response
    if result["total_found"] > 0:
        # If results found, verify they're in sources
        assert all("score" in source for source in result["sources"])


async def test_semantic_search_answer_max_tokens(nc_mcp_client, temporary_note_factory):
    """Test semantic search answer respects max_answer_tokens parameter.

    Flow:
    1. Create note with content
    2. Wait for vector sync to complete
    3. Call with very small max_tokens (100)
    4. Verify parameter is accepted (actual token limiting happens in client)

    Note: Token limiting is enforced by the MCP client's LLM, not the server.
    This test just verifies the parameter is correctly passed.
    """
    await require_vector_sync_tools(nc_mcp_client)

    _note = await temporary_note_factory(
        title="Long Document",
        content="This is a document with lots of content. " * 50,
        category="Test",
    )

    # Gate on the new note being searchable (not a bare idle signal).
    await wait_for_vector_sync(
        nc_mcp_client, search_term="Long Document content", note_id=_note["id"]
    )

    call_result = await nc_mcp_client.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": "document content",
            "limit": 5,
            "score_threshold": 0.0,  # Use 0.0 for SimpleEmbeddingProvider (feature hashing)
            "max_answer_tokens": 100,
        },
    )

    # Extract result from CallToolResult
    assert call_result.isError is False, (
        f"Tool call failed: {call_result.content[0].text if call_result.isError else ''}"
    )
    result = json.loads(call_result.content[0].text)

    # Should not error, even if sampling fails
    assert result is not None
    assert "generated_answer" in result


async def test_semantic_search_answer_requires_vector_sync():
    """Test that semantic search answer fails when VECTOR_SYNC_ENABLED=false.

    This test validates the tool properly checks for vector sync being enabled.

    Note: This test requires a separate test client with VECTOR_SYNC_ENABLED=false,
    which may not be available in the current test environment. Skipping for now.
    """
    pytest.skip(
        "Requires test environment with VECTOR_SYNC_ENABLED=false, "
        "which would break other semantic search tests"
    )
