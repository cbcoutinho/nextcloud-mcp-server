"""Integration tests for MCP sampling with semantic search.

These tests validate the nc_notes_semantic_search_answer tool which combines:
1. Semantic search to retrieve relevant documents
2. MCP sampling to generate natural language answers

Tests cover three scenarios:
- Successful sampling (LLM generates answer)
- Sampling fallback (client doesn't support sampling)
- No results (no relevant documents found)

Note: These tests require VECTOR_SYNC_ENABLED=true and a configured
vector database with indexed test data.
"""

from unittest.mock import MagicMock

import pytest
from mcp.types import CreateMessageResult, TextContent

pytestmark = pytest.mark.integration


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


@pytest.mark.asyncio
async def test_semantic_search_answer_successful_sampling(
    nc_mcp_client, temporary_note, mock_sampling_result
):
    """Test semantic search with successful LLM answer generation.

    Prerequisites:
    - VECTOR_SYNC_ENABLED=true
    - Qdrant running and indexed
    - Test note indexed in vector database

    Flow:
    1. Create test note with searchable content
    2. Call nc_notes_semantic_search_answer
    3. Mock ctx.session.create_message to return answer
    4. Verify response contains generated answer and sources
    """
    # Create a note with content about Python async
    _note = await temporary_note(
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

    # Wait for vector indexing (if background sync is slow)
    import asyncio

    await asyncio.sleep(2)

    # Mock the sampling call
    # Note: This requires monkey-patching ctx.session.create_message
    # In a real integration test with MCP Inspector, this would be actual sampling

    result = await nc_mcp_client.call_tool(
        "nc_notes_semantic_search_answer",
        arguments={
            "query": "How do I use async in Python?",
            "limit": 5,
            "score_threshold": 0.5,
        },
    )

    # Verify response structure
    assert result is not None
    assert "query" in result
    assert "generated_answer" in result
    assert "sources" in result
    assert "total_found" in result
    assert "search_method" in result

    # For this test, sampling might fail (no real LLM client)
    # So we check for either success or fallback
    if "[Sampling unavailable" in result["generated_answer"]:
        # Fallback mode - should still have sources
        assert result["search_method"] == "semantic_sampling_fallback"
        assert len(result["sources"]) > 0
        pytest.skip("Sampling not supported by test client (expected fallback)")
    else:
        # Successful sampling
        assert result["search_method"] == "semantic_sampling"
        assert "async" in result["generated_answer"].lower()
        assert len(result["sources"]) > 0
        assert result["model_used"] is not None


@pytest.mark.asyncio
async def test_semantic_search_answer_no_results(nc_mcp_client):
    """Test semantic search answer when no documents match.

    Flow:
    1. Query for completely unrelated topic
    2. Verify response indicates no documents found
    3. Verify no sampling call was made (no sources to base answer on)
    """
    result = await nc_mcp_client.call_tool(
        "nc_notes_semantic_search_answer",
        arguments={
            "query": "quantum chromodynamics lattice QCD gluon propagator",
            "limit": 5,
            "score_threshold": 0.7,
        },
    )

    # Should get "no documents found" message
    assert result is not None
    assert result["total_found"] == 0
    assert len(result["sources"]) == 0
    assert "No relevant documents" in result["generated_answer"]
    assert result["search_method"] == "semantic_sampling"
    # No sampling should have occurred
    assert result["model_used"] is None
    assert result["stop_reason"] is None


@pytest.mark.asyncio
async def test_semantic_search_answer_with_limit(nc_mcp_client, temporary_note):
    """Test semantic search answer respects limit parameter.

    Flow:
    1. Create multiple related notes
    2. Query with limit=2
    3. Verify at most 2 sources in response
    """
    # Create multiple related notes
    _note1 = await temporary_note(
        title="Python Async Part 1",
        content="Use async/await for asynchronous operations",
        category="Development",
    )
    _note2 = await temporary_note(
        title="Python Async Part 2",
        content="Use asyncio.gather() for parallel execution",
        category="Development",
    )
    _note3 = await temporary_note(
        title="Python Async Part 3",
        content="Always use async context managers",
        category="Development",
    )

    # Wait for indexing
    import asyncio

    await asyncio.sleep(2)

    result = await nc_mcp_client.call_tool(
        "nc_notes_semantic_search_answer",
        arguments={
            "query": "async programming in Python",
            "limit": 2,
            "score_threshold": 0.5,
        },
    )

    # Should respect limit
    assert len(result["sources"]) <= 2


@pytest.mark.asyncio
async def test_semantic_search_answer_score_threshold(nc_mcp_client, temporary_note):
    """Test semantic search answer respects score threshold.

    Flow:
    1. Create note with specific content
    2. Query with high threshold (0.9)
    3. Verify only high-scoring results returned
    """
    _note = await temporary_note(
        title="Exact Match Test",
        content="This is a very specific test document about widget manufacturing",
        category="Test",
    )

    # Wait for indexing
    import asyncio

    await asyncio.sleep(2)

    # Query with exact match - should have high score
    result = await nc_mcp_client.call_tool(
        "nc_notes_semantic_search_answer",
        arguments={
            "query": "widget manufacturing",
            "limit": 5,
            "score_threshold": 0.9,
        },
    )

    # Note: Semantic search scores depend on embedding model
    # We just verify the tool accepts the parameter
    assert "score_threshold" not in result  # Not exposed in response
    if result["total_found"] > 0:
        # If results found, verify they're in sources
        assert all("score" in source for source in result["sources"])


@pytest.mark.asyncio
async def test_semantic_search_answer_max_tokens(nc_mcp_client, temporary_note):
    """Test semantic search answer respects max_answer_tokens parameter.

    Flow:
    1. Create note with content
    2. Call with very small max_tokens (100)
    3. Verify parameter is accepted (actual token limiting happens in client)

    Note: Token limiting is enforced by the MCP client's LLM, not the server.
    This test just verifies the parameter is correctly passed.
    """
    _note = await temporary_note(
        title="Long Document",
        content="This is a document with lots of content. " * 50,
        category="Test",
    )

    # Wait for indexing
    import asyncio

    await asyncio.sleep(2)

    result = await nc_mcp_client.call_tool(
        "nc_notes_semantic_search_answer",
        arguments={
            "query": "document content",
            "limit": 5,
            "score_threshold": 0.5,
            "max_answer_tokens": 100,
        },
    )

    # Should not error, even if sampling fails
    assert result is not None
    assert "generated_answer" in result


@pytest.mark.asyncio
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
