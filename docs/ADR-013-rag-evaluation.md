## ADR-013: RAG Evaluation Testing Framework

**Status:** Proposed

**Date:** 2025-11-15

### Context

The `nc_semantic_search_answer` tool implements a Retrieval-Augmented Generation (RAG) system where:
1. **Retrieval**: Vector sync pipeline indexes Nextcloud documents (notes, calendar, contacts, etc.) into a vector database
2. **Generation**: MCP client's LLM synthesizes answers from retrieved documents via MCP sampling (ADR-008)

We need a testing framework to evaluate RAG system performance and identify whether failures occur in retrieval (wrong documents found) or generation (poor answer quality). This framework must use industry-standard evaluation methodologies while remaining practical to implement and maintain.

To establish a baseline, we will use the **BeIR/nfcorpus** dataset (medical/biomedical corpus) with ~5,000 documents and established query/answer pairs.

Homepage: https://www.cl.uni-heidelberg.de/statnlpgroup/nfcorpus/
Download: https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip

### Decision

We will implement a **two-part evaluation framework** that independently tests retrieval and generation quality using pytest fixtures.

#### In Scope

**1. Retrieval Evaluation**
Tests the vector sync/embedding pipeline's ability to find relevant documents.

- **Metric: Context Recall** (Did we retrieve documents containing the answer?)
  - **Evaluation method**: Heuristic - Check if ground-truth document IDs appear in top-k retrieval results
  - **Test**: Query → Semantic search → Assert expected doc IDs present

**2. Generation Evaluation**
Tests the MCP client LLM's ability to synthesize correct answers from retrieved context.

- **Metric: Answer Correctness** (Is the generated answer factually correct?)
  - **Evaluation method**: LLM-as-judge - Compare RAG answer against ground-truth answer
  - **Test**: Query → `nc_semantic_search_answer` → LLM evaluates answer vs. ground truth (binary true/false)

#### Out of Scope (Initial Implementation)

- **Context Relevance/Precision**: Measuring irrelevant documents in retrieval results
- **Faithfulness/Groundedness**: Detecting hallucinations not supported by retrieved context
- **Answer Relevance**: Whether answer addresses the specific question asked
- **Out-of-Scope Handling**: Testing "I don't know" responses when answer isn't in context
- **Continuous benchmarking**: Automated tracking of metric trends over time
- **Custom domain datasets**: Production-specific test data (medical corpus used initially)

These remain valuable for future iterations but add complexity beyond our initial goals.

#### Implementation

**Test Structure**

Location: `tests/rag_evaluation/`
- `test_retrieval_quality.py` - Retrieval evaluation tests
- `test_generation_quality.py` - Generation evaluation tests
- `conftest.py` - Fixtures for test data, MCP clients, and evaluation LLMs

**Required Pytest Fixtures**

1. **`nfcorpus_test_data`** (session-scoped)
   - Downloads/caches BeIR nfcorpus dataset at runtime
   - Loads 5 pre-selected test queries with:
     - Query text
     - Pre-generated ground-truth answer (from `tests/rag_evaluation/fixtures/ground_truth.json`)
     - Expected document IDs (from qrels with score=2)
   - Uploads all corpus documents as notes in test Nextcloud instance
   - Triggers vector sync to index documents
   - Waits for indexing completion
   - Returns test case data structure

2. **`mcp_sampling_client`** (session-scoped)
   - Creates MCP client that supports sampling
   - Configurable LLM provider (ollama or anthropic) via environment:
     - `RAG_EVAL_PROVIDER=ollama` (default) or `anthropic`
     - `RAG_EVAL_OLLAMA_BASE_URL=http://localhost:11434`
     - `RAG_EVAL_OLLAMA_MODEL=llama3.1:8b`
     - `RAG_EVAL_ANTHROPIC_API_KEY=sk-...`
     - `RAG_EVAL_ANTHROPIC_MODEL=claude-3-5-sonnet-20241022`
   - Returns configured MCP client fixture

3. **`evaluation_llm`** (session-scoped)
   - Separate LLM instance for evaluation (independent from MCP client)
   - Same provider configuration as `mcp_sampling_client`
   - Returns callable: `async def evaluate(prompt: str) -> str`

**Test Implementation Examples**

```python
# tests/rag_evaluation/test_retrieval_quality.py
async def test_retrieval_recall(nc_client, nfcorpus_test_data):
    """Test that semantic search retrieves documents containing the answer."""
    for test_case in nfcorpus_test_data:
        # Perform semantic search (retrieval only, no generation)
        results = await nc_client.notes.semantic_search(
            query=test_case.query,
            limit=10
        )

        retrieved_doc_ids = {r.document_id for r in results}
        expected_doc_ids = set(test_case.expected_document_ids)

        # Context Recall: Are expected documents in top-k results?
        recall = len(expected_doc_ids & retrieved_doc_ids) / len(expected_doc_ids)
        assert recall >= 0.8, f"Recall {recall} below threshold for query: {test_case.query}"


# tests/rag_evaluation/test_generation_quality.py
async def test_answer_correctness(mcp_sampling_client, evaluation_llm, nfcorpus_test_data):
    """Test that RAG system generates factually correct answers."""
    for test_case in nfcorpus_test_data:
        # Execute full RAG pipeline (retrieval + generation)
        result = await mcp_sampling_client.call_tool(
            "nc_semantic_search_answer",
            arguments={"query": test_case.query, "limit": 5}
        )

        rag_answer = result["generated_answer"]

        # LLM-as-judge evaluation
        evaluation_prompt = f"""Compare these two answers and respond with only TRUE or FALSE.

Question: {test_case.query}

Generated Answer: {rag_answer}

Ground Truth Answer: {test_case.ground_truth}

Are these answers semantically equivalent (do they convey the same factual information)?
Respond with only: TRUE or FALSE"""

        evaluation_result = await evaluation_llm(evaluation_prompt)

        assert evaluation_result.strip().upper() == "TRUE", \
            f"Answer mismatch for query: {test_case.query}\nGot: {rag_answer}\nExpected: {test_case.ground_truth}"
```

