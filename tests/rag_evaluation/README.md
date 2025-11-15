# RAG Evaluation Tests

This directory contains tests for evaluating the Retrieval-Augmented Generation (RAG) system in the Nextcloud MCP server, specifically the `nc_semantic_search_answer` tool.

## Architecture

The RAG system has two components that are tested independently:

1. **Retrieval** - Vector sync/embedding pipeline (indexed Nextcloud documents → vector database)
2. **Generation** - MCP client LLM synthesis (retrieved context → natural language answer)

See [ADR-013](../../docs/ADR-013-rag-evaluation.md) for full architectural details.

## Test Structure

```
tests/rag_evaluation/
├── README.md                       # This file
├── conftest.py                     # Pytest fixtures
├── llm_providers.py                # LLM provider abstraction (Ollama/Anthropic)
├── fixtures/
│   └── ground_truth.json           # Pre-generated reference answers
├── test_retrieval_quality.py       # Retrieval evaluation (Context Recall)
└── test_generation_quality.py      # Generation evaluation (Answer Correctness)
```

## Metrics

### Retrieval Evaluation
- **Metric**: Context Recall
- **Method**: Heuristic - Check if ground-truth document IDs appear in top-k results
- **Target**: ≥80% recall

### Generation Evaluation
- **Metric**: Answer Correctness
- **Method**: LLM-as-judge - Compare RAG answer vs ground truth (binary true/false)
- **Evaluation**: External LLM evaluates semantic equivalence

## Dataset

**BeIR/nfcorpus** - Medical/biomedical corpus with ~3,600 documents

**Test Queries** (5 selected):
1. PLAIN-2630: "Alkylphenol Endocrine Disruptors and Allergies" (21 relevant docs)
2. PLAIN-2660: "How Long to Detox From Fish Before Pregnancy?" (20 relevant docs)
3. PLAIN-2510: "Coffee and Artery Function" (16 relevant docs)
4. PLAIN-2430: "Preventing Brain Loss with B Vitamins?" (15 relevant docs)
5. PLAIN-2690: "Chronic Headaches and Pork Tapeworms" (14 relevant docs)

## Setup

### 1. Install Dependencies

```bash
uv sync --group dev
```

This installs:
- `anthropic>=0.42.0` - For Anthropic LLM evaluation
- `click>=8.1.8` - For CLI interface
- `datasets>=3.3.0` - For BeIR nfcorpus dataset loading

### 2. Configure LLM Provider

Set environment variables for your LLM provider:

**Option A: Ollama (default, local/remote)**
```bash
export RAG_EVAL_PROVIDER=ollama
export OLLAMA_HOST=https://ollama.example.com  # or RAG_EVAL_OLLAMA_BASE_URL
export RAG_EVAL_OLLAMA_MODEL=llama3.2:1b
```

**Option B: Anthropic (cloud)**
```bash
export RAG_EVAL_PROVIDER=anthropic
export RAG_EVAL_ANTHROPIC_API_KEY=sk-ant-...
export RAG_EVAL_ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
```

### 3. One-Time Setup: Generate Ground Truth

Generate synthetic reference answers for the 5 test queries:

```bash
uv run python tools/rag_eval_cli.py generate
```

**What this does:**
- Downloads nfcorpus dataset to `tests/rag_evaluation/fixtures/nfcorpus/` (cached locally)
- For each of the 5 selected queries, extracts highly relevant documents
- Uses configured LLM to synthesize a reference answer
- Saves to `tests/rag_evaluation/fixtures/ground_truth.json`

**Optional flags:**
- `--provider ollama|anthropic` - Override LLM provider
- `--model MODEL_NAME` - Override model name
- `--force-download` - Re-download nfcorpus dataset

### 4. One-Time Setup: Upload Corpus to Nextcloud

Upload all 3,633 nfcorpus documents as Nextcloud notes:

```bash
uv run python tools/rag_eval_cli.py upload \
    --nextcloud-url http://localhost:8000 \
    --username admin \
    --password admin
```

**What this does:**
- Downloads nfcorpus dataset (if not already cached)
- Uploads all documents as notes in Nextcloud
- Saves document ID → note ID mapping to `tests/rag_evaluation/fixtures/note_mapping.json`

**Optional flags:**
- `--category CATEGORY` - Custom category for notes (default: `nfcorpus_rag_eval`)
- `--force-download` - Re-download nfcorpus dataset

**Important:** This step requires:
- A running Nextcloud instance with vector sync enabled
- Notes app installed
- Valid credentials

**Duration:** ~10-15 minutes to upload 3,633 documents

## Running Tests

### Run All RAG Evaluation Tests

```bash
uv run pytest tests/rag_evaluation/ -v
```

### Run Specific Test Suites

**Retrieval Quality Only:**
```bash
uv run pytest tests/rag_evaluation/test_retrieval_quality.py -v
```

