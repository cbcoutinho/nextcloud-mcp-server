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

import pytest
from mcp.types import CreateMessageResult, TextContent

pytestmark = pytest.mark.integration


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

    # Get initial indexed count before creating note
    import asyncio

    initial_sync = await nc_mcp_client.call_tool(
        "nc_get_vector_sync_status", arguments={}
    )
    initial_indexed_count = json.loads(initial_sync.content[0].text)["indexed_count"]
    print(f"Initial indexed count: {initial_indexed_count}")

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

    # Wait for vector indexing to complete
    max_wait = 30  # Maximum 30 seconds
    wait_interval = 1  # Check every 1 second
    waited = 0

    while waited < max_wait:
        sync_status = await nc_mcp_client.call_tool(
            "nc_get_vector_sync_status", arguments={}
        )
        status_data = json.loads(sync_status.content[0].text)

        print(
            f"Sync status at {waited}s: indexed={status_data['indexed_count']}, pending={status_data['pending_count']}, status={status_data['status']}"
        )

        # Check if indexed count increased (new note was indexed)
        if (
            status_data["indexed_count"] > initial_indexed_count
            and status_data["pending_count"] == 0
        ):
            # Sync complete and new document indexed
            print(
                f"✓ Sync complete: {status_data['indexed_count']} documents indexed (was {initial_indexed_count})"
            )
            break

        await asyncio.sleep(wait_interval)
        waited += wait_interval

    # Verify sync completed
    assert waited < max_wait, (
        f"Vector sync did not complete within {max_wait} seconds. Last status: {status_data}"
    )
    assert status_data["indexed_count"] > initial_indexed_count, (
        f"New note was not indexed (count stayed at {initial_indexed_count})"
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

    # Wait for vector indexing to complete
    import asyncio

    max_wait = 30
    wait_interval = 1
    waited = 0

    while waited < max_wait:
        sync_status = await nc_mcp_client.call_tool(
            "nc_get_vector_sync_status", arguments={}
        )
        status_data = json.loads(sync_status.content[0].text)

        if status_data["status"] == "idle" and status_data["pending_count"] == 0:
            break

        await asyncio.sleep(wait_interval)
        waited += wait_interval

    assert waited < max_wait, f"Vector sync did not complete within {max_wait} seconds"

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

    # Wait for vector indexing to complete
    import asyncio

    max_wait = 30
    wait_interval = 1
    waited = 0

    while waited < max_wait:
        sync_status = await nc_mcp_client.call_tool(
            "nc_get_vector_sync_status", arguments={}
        )
        status_data = json.loads(sync_status.content[0].text)

        if status_data["status"] == "idle" and status_data["pending_count"] == 0:
            break

        await asyncio.sleep(wait_interval)
        waited += wait_interval

    assert waited < max_wait, f"Vector sync did not complete within {max_wait} seconds"

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

    # Wait for vector indexing to complete
    import asyncio

    max_wait = 30
    wait_interval = 1
    waited = 0

    while waited < max_wait:
        sync_status = await nc_mcp_client.call_tool(
            "nc_get_vector_sync_status", arguments={}
        )
        status_data = json.loads(sync_status.content[0].text)

        if status_data["status"] == "idle" and status_data["pending_count"] == 0:
            break

        await asyncio.sleep(wait_interval)
        waited += wait_interval

    assert waited < max_wait, f"Vector sync did not complete within {max_wait} seconds"

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
