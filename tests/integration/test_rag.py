"""Integration tests for RAG pipeline with multiple LLM providers.

These tests validate the complete semantic search and MCP sampling flow using:
1. MCP server's built-in semantic search (embeddings handled server-side)
2. MCP sampling for answer generation (any generation-capable provider)
3. Pre-indexed Nextcloud User Manual as the knowledge base

Usage:
    # Run with OpenAI (including GitHub Models API)
    OPENAI_API_KEY=... pytest tests/integration/test_rag.py --provider=openai -v

    # Run with Ollama
    OLLAMA_BASE_URL=http://localhost:11434 OLLAMA_GENERATION_MODEL=llama3.2:1b \\
        pytest tests/integration/test_rag.py --provider=ollama -v

    # Run with Anthropic
    ANTHROPIC_API_KEY=... pytest tests/integration/test_rag.py --provider=anthropic -v

    # Run with AWS Bedrock
    AWS_REGION=us-east-1 BEDROCK_GENERATION_MODEL=... \\
        pytest tests/integration/test_rag.py --provider=bedrock -v

Environment Variables:
    See tests/integration/provider_fixtures.py for provider-specific configuration.
    RAG_MANUAL_PATH: Path to manual PDF in Nextcloud (default: "Nextcloud Manual.pdf")

Prerequisites:
    - Nextcloud User Manual PDF uploaded to Nextcloud
    - VECTOR_SYNC_ENABLED=true on the MCP server
    - Provider-specific environment variables set
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator

import anyio
import pytest
from httpx import HTTPStatusError
from mcp import ClientSession

from nextcloud_mcp_server.providers.base import Provider
from tests.conftest import create_mcp_client_session
from tests.integration.provider_fixtures import create_generation_provider
from tests.integration.sampling_support import create_sampling_callback

logger = logging.getLogger(__name__)

# Default path to the Nextcloud User Manual PDF
DEFAULT_MANUAL_PATH = "Nextcloud Manual.pdf"


async def require_vector_sync_tools(nc_mcp_client):
    """Skip test if vector sync tools are not available."""
    tools = await nc_mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    if "nc_get_vector_sync_status" not in tool_names:
        pytest.skip("Vector sync tools not available (VECTOR_SYNC_ENABLED not set)")


async def llm_judge(
    provider: Provider,
    ground_truth: str,
    system_output: str,
) -> bool:
    """Use LLM to judge if system output aligns with ground truth.

    Args:
        provider: Any provider with generation capability
        ground_truth: The expected/reference answer
        system_output: The system's actual output to evaluate

    Returns:
        True if output aligns with ground truth, False otherwise
    """
    prompt = f"""GROUND TRUTH: {ground_truth}

SYSTEM OUTPUT: {system_output}

Does the system output contain the key facts from the ground truth?

