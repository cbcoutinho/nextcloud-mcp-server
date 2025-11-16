"""Pytest fixtures for RAG evaluation tests.

IMPORTANT: Before running these tests, you must:
1. Generate ground truth: uv run python tools/rag_eval_cli.py generate
2. Upload corpus: uv run python tools/rag_eval_cli.py upload --nextcloud-url http://localhost:8000 --username admin --password admin

This ensures that the ground truth and note mappings are available.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.rag_evaluation.llm_providers import create_llm_provider

# Paths
FIXTURES_DIR = Path(__file__).parent / "fixtures"
GROUND_TRUTH_FILE = FIXTURES_DIR / "ground_truth.json"
NOTE_MAPPING_FILE = FIXTURES_DIR / "note_mapping.json"


@pytest.fixture(scope="session")
def ground_truth_data() -> list[dict[str, Any]]:
    """Load pre-generated ground truth data.

    Returns:
        List of test cases with query, ground truth answer, and expected doc IDs

    Raises:
        FileNotFoundError: If ground_truth.json doesn't exist
    """
    if not GROUND_TRUTH_FILE.exists():
        raise FileNotFoundError(
            f"Ground truth file not found: {GROUND_TRUTH_FILE}\n"
            "Run: uv run python tools/rag_eval_cli.py generate"
        )

    with open(GROUND_TRUTH_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def note_mapping() -> dict[str, int]:
    """Load document ID → note ID mapping.

    Returns:
        Dict mapping nfcorpus document ID to Nextcloud note ID

    Raises:
        FileNotFoundError: If note_mapping.json doesn't exist
    """
    if not NOTE_MAPPING_FILE.exists():
        raise FileNotFoundError(
            f"Note mapping file not found: {NOTE_MAPPING_FILE}\n"
            "Run: uv run python tools/rag_eval_cli.py upload --nextcloud-url ... --username ... --password ..."
        )

    with open(NOTE_MAPPING_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def nfcorpus_test_data(
    ground_truth_data: list[dict[str, Any]],
    note_mapping: dict[str, int],
):
    """Prepare nfcorpus test data for evaluation.

    This fixture combines ground truth answers with note mappings to create
    test cases ready for retrieval and generation quality tests.

    Args:
        ground_truth_data: Pre-generated ground truth answers
        note_mapping: Document ID → note ID mapping

    Returns:
        List of test cases with query, ground truth, expected doc IDs, and note IDs
    """
    test_cases = []

    for gt in ground_truth_data:
        # Map expected document IDs to note IDs
        expected_note_ids = [
            note_mapping.get(doc_id)
            for doc_id in gt["expected_document_ids"]
            if doc_id in note_mapping
        ]

        # Filter out None values (docs that weren't uploaded)
        expected_note_ids = [nid for nid in expected_note_ids if nid is not None]

        test_cases.append(
            {
                "query_id": gt["query_id"],
                "query_text": gt["query_text"],
                "ground_truth_answer": gt["ground_truth_answer"],
                "expected_document_ids": gt["expected_document_ids"],
                "expected_note_ids": expected_note_ids,
                "highly_relevant_count": gt["highly_relevant_count"],
            }
        )

    return test_cases


@pytest.fixture(scope="session")
async def evaluation_llm():
    """Create LLM provider for evaluation (separate from MCP client).

    Environment variables:
      RAG_EVAL_PROVIDER: Provider type (ollama or anthropic)
      RAG_EVAL_OLLAMA_BASE_URL: Ollama base URL (or OLLAMA_HOST)
      RAG_EVAL_OLLAMA_MODEL: Ollama model name
      RAG_EVAL_ANTHROPIC_API_KEY: Anthropic API key
      RAG_EVAL_ANTHROPIC_MODEL: Anthropic model name

    Returns:
        LLM provider instance (OllamaProvider or AnthropicProvider)
    """
    llm = create_llm_provider()
    yield llm
    await llm.close()


@pytest.fixture(scope="session")
async def mcp_sampling_client():
    """Create MCP client that supports sampling for RAG generation.

    This fixture creates an MCP client configured to support sampling,
    which is required for testing the nc_semantic_search_answer tool.

    TODO: Implement MCP client with sampling support
    For now, this is a placeholder.

    Returns:
        MCP client instance with sampling enabled
    """
    # TODO: Implement MCP client creation with sampling support
    # This will require:
    # 1. Creating an MCP client configured for sampling
    # 2. Authenticating with Nextcloud
    # 3. Ensuring sampling is enabled
    pytest.skip("MCP sampling client not yet implemented")
