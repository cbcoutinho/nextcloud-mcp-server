# ADR-015: Unified Provider Architecture for Embeddings and Text Generation

**Status:** Accepted
**Date:** 2025-01-16
**Deciders:** Development Team
**Related:** ADR-003 (Vector Database), ADR-008 (MCP Sampling), ADR-013 (RAG Evaluation)

## Context

Prior to this refactoring, the codebase had two separate provider systems:

1. **Embedding Providers** (`nextcloud_mcp_server/embedding/`)
   - Used `EmbeddingProvider` ABC with methods: `embed()`, `embed_batch()`, `get_dimension()`
   - Had auto-detection via `EmbeddingService._detect_provider()`
   - Used for semantic search and vector indexing (production)

2. **LLM Providers** (`tests/rag_evaluation/llm_providers.py`)
   - Used `LLMProvider` Protocol with method: `generate()`
   - Had separate factory function `create_llm_provider()`
   - Used only for RAG evaluation tests (not production)

This fragmentation created several problems:

### Problems with Dual Provider Systems

1. **Code Duplication**
   - Ollama configuration appeared in both `embedding/service.py` and `tests/rag_evaluation/llm_providers.py`
   - Similar provider detection logic in multiple places
   - Separate singleton patterns for each system

2. **Limited Extensibility**
   - Hard-coded provider detection in `EmbeddingService._detect_provider()`
   - No support for providers that offer both capabilities (like Bedrock)
   - Adding new providers required modifying multiple files

3. **Inconsistent Patterns**
   - BM25 provider didn't follow `EmbeddingProvider` ABC
   - Different method names across providers (`embed` vs `encode`)
   - ABC vs Protocol for type checking

4. **Difficult Scaling**
   - Adding Amazon Bedrock (our third provider) would exacerbate all issues
   - No clear path for future providers (OpenAI, Cohere, etc.)

### Amazon Bedrock Requirements

Bedrock naturally supports **both** embeddings and text generation:
- **Embeddings**: `amazon.titan-embed-text-v1/v2`, `cohere.embed-*`
- **Text Generation**: `anthropic.claude-*`, `meta.llama3-*`, `amazon.titan-text-*`
- **Unified API**: Single `invoke_model()` method via bedrock-runtime

This made it the perfect opportunity to establish a unified provider architecture.

## Decision

We refactored the provider infrastructure to use a **unified Provider ABC** with optional capabilities:

### 1. Unified Provider Interface

**New Structure:**
```
nextcloud_mcp_server/providers/
├── __init__.py
├── base.py              # Provider ABC with optional capabilities
├── registry.py          # Auto-detection and factory
├── ollama.py            # Supports both embedding + generation
├── anthropic.py         # Generation only
├── bedrock.py           # Supports both embedding + generation
└── simple.py            # Embedding only (testing fallback)
```

**Base Class (`providers/base.py`):**
```python
class Provider(ABC):
    @property
    @abstractmethod
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        pass

    @property
    @abstractmethod
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        pass

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding (raises NotImplementedError if not supported)."""
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate batch embeddings (raises NotImplementedError if not supported)."""
        pass

    @abstractmethod
    def get_dimension(self) -> int:
        """Get embedding dimension (raises NotImplementedError if not supported)."""
        pass

    @abstractmethod
    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """Generate text (raises NotImplementedError if not supported)."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close provider and release resources."""
        pass
```

### 2. Provider Registry

**Auto-Detection Priority** (`providers/registry.py`):
```python
class ProviderRegistry:
    @staticmethod
    def create_provider() -> Provider:
        # 1. Bedrock (AWS_REGION or BEDROCK_*_MODEL)
        # 2. Ollama (OLLAMA_BASE_URL)
        # 3. Simple (fallback)
```

**Environment Variables:**

**Bedrock:**
- `AWS_REGION`: AWS region (e.g., "us-east-1")
- `AWS_ACCESS_KEY_ID`: AWS access key (optional, uses credential chain)
- `AWS_SECRET_ACCESS_KEY`: AWS secret key (optional)
- `BEDROCK_EMBEDDING_MODEL`: Model ID for embeddings (e.g., "amazon.titan-embed-text-v2:0")
- `BEDROCK_GENERATION_MODEL`: Model ID for text generation (e.g., "anthropic.claude-3-sonnet-20240229-v1:0")

**Ollama:**
- `OLLAMA_BASE_URL`: Ollama API base URL (e.g., "http://localhost:11434")
- `OLLAMA_EMBEDDING_MODEL`: Model for embeddings (default: "nomic-embed-text")
- `OLLAMA_GENERATION_MODEL`: Model for text generation (e.g., "llama3.2:1b")
- `OLLAMA_VERIFY_SSL`: Verify SSL certificates (default: "true")

**Simple (no configuration, fallback):**
- `SIMPLE_EMBEDDING_DIMENSION`: Embedding dimension (default: 384)

### 3. Backward Compatibility

**Old Code Continues to Work:**
```python
# Old way (still works)
from nextcloud_mcp_server.embedding import get_embedding_service

service = get_embedding_service()  # Returns singleton Provider
embeddings = await service.embed_batch(texts)
```

**New Way (recommended):**
```python
# New way (cleaner)
from nextcloud_mcp_server.providers import get_provider

provider = get_provider()  # Returns singleton Provider
embeddings = await provider.embed_batch(texts)

# Can also use generation if provider supports it
if provider.supports_generation:
    text = await provider.generate("prompt")
```

**Migration Path:**
- `embedding/service.py` now wraps `providers.get_provider()` for compatibility
- `tests/rag_evaluation/llm_providers.py` now uses unified providers
- Old imports still work, marked as deprecated in docstrings

### 4. Amazon Bedrock Implementation