Answer: TRUE or FALSE"""

    logger.info("Received ground truth: %s", ground_truth)
    logger.info("Received system output: %s", system_output)

    response = await provider.generate(prompt, max_tokens=10)
    logger.info("LLM Judge response: %s", response)
    return "TRUE" in response.upper()


# Mark all tests as integration tests
pytestmark = [
    pytest.mark.integration,
    pytest.mark.rag,
]

# Ground truth fixture path
FIXTURES_DIR = Path(__file__).parent / "fixtures"
GROUND_TRUTH_FILE = FIXTURES_DIR / "nextcloud_manual_ground_truth.json"


@pytest.fixture(scope="module")
def ground_truth_qa():
    """Load ground truth Q&A pairs for the Nextcloud manual."""
    if not GROUND_TRUTH_FILE.exists():
        pytest.skip(f"Ground truth file not found: {GROUND_TRUTH_FILE}")

    with open(GROUND_TRUTH_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
async def indexed_manual_pdf(nc_client, nc_mcp_client):
    """Ensure the Nextcloud User Manual PDF is tagged and indexed for vector search.

    This fixture:
    1. Gets file info for the manual PDF
    2. Creates/gets the 'vector-index' tag
    3. Assigns the tag to the file
    4. Waits for vector sync to complete indexing

    Environment Variables:
        RAG_MANUAL_PATH: Path to manual PDF in Nextcloud (default: Nextcloud Manual.pdf)
    """
    await require_vector_sync_tools(nc_mcp_client)

    manual_path = os.getenv("RAG_MANUAL_PATH", DEFAULT_MANUAL_PATH)

    logger.info("Setting up indexed manual PDF: %s", manual_path)

    # Get file info to verify file exists and get file ID. After the
    # round-7 contract widening, get_file_info raises HTTPStatusError on
    # 404 instead of returning None — so wrap and skip on a definitive
    # not-found.
    try:
        file_info = await nc_client.webdav.get_file_info(manual_path)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            pytest.skip(f"Manual PDF not found at '{manual_path}'")
        raise
    if not file_info:
        pytest.skip(f"Manual PDF unreadable at '{manual_path}' (malformed PROPFIND)")

    file_id = file_info["id"]
    logger.info("Found manual PDF: %s (file_id=%s)", manual_path, file_id)

    # Create or get the vector-index tag
    tag = await nc_client.webdav.get_or_create_tag("vector-index")
    tag_id = tag["id"]
    logger.info("Using tag 'vector-index' (tag_id=%s)", tag_id)

    # Assign tag to file
    await nc_client.webdav.assign_tag_to_file(file_id, tag_id)
    logger.info("Tagged file %s with vector-index tag", file_id)

    # Wait for vector sync to complete indexing
    max_attempts = 60
    poll_interval = 10

    logger.info("Waiting for vector sync to index the manual...")

    for attempt in range(1, max_attempts + 1):
        try:
            # Call the MCP tool via the existing client session
            result = await nc_mcp_client.call_tool(
                "nc_get_vector_sync_status",
                arguments={},
            )

            if not result.isError:
                content = json.loads(result.content[0].text) if result.content else {}
                indexed = content.get("indexed_count", 0)
                pending = content.get("pending_count", 1)
                status = content.get("status")

                logger.info(
                    "Attempt %s/%s: indexed=%s, pending=%s, status=%s",
                    attempt,
                    max_attempts,
                    indexed,
                    pending,
                    status,
                )

                # Require indexed > 0 (the manual must actually be indexed —
                # idle/pending==0 is also the *initial* empty state) AND a
                # settled idle scan so we don't break during a transient
                # pending==0 window mid re-scan churn.
                if indexed > 0 and pending == 0 and status == "idle":
                    logger.info(
                        "Vector indexing complete: %s documents indexed", indexed
                    )
                    break
        except Exception as e:
            logger.warning("Attempt %s: Error checking status: %s", attempt, e)

        if attempt < max_attempts:
            await anyio.sleep(poll_interval)
    else:
        logger.warning(
            "Vector indexing may not be complete after %s attempts", max_attempts
        )

    yield {
        "path": manual_path,
        "file_id": file_id,
        "tag_id": tag_id,
    }


@pytest.fixture(scope="module")
def provider_name(request) -> str:
    """Get the provider name from --provider flag.

    Raises pytest.skip if --provider not specified.
    """
    name = request.config.getoption("--provider")
    if not name:
        pytest.skip("--provider flag required (openai, ollama, anthropic, bedrock)")
    return name


@pytest.fixture(scope="module")
async def generation_provider(provider_name: str) -> AsyncGenerator[Provider, None]:
    """Provider configured for text generation.

    Requires --provider flag to be set.
    """
    provider = await create_generation_provider(provider_name)
    yield provider
    await provider.close()


@pytest.fixture(scope="module")
async def nc_mcp_client_with_sampling(
    anyio_backend, generation_provider, provider_name
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client with sampling support using the specified provider.

    This fixture creates an MCP client that can handle sampling requests
    from the server using the configured generation provider.
    """
    sampling_callback = create_sampling_callback(generation_provider)

    async with create_mcp_client_session(
        url="http://localhost:8000/mcp",
        client_name=f"Sampling MCP ({provider_name})",
        sampling_callback=sampling_callback,
    ) as session:
        yield session


async def test_semantic_search_retrieval(
    nc_mcp_client, ground_truth_qa, indexed_manual_pdf, generation_provider
):
    """Test that semantic search retrieves relevant documents from the manual.

    This tests the retrieval component of RAG - ensuring that queries
    return relevant chunks from the indexed Nextcloud User Manual.
    """
    # Use first query from ground truth
    test_case = ground_truth_qa[0]  # 2FA question
    query = test_case["query"]

    # Perform semantic search via MCP tool
    result = await nc_mcp_client.call_tool(
        "nc_semantic_search",
        arguments={
            "query": query,
            "limit": 5,
            "score_threshold": 0.0,
        },
    )

    assert result.isError is False, f"Tool call failed: {result}"
    data = json.loads(result.content[0].text)

    # Verify we got results
    assert data["success"] is True
    assert data["total_found"] > 0, f"No results for query: {query}"
    assert len(data["results"]) > 0

    # Use LLM judge to evaluate if excerpts are relevant to ground truth
    all_excerpts = " ".join([r["excerpt"] for r in data["results"]])
    is_relevant = await llm_judge(
        generation_provider,
        test_case["ground_truth"],
        all_excerpts,
    )
    assert is_relevant, f"LLM judge: excerpts not relevant to query: {query}"


