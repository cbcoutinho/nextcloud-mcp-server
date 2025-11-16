"""Tests for RAG generation quality (Answer Correctness metric).

These tests evaluate whether the MCP client LLM generates factually correct
answers from retrieved context using the nc_semantic_search_answer tool.

Metric: Answer Correctness
- Measures: Is the generated answer factually correct?
- Method: LLM-as-judge - Compare RAG answer vs ground truth (binary true/false)
- Evaluation: External LLM evaluates semantic equivalence
"""

import pytest


@pytest.mark.integration
async def test_answer_correctness(
    mcp_sampling_client,
    evaluation_llm,
    nfcorpus_test_data,
):
    """Test that RAG system generates factually correct answers.

    For each test query:
    1. Execute full RAG pipeline via nc_semantic_search_answer MCP tool
    2. Extract generated answer from RAG response
    3. Use LLM-as-judge to compare against ground truth (binary true/false)
    4. Assert answer is semantically equivalent to ground truth

    This tests the quality of the generation component (MCP client LLM).
    """
    results_summary = []

    for test_case in nfcorpus_test_data:
        query = test_case["query_text"]
        ground_truth = test_case["ground_truth_answer"]

        print(f"\n{'=' * 80}")
        print(f"Query: {query}")

        # Execute full RAG pipeline
        print("Executing RAG pipeline...")
        rag_result = await mcp_sampling_client.call_tool(
            "nc_semantic_search_answer",
            arguments={"query": query, "limit": 5},
        )

        rag_answer = rag_result["generated_answer"]

        print(f"RAG Answer preview: {rag_answer[:200]}...")
        print(f"Ground Truth preview: {ground_truth[:200]}...")

        # LLM-as-judge evaluation
        evaluation_prompt = f"""Compare these two answers and respond with only TRUE or FALSE.

Question: {query}

Generated Answer: {rag_answer}

Ground Truth Answer: {ground_truth}

Are these answers semantically equivalent (do they convey the same factual information)?
Respond with only: TRUE or FALSE"""

        print("Evaluating answer correctness...")
        evaluation_result = await evaluation_llm.generate(
            evaluation_prompt,
            max_tokens=10,
        )

        is_correct = evaluation_result.strip().upper() == "TRUE"

        result = {
            "query_id": test_case["query_id"],
            "query": query,
            "rag_answer_length": len(rag_answer),
            "ground_truth_length": len(ground_truth),
            "is_correct": is_correct,
            "evaluation_result": evaluation_result.strip(),
        }
        results_summary.append(result)

        print(f"  Evaluation: {evaluation_result.strip()}")
        print(f"  Status: {'✓ CORRECT' if is_correct else '✗ INCORRECT'}")

        # Assert answer correctness
        assert is_correct, (
            f"Answer mismatch for query: {query}\n\n"
            f"Generated Answer:\n{rag_answer}\n\n"
            f"Ground Truth:\n{ground_truth}\n\n"
            f"Evaluation: {evaluation_result.strip()}"
        )

    # Print summary
    print(f"\n{'=' * 80}")
    print("Answer Correctness Summary:")
    print(f"  Total queries: {len(results_summary)}")
    print(f"  Correct: {sum(r['is_correct'] for r in results_summary)}")
    print(f"  Incorrect: {sum(not r['is_correct'] for r in results_summary)}")
    accuracy = sum(r["is_correct"] for r in results_summary) / len(results_summary)
    print(f"  Accuracy: {accuracy:.2%}")
    print(f"{'=' * 80}")


@pytest.mark.integration
async def test_answer_contains_sources(mcp_sampling_client, nfcorpus_test_data):
    """Test that RAG answers include source citations.

    This is a basic quality check - we verify that the nc_semantic_search_answer
    tool returns both a generated answer and source documents.
    """
    for test_case in nfcorpus_test_data:
        query = test_case["query_text"]

        # Execute RAG pipeline
        rag_result = await mcp_sampling_client.call_tool(
            "nc_semantic_search_answer",
            arguments={"query": query, "limit": 5},
        )

        # Check response structure
        assert "generated_answer" in rag_result, "Response missing 'generated_answer'"
        assert "sources" in rag_result, "Response missing 'sources'"

        # Check sources are provided
        sources = rag_result["sources"]
        assert len(sources) > 0, f"No sources returned for query: {query}"

        # Check each source has required fields
        for i, source in enumerate(sources):
            assert "document_id" in source or "id" in source, (
                f"Source {i} missing document ID"
            )
            assert "excerpt" in source or "content" in source or "text" in source, (
                f"Source {i} missing content"
            )

        print(f"Query: {query}")
        print(f"  Sources provided: {len(sources)}")
        print("  Status: ✓ PASS")