**Features:**
- Supports both embeddings and text generation
- Model-specific request/response handling for:
  - Titan Embed (amazon.titan-embed-text-*)
  - Cohere Embed (cohere.embed-*)
  - Claude (anthropic.claude-*)
  - Llama (meta.llama3-*)
  - Titan Text (amazon.titan-text-*)
  - Mistral (mistral.*)
- Uses boto3 bedrock-runtime client
- Graceful degradation if boto3 not installed
- Async implementation matching existing patterns

**Model-Specific Handling:**
```python
# Bedrock embedding request (Titan)
{"inputText": text}

# Bedrock generation request (Claude)
{
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": max_tokens,
    "temperature": 0.7,
    "messages": [{"role": "user", "content": prompt}]
}
```

## Consequences

### Positive

1. **Sustainable Provider Additions**
   - New providers only need to implement `Provider` ABC
   - Auto-detection via environment variables
   - No modifications to existing code required

2. **Code Consolidation**
   - Single provider interface instead of two
   - Unified configuration pattern
   - Eliminated duplication

3. **Better Extensibility**
   - Providers can support one or both capabilities
   - Clear capability detection via properties
   - Registry pattern simplifies auto-detection

4. **Improved Testing**
   - RAG evaluation can use any provider (Ollama, Anthropic, Bedrock)
   - Comprehensive unit tests for all providers
   - Mocked boto3 tests for Bedrock

5. **Production-Ready Bedrock Support**
   - Full embedding and generation support
   - Multiple model families supported
   - AWS credential chain integration

### Neutral

1. **Optional Boto3 Dependency**
   - boto3 is dev dependency only (not required for core functionality)
   - Bedrock provider gracefully fails if boto3 not installed
   - Users who want Bedrock must `pip install boto3`

2. **Capability Properties**
   - All providers must implement capability properties
   - Methods raise `NotImplementedError` if capability not supported
   - Clear error messages guide users to alternatives

### Negative

1. **Migration Effort**
   - Existing code must be migrated to new imports (optional, backward compatible)
   - Documentation needs updating
   - Users must learn new environment variables

2. **Increased Complexity**
   - Provider base class has more methods (embedding + generation)
   - More environment variables to configure
   - Capability detection adds runtime checks

## Implementation

### Files Created

**New Provider Infrastructure:**
- `nextcloud_mcp_server/providers/__init__.py`
- `nextcloud_mcp_server/providers/base.py`
- `nextcloud_mcp_server/providers/registry.py`
- `nextcloud_mcp_server/providers/ollama.py`
- `nextcloud_mcp_server/providers/anthropic.py`
- `nextcloud_mcp_server/providers/bedrock.py`
- `nextcloud_mcp_server/providers/simple.py`

**Tests:**
- `tests/unit/providers/__init__.py`
- `tests/unit/providers/test_bedrock.py` (9 unit tests)

**Documentation:**
- `docs/ADR-015-unified-provider-architecture.md` (this file)

### Files Modified

**Backward Compatibility:**
- `nextcloud_mcp_server/embedding/service.py` - Now wraps `get_provider()`
- `tests/rag_evaluation/llm_providers.py` - Uses unified providers

**Dependencies:**
- `pyproject.toml` - Added `boto3>=1.35.0` to dev dependencies

### Testing Results

**Unit Tests:** 127 passed (including 9 new Bedrock tests)
**Type Checking:** All checks passed (ty)
**Linting:** All checks passed (ruff)
**Backward Compatibility:** Verified - existing embedding tests work

## Alternatives Considered

### Alternative 1: Keep Separate Provider Systems

**Pros:**
- No refactoring needed
- Simpler short-term

**Cons:**
- Bedrock would need to be implemented twice
- Continued code duplication
- No long-term scalability

**Decision:** Rejected - technical debt would continue to grow

### Alternative 2: Separate Embedding and Generation Providers

Use composition instead of unified interface:
```python
class CombinedProvider:
    def __init__(self, embedding: EmbeddingProvider, generation: LLMProvider):
        self.embedding = embedding
        self.generation = generation
```

**Pros:**
- Clearer separation of concerns
- Simpler individual providers

**Cons:**
- Bedrock and Ollama naturally do both - artificial separation
- More complex configuration (two providers to configure)
- More boilerplate code

**Decision:** Rejected - unified interface better matches provider capabilities

### Alternative 3: Plugin System

Dynamic provider registration via entry points:
```python
# setup.py
entry_points={
    'nextcloud_mcp.providers': [
        'ollama = nextcloud_mcp_server.providers.ollama:OllamaProvider',
        'bedrock = nextcloud_mcp_server.providers.bedrock:BedrockProvider',
    ]
}
```

**Pros:**
- Most extensible
- Third-party providers possible

**Cons:**
- Over-engineered for current needs
- Added complexity
- No immediate benefit

**Decision:** Deferred - can add later if needed

## Future Work

1. **Additional Providers**
   - OpenAI (embeddings + generation)
   - Cohere (embeddings + generation)
   - Google Vertex AI
   - Azure OpenAI

2. **Provider Features**
   - Streaming generation support
   - Batch API optimization (when available)
   - Model-specific optimizations
   - Cost tracking and metrics

3. **Configuration Improvements**
   - Provider profiles (development, production)
   - Model aliasing (e.g., "small", "large")
   - Fallback provider chains

4. **Testing**
   - Integration tests with real Bedrock endpoints
   - Performance benchmarking across providers
   - Cost comparison analysis

## References

- [boto3 Bedrock Runtime Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-runtime.html)
- [Amazon Bedrock User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)
- ADR-003: Vector Database and Semantic Search
- ADR-008: MCP Sampling for Semantic Search
- ADR-013: RAG Evaluation Framework