**Dataset Integration**

The BeIR nfcorpus dataset structure:
- **corpus.jsonl**: 3,633 medical/biomedical documents (articles from PubMed)
- **queries.jsonl**: 3,237 queries (questions)
- **qrels/*.tsv**: Relevance judgments mapping query IDs to document IDs with scores (2=highly relevant, 1=somewhat relevant)

**Important**: The dataset provides relevance judgments (which documents answer which queries) but does NOT include ground truth answers. We must generate synthetic ground truth offline.

**Selected Test Queries** (5 diverse candidates):

1. **PLAIN-2630**: "Alkylphenol Endocrine Disruptors and Allergies" (5 words, 21 highly relevant docs)
2. **PLAIN-2660**: "How Long to Detox From Fish Before Pregnancy?" (8 words, 20 highly relevant docs)
3. **PLAIN-2510**: "Coffee and Artery Function" (4 words, 16 highly relevant docs)
4. **PLAIN-2430**: "Preventing Brain Loss with B Vitamins?" (6 words, 15 highly relevant docs)
5. **PLAIN-2690**: "Chronic Headaches and Pork Tapeworms" (5 words, 14 highly relevant docs)

**Ground Truth Generation** (offline, pre-test):

Ground truth answers will be generated offline using a script that:
1. Loads nfcorpus dataset
2. For each selected query, extracts top 3-5 highly relevant documents
3. Uses an LLM (ollama/anthropic) to synthesize a reference answer
4. Stores ground truth in `tests/rag_evaluation/fixtures/ground_truth.json`

```python
# tools/generate_rag_ground_truth.py
async def generate_ground_truth(query: str, relevant_docs: List[dict], llm: LLMProvider) -> str:
    """Generate synthetic ground truth answer from highly relevant documents."""
    context = "\n\n".join([
        f"Document {i+1}:\nTitle: {doc['title']}\n{doc['text']}"
        for i, doc in enumerate(relevant_docs[:5])
    ])

    prompt = f"""Based on the following documents, provide a comprehensive answer to this question:

Question: {query}

{context}

Provide a factual, well-structured answer that synthesizes information from the documents.
Focus on accuracy and completeness."""

    return await llm.generate(prompt, max_tokens=500)
```

**Dataset Loading at Test Runtime** (in `nfcorpus_test_data` fixture):

1. Download nfcorpus dataset (cached in pytest temp directory)
2. Load corpus, queries, and qrels (relevance judgments)
3. Load pre-generated ground truth from `tests/rag_evaluation/fixtures/ground_truth.json`
4. Upload all corpus documents as Nextcloud notes
5. Trigger vector sync to index documents
6. Wait for indexing completion
7. Return test cases with query, ground truth, and expected doc IDs

**LLM Provider Abstraction**

```python
# tests/rag_evaluation/llm_providers.py
class LLMProvider(Protocol):
    async def generate(self, prompt: str, max_tokens: int = 100) -> str: ...

class OllamaProvider:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    async def generate(self, prompt: str, max_tokens: int = 100) -> str:
        # Use httpx to call Ollama API
        ...

class AnthropicProvider:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate(self, prompt: str, max_tokens: int = 100) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
```

### Consequences

**Positive:**

* **Actionable debugging**: Separate retrieval/generation tests pinpoint failure location
* **Industry-standard metrics**: Context Recall and Answer Correctness are recognized RAG evaluation metrics
* **Simple initial implementation**: Binary LLM evaluation (true/false) is straightforward to implement and interpret
* **Extensible framework**: Easy to add more metrics (faithfulness, relevance) later
* **Standardized benchmark**: nfcorpus provides objective comparison against published RAG systems
* **Hybrid evaluation**: Combines efficiency (heuristics for retrieval) with quality (LLM-as-judge for generation)
* **Provider flexibility**: Supports both local (Ollama) and cloud (Anthropic) LLM evaluation

**Negative:**

* **Medical domain bias**: nfcorpus is medical/biomedical content, may not represent production use cases (personal notes, calendar events, etc.)
* **Manual test execution**: Tests require external LLM access and are not integrated into CI pipeline
* **Limited initial coverage**: Starting with only 5 queries provides limited statistical confidence
* **Evaluation cost**: LLM-as-judge for generation evaluation incurs API costs (Anthropic) or requires local inference (Ollama)
* **Single metric per component**: Initial scope tests only one metric per component, missing other important quality dimensions
* **Synthetic ground truth**: Ground truth answers are LLM-generated, not human-validated, which may introduce evaluation bias
* **Large corpus upload**: Uploading 3,633 documents at test runtime may be slow; caching strategy needed

**Future Work:**

* Expand to 50-100 queries for statistical significance
* Add custom test dataset with production-representative documents (meeting notes, task lists, etc.)
* Implement additional metrics (faithfulness, context relevance, answer relevance)
* Create automated benchmarking dashboard to track metric trends
* Test multi-hop reasoning (synthesis questions requiring multiple documents)
* Evaluate out-of-scope handling ("I don't know" responses)
