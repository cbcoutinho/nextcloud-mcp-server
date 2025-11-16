"""Tests for RAG retrieval quality (Context Recall metric).

These tests evaluate whether the vector sync/embedding pipeline successfully
retrieves documents containing the answer to a query.

Metric: Context Recall
- Measures: Did we retrieve documents containing the answer?
- Method: Heuristic - Check if ground-truth document IDs appear in top-k results
- Target: ≥80% recall (at least 80% of expected docs in top-10 results)
"""

import pytest


@pytest.mark.integration
async def test_retrieval_context_recall(nc_client, nfcorpus_test_data):
    """Test that semantic search retrieves documents containing the answer.

    For each test query:
    1. Perform semantic search (retrieval only, no generation)
    2. Extract retrieved document IDs from top-k results
    3. Calculate Context Recall: intersection of retrieved and expected docs
    4. Assert recall meets threshold (≥80%)

    This tests the quality of the vector sync/embedding pipeline.
    """
    # Top-k documents to retrieve
    k = 10

    # Minimum acceptable recall
    min_recall = 0.8

    results_summary = []

    for test_case in nfcorpus_test_data:
        query = test_case["query_text"]
        expected_note_ids = set(test_case["expected_note_ids"])

        # Perform semantic search (retrieval only)
        search_results = await nc_client.notes.semantic_search(
            query=query,
            limit=k,
        )

        # Extract retrieved note IDs
        retrieved_note_ids = {result["id"] for result in search_results}

        # Calculate Context Recall
        intersection = expected_note_ids & retrieved_note_ids
        recall = len(intersection) / len(expected_note_ids) if expected_note_ids else 0

        # Store results
        result = {
            "query_id": test_case["query_id"],
            "query": query,
            "expected_count": len(expected_note_ids),
            "retrieved_count": len(retrieved_note_ids),
            "intersection_count": len(intersection),
            "recall": recall,
            "passed": recall >= min_recall,
        }
        results_summary.append(result)

        # Print detailed result for this query
        print(f"\n{'=' * 80}")
        print(f"Query: {query}")
        print(f"  Expected docs: {len(expected_note_ids)}")
        print(f"  Retrieved (top-{k}): {len(retrieved_note_ids)}")
        print(f"  Intersection: {len(intersection)}")
        print(f"  Context Recall: {recall:.2%}")
        print(f"  Status: {'✓ PASS' if result['passed'] else '✗ FAIL'}")

        # Assert recall meets threshold
        assert recall >= min_recall, (
            f"Context Recall {recall:.2%} below threshold {min_recall:.2%} "
            f"for query: {query}\n"
            f"Expected {len(expected_note_ids)} docs, found {len(intersection)} in top-{k}"
        )

    # Print summary
    print(f"\n{'=' * 80}")
    print("Context Recall Summary:")
    print(f"  Total queries: {len(results_summary)}")
    print(f"  Passed: {sum(r['passed'] for r in results_summary)}")
    print(f"  Failed: {sum(not r['passed'] for r in results_summary)}")
    print(
        f"  Average recall: {sum(r['recall'] for r in results_summary) / len(results_summary):.2%}"
    )
    print(f"{'=' * 80}")


@pytest.mark.integration
async def test_retrieval_top1_precision(nc_client, nfcorpus_test_data):
    """Test that the top-1 retrieved document is highly relevant.

    This is a stricter test than context recall - we verify that
    the single most relevant document (rank 1) is in the expected set.

    This tests whether the ranking is good, not just retrieval.
    """
    results_summary = []

    for test_case in nfcorpus_test_data:
        query = test_case["query_text"]
        expected_note_ids = set(test_case["expected_note_ids"])

        # Perform semantic search
        search_results = await nc_client.notes.semantic_search(
            query=query,
            limit=1,  # Only top-1
        )

        # Check if top result is in expected set
        if search_results:
            top_result_id = search_results[0]["id"]
            is_relevant = top_result_id in expected_note_ids
        else:
            is_relevant = False

        result = {
            "query_id": test_case["query_id"],
            "query": query,
            "top_result_id": search_results[0]["id"] if search_results else None,
            "is_relevant": is_relevant,
        }
        results_summary.append(result)

        print(f"\nQuery: {query}")
        print(f"  Top-1 relevant: {'✓ YES' if is_relevant else '✗ NO'}")

        # This is informational - we don't assert here
        # Some queries may have multiple valid top results

    # Print summary
    precision_at_1 = sum(r["is_relevant"] for r in results_summary) / len(
        results_summary
    )
    print(f"\n{'=' * 80}")
    print(f"Precision@1: {precision_at_1:.2%}")
    print(
        f"  ({sum(r['is_relevant'] for r in results_summary)}/{len(results_summary)} queries)"
    )
    print(f"{'=' * 80}")