**Generation Quality Only:**
```bash
uv run pytest tests/rag_evaluation/test_generation_quality.py -v
```

### Run Individual Tests

```bash
uv run pytest tests/rag_evaluation/test_retrieval_quality.py::test_retrieval_context_recall -v
uv run pytest tests/rag_evaluation/test_generation_quality.py::test_answer_correctness -v
```

## Test Execution Flow

**Prerequisites** (one-time setup):
1. Generated ground truth (`tools/rag_eval_cli.py generate`)
2. Uploaded corpus to Nextcloud (`tools/rag_eval_cli.py upload`)

### Retrieval Quality Tests

1. **Setup** (`nfcorpus_test_data` fixture):
   - Loads pre-generated ground truth from `fixtures/ground_truth.json`
   - Loads note mapping from `fixtures/note_mapping.json`
   - Returns test cases with expected note IDs

2. **Test** (`test_retrieval_context_recall`):
   - For each query: Perform semantic search (top-10)
   - Extract retrieved note IDs
   - Calculate Context Recall = (expected ∩ retrieved) / expected
   - Assert recall ≥ 80%

3. **Cleanup**:
   - None required (notes persist in Nextcloud for reuse)

### Generation Quality Tests

1. **Setup**:
   - Same as retrieval tests (reuses `nfcorpus_test_data` fixture)
   - Creates evaluation LLM provider

2. **Test** (`test_answer_correctness`):
   - For each query: Call `nc_semantic_search_answer` MCP tool
   - Extract generated answer
   - Use LLM-as-judge to compare vs ground truth
   - Assert semantic equivalence (TRUE/FALSE)

3. **Cleanup**:
   - LLM provider closed

## Expected Test Duration

**One-time setup:**
- **Generate ground truth**: ~5-10 minutes (5 queries with LLM generation)
- **Upload corpus**: ~10-15 minutes (3,633 documents)
- **Total setup**: ~15-25 minutes

**Test execution** (after setup):
- **Retrieval tests**: ~1-2 minutes (5 queries, no upload/cleanup)
- **Generation tests**: ~5-10 minutes (RAG generation + LLM evaluation)
- **Total per run**: ~6-12 minutes

**Note**: These are NOT smoke tests and are NOT run in CI.

## Limitations & Future Work

**Current Limitations:**
- Only 5 test queries (limited statistical confidence)
- Medical domain bias (may not represent production use cases)
- Synthetic ground truth (LLM-generated, not human-validated)
- Manual test execution (requires external LLM access)

**Future Enhancements:**
- Expand to 50-100 queries for statistical significance
- Add custom test dataset with production-representative documents
- Implement additional metrics (faithfulness, context relevance, answer relevance)
- Create automated benchmarking dashboard
- Test multi-hop reasoning (synthesis questions)
- Evaluate out-of-scope handling ("I don't know" responses)

## Troubleshooting

### Tests Fail with "Ground truth file not found"

Run the generate command first:
```bash
uv run python tools/rag_eval_cli.py generate
```

### Tests Fail with "Note mapping file not found"

Run the upload command first:
```bash
uv run python tools/rag_eval_cli.py upload --nextcloud-url http://localhost:8000 --username admin --password admin
```

### Tests Fail with "MCP sampling client not yet implemented"

The `mcp_sampling_client` fixture is a placeholder. You need to implement MCP client creation with sampling support. See the TODO in `conftest.py`.

### Upload Command Fails

Common issues:
1. **Nextcloud not running**: Ensure Nextcloud is accessible at the URL
2. **Invalid credentials**: Verify username/password
3. **Notes app not installed**: Install Notes app in Nextcloud
4. **Network timeout**: Increase timeout in CLI (currently 60s)

### LLM Timeout

If ground truth generation times out:
1. Increase timeout in `llm_providers.py` (currently 10 min)
2. Use a faster model: `--model llama3.2:1b`
3. Check Ollama/Anthropic service availability

### Dataset Download Fails

The nfcorpus dataset is downloaded automatically. If download fails:
1. Check internet connection
2. Manually download from: https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip
3. Extract to `tests/rag_evaluation/fixtures/nfcorpus/`
4. Or use HuggingFace datasets cache: `~/.cache/huggingface/datasets/BeIR___nfcorpus/`

### Vector Sync Not Indexing Documents

After uploading, vector sync must index the documents:
1. Check vector sync is enabled in Nextcloud
2. Trigger manual sync if needed
3. Wait for background job to process all documents
4. Verify in Qdrant that vectors exist for uploaded notes

## References

- [ADR-013: RAG Evaluation Testing Framework](../../docs/ADR-013-rag-evaluation.md)
- [ADR-008: MCP Sampling for Semantic Search](../../docs/ADR-008-mcp-sampling-for-semantic-search.md)
- [BeIR Benchmark](https://github.com/beir-cellar/beir)
- [NFCorpus Dataset](https://www.cl.uni-heidelberg.de/statnlpgroup/nfcorpus/)