async def test_semantic_search_answer_with_sampling(
    nc_mcp_client_with_sampling,
    ground_truth_qa,
    indexed_manual_pdf,
    generation_provider,
):
    """Test semantic search with MCP sampling for answer generation.

    This tests the full RAG pipeline:
    1. Semantic search retrieves relevant documents
    2. MCP sampling generates an answer from the retrieved context
    3. Provider generates the answer via the sampling callback

    Uses nc_mcp_client_with_sampling which has sampling enabled.
    """
    # Use the 2FA question - has clear expected answer
    test_case = ground_truth_qa[0]
    query = test_case["query"]

    result = await nc_mcp_client_with_sampling.call_tool(
        "nc_semantic_search_answer",
        arguments={
            "query": query,
            "limit": 5,
            "score_threshold": 0.0,
            "max_answer_tokens": 300,
        },
    )

    assert result.isError is False, f"Tool call failed: {result}"
    data = json.loads(result.content[0].text)

    # Verify response structure
    assert data["success"] is True
    assert "query" in data
    assert "generated_answer" in data
    assert "sources" in data
    assert "search_method" in data

    # Check for either successful sampling or graceful fallback
    fallback_methods = {
        "semantic_sampling_unsupported",
        "semantic_sampling_user_declined",
        "semantic_sampling_timeout",
        "semantic_sampling_mcp_error",
        "semantic_sampling_fallback",
    }

    if data["search_method"] in fallback_methods:
        # Fallback mode - verify sources still returned
        assert len(data["sources"]) > 0, "Expected sources even in fallback mode"
        pytest.skip(
            f"MCP sampling not available (method: {data['search_method']}), "
            f"but retrieval succeeded with {len(data['sources'])} sources"
        )
    else:
        # Successful sampling - verify answer quality
        assert data["search_method"] == "semantic_sampling"
        assert data["generated_answer"] is not None
        assert len(data["generated_answer"]) > 50  # Non-trivial answer

        # Use LLM judge to evaluate answer relevance
        is_relevant = await llm_judge(
            generation_provider,
            test_case["ground_truth"],
            data["generated_answer"],
        )
        assert is_relevant, f"LLM judge: answer not relevant to query: {query}"


@pytest.mark.parametrize(
    "qa_index,min_expected_results",
    [
        (0, 1),  # 2FA question
        (1, 1),  # File quotas question
        (2, 1),  # Linux installation question
        (3, 1),  # Windows requirements question
        (4, 1),  # Client apps with 2FA question
    ],
)
async def test_retrieval_quality_all_queries(
    nc_mcp_client, ground_truth_qa, indexed_manual_pdf, qa_index, min_expected_results
):
    """Test retrieval quality for all ground truth queries.

    Validates that each query returns at least the minimum expected
    number of relevant results from the Nextcloud manual.
    """
    if qa_index >= len(ground_truth_qa):
        pytest.skip(f"Ground truth index {qa_index} not available")

    test_case = ground_truth_qa[qa_index]
    query = test_case["query"]

    result = await nc_mcp_client.call_tool(
        "nc_semantic_search",
        arguments={
            "query": query,
            "limit": 5,
            "score_threshold": 0.0,
        },
    )

    assert result.isError is False
    data = json.loads(result.content[0].text)

    assert data["total_found"] >= min_expected_results, (
        f"Query '{query}' returned {data['total_found']} results, "
        f"expected at least {min_expected_results}"
    )


async def _top_score(nc_mcp_client: Any, query: str) -> float | None:
    """Return the best fusion score for ``query``, or None if no results."""
    result = await nc_mcp_client.call_tool(
        "nc_semantic_search",
        arguments={"query": query, "limit": 5, "score_threshold": 0.0},
    )
    assert result.isError is False, result.content
    data = json.loads(result.content[0].text)
    results = data.get("results", [])
    if not results:  # guard the list directly, not via total_found
        return None
    return max(r["score"] for r in results)


async def test_no_results_for_unrelated_query(nc_mcp_client, indexed_manual_pdf):
    """An unrelated query must not out-rank a genuinely relevant one.

    The Nextcloud manual has no quantum-physics content, so a physics query
    must not look *more* relevant than a real manual query.

    We deliberately do NOT assert on an absolute score magnitude. Fusion scores
    (RRF/DBSF) are rank-based, not calibrated relevance: the top hit saturates
    near the high end of the range regardless of true relevance, so a hardcoded
    ``max_score < 0.8`` check was a CI flake (it tripped whenever the unrelated
    query happened to retrieve any chunk at all). Comparing against a relevant
    query on the same corpus is self-calibrating and stable.
    """
    # No results for the nonsense query is the ideal outcome — treat as score
    # 0.0 and fall through, so the comparison (and the manual-is-indexed check
    # below) still runs instead of the test silently skipping every time the
    # physics query finds nothing.
    unrelated = (
        await _top_score(
            nc_mcp_client, "quantum entanglement hadron collider particle physics"
        )
        or 0.0
    )

    relevant = await _top_score(
        nc_mcp_client, "how do I enable two-factor authentication"
    )
    assert relevant is not None, (
        "Relevant control query returned nothing — manual not indexed?"
    )

    # The unrelated query must not appear more relevant than the real one.
    assert unrelated <= relevant, (
        f"Unrelated query scored {unrelated}, higher than the relevant "
        f"control query's {relevant} — retrieval is not discriminating."
    )
