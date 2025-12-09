# Ollama Embeddings Investigation

**Date**: 2025-10-30
**Status**: Recommendation for Integration

## Executive Summary

Ollama provides a **local, self-hosted embedding solution** that is excellent for **development and small-scale deployments** but has **performance limitations** compared to specialized embedding inference engines (TEI, Infinity).

**Recommendation**: Include Ollama as **Tier 2 fallback** in our embedding strategy (after cloud APIs, before local sentence-transformers), prioritizing ease of setup over maximum performance.

## Overview

Ollama is primarily known as a local LLM runner but added embedding model support in version 0.1.26, making it a convenient option for generating vector embeddings without external API dependencies.

### Key Characteristics

- **Local & Self-Hosted**: No external API calls, full privacy
- **Easy Setup**: Single binary, simple model downloads (`ollama pull nomic-embed-text`)
- **Unified Platform**: Same tool for both LLMs and embeddings
- **OpenAI Compatible**: `/v1/embeddings` endpoint for drop-in replacement
- **Multi-Platform**: Linux, macOS, Windows support
- **GPU Support**: CUDA, ROCm, Metal acceleration

## API Details

### Endpoint Structure

**New API** (recommended):
```bash
POST http://localhost:11434/api/embed
```

**OpenAI Compatible**:
```bash
POST http://localhost:11434/v1/embeddings
```

**Legacy API** (deprecated):
```bash
POST http://localhost:11434/api/embeddings
```

### Request Format

**Single Text Embedding**:
```json
{
  "model": "nomic-embed-text",
  "input": "Text to embed"
}
```

**Batch Embedding** (since v0.2.0):
```json
{
  "model": "nomic-embed-text",
  "input": [
    "First text to embed",
    "Second text to embed",
    "Third text to embed"
  ]
}
```

### Response Format

```json
{
  "model": "nomic-embed-text",
  "embeddings": [
    [0.123, -0.456, 0.789, ...],  // 768 dimensions for nomic-embed-text
    [0.234, -0.567, 0.890, ...]
  ]
}
```

### Python Integration

```python
import ollama

# Single embedding
response = ollama.embed(
    model='nomic-embed-text',
    input='Text to embed'
)
embedding = response['embeddings'][0]

# Batch embeddings (more efficient)
response = ollama.embed(
    model='nomic-embed-text',
    input=[
        'First text',
        'Second text',
        'Third text'
    ]
)
embeddings = response['embeddings']
```

## Available Models

### 1. nomic-embed-text (Recommended)

**Specifications**:
- **Parameters**: 137M
- **Dimensions**: 768
- **Context Length**: 8,192 tokens (2K effective)
- **Size**: 274MB
- **Architecture**: BERT-based

**Performance**:
- Outperforms OpenAI `text-embedding-ada-002` and `text-embedding-3-small`
- Excellent for long-context tasks
- Strong general-purpose performance

**Use Cases**:
- General RAG applications
- Long document processing
- Semantic search
- Document clustering

**Pull Command**:
```bash
ollama pull nomic-embed-text
```

### 2. mxbai-embed-large

**Specifications**:
- **Parameters**: 334M
- **Dimensions**: 1,024
- **Context Length**: 512 tokens
- **Architecture**: BERT-large optimized

**Performance**:
- Claims to outperform commercial models
- Higher precision for complex queries
- Best quality but slower

**Use Cases**:
- High-precision semantic search
- Enterprise knowledge bases
- Multilingual content

**Pull Command**:
```bash
ollama pull mxbai-embed-large
```

### 3. all-minilm

**Specifications**:
- **Parameters**: 23M
- **Dimensions**: 384
- **Context Length**: 256 tokens
- **Size**: Smallest footprint

**Performance**:
- Fastest processing speed
- Good for sentence-level tasks
- Limited context window

**Use Cases**:
- Real-time applications
- Resource-constrained environments
- High-throughput scenarios
- Development/testing

**Pull Command**:
```bash
ollama pull all-minilm
```

## Performance Benchmarks

### Throughput Comparison

| Hardware | Model | Batch Size | Throughput | Notes |
|----------|-------|------------|------------|-------|
| RTX 4090 (24GB) | nomic-embed-text | 256 | 12,450 tok/sec | GPU-accelerated |
| RTX 4090 (24GB) | mxbai-embed-large | 128 | 8,920 tok/sec | GPU-accelerated |
| Intel i9-13900K (CPU) | nomic-embed-text | 32 | 3,250 tok/sec | CPU-only |
| Intel i9-13900K (CPU) | mxbai-embed-large | 16 | 2,180 tok/sec | CPU-only |

### Latency Comparison

**Single Request Latency** (RTX 4060):
- Ollama: ~99ms
- TEI: ~20ms (5x faster)
- Infinity: ~30-40ms (2.5-3x faster)

**Batch Processing**:
- Optimal batch size: 32-64 (model dependent)
- Performance degrades with batches >16 (quality issues reported)
- 2x slower than direct sentence-transformers usage

### Engine Comparison

Based on benchmarks from Baseten (2024):

| Engine | Relative Throughput | Notes |
|--------|---------------------|-------|
| BEI | 9.0x (baseline) | Fastest (proprietary) |
| TEI | 4.5x | Open source, Rust-based |
| Infinity | 3.5x | PyTorch/ONNX optimized |
| vLLM | 3.0x | General LLM inference |
| **Ollama** | **1.0x** | Slowest for embeddings |

**Key Insight**: Ollama is **5-9x slower** than specialized embedding engines but trades performance for ease of use and unified platform.

## Integration Implementation

### Python Client Wrapper

```python
# nextcloud_mcp_server/embeddings/ollama.py
import httpx
from typing import List


class OllamaEmbedding:
    """Ollama embedding provider"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text"
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=60.0)

        # Model dimension mapping
        self.dimensions = {
            "nomic-embed-text": 768,
            "mxbai-embed-large": 1024,
            "all-minilm": 384
        }
        self.dimension = self.dimensions.get(model, 768)

    async def embed(self, text: str) -> List[float]:
        """Generate embedding for single text"""
        response = await self.client.post(
            f"{self.base_url}/api/embed",
            json={
                "model": self.model,
                "input": text
            }
        )
        response.raise_for_status()
        data = response.json()
        return data["embeddings"][0]

    async def embed_batch(
        self,
        texts: List[str],
        batch_size: int = 32
    ) -> List[List[float]]:
        """
        Generate embeddings for multiple texts in batches.

        Note: Ollama has reported quality issues with batch sizes >16.
        We use batch_size=32 as default but allow configuration.
        """
        all_embeddings = []

        # Process in chunks to avoid batch size issues
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]

            response = await self.client.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": self.model,
                    "input": batch
                }
            )
            response.raise_for_status()
            data = response.json()
            all_embeddings.extend(data["embeddings"])

        return all_embeddings

    async def check_health(self) -> bool:
        """Check if Ollama server is running and model is available"""
        try:
            # Check if server is up
            response = await self.client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()

            # Check if model is pulled
            models = response.json().get("models", [])
            model_names = [m["name"] for m in models]

            if self.model not in model_names:
                raise ValueError(
                    f"Model '{self.model}' not found. "
                    f"Run: ollama pull {self.model}"
                )

            return True

        except Exception as e:
            raise ConnectionError(f"Ollama health check failed: {e}")

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
```

### Auto-Detection in Embedding Service

```python
# nextcloud_mcp_server/embeddings/service.py
from typing import Optional
import os
import logging

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Unified embedding service with automatic provider detection"""

    def __init__(self):
        self.provider = None
        self._detect_provider()

    def _detect_provider(self):
        """Auto-detect available embedding provider"""

        # Tier 1: OpenAI API (best quality)
        if os.getenv("OPENAI_API_KEY"):
            from .openai import OpenAIEmbedding
            self.provider = OpenAIEmbedding(
                model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
                api_key=os.getenv("OPENAI_API_KEY")
            )
            logger.info("✓ Using OpenAI embeddings")
            return

        # Tier 2a: Infinity (optimized self-hosted)
        if os.getenv("INFINITY_URL"):
            from .infinity import InfinityEmbedding
            try:
                self.provider = InfinityEmbedding(
                    url=os.getenv("INFINITY_URL"),
                    model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
                )
                logger.info("✓ Using Infinity embeddings (optimized)")
                return
            except Exception as e:
                logger.warning(f"Infinity unavailable: {e}")

        # Tier 2b: Ollama (easy self-hosted)
        if os.getenv("OLLAMA_URL"):
            from .ollama import OllamaEmbedding
            try:
                self.provider = OllamaEmbedding(
                    base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
                    model=os.getenv("OLLAMA_MODEL", "nomic-embed-text")
                )
                # Verify Ollama is running and model is available
                import asyncio
                asyncio.run(self.provider.check_health())
                logger.info("✓ Using Ollama embeddings (easy setup)")
                return
            except Exception as e:
                logger.warning(f"Ollama unavailable: {e}")

        # Tier 3: Local model (fallback)
        logger.warning("No cloud/hosted embeddings available, using local model")
        from .local import LocalEmbedding
        self.provider = LocalEmbedding(
            model=os.getenv("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        )
        logger.info("✓ Using local embeddings (CPU fallback)")

    async def embed(self, text: str):
        """Generate embedding for text"""
        return await self.provider.embed(text)

    async def embed_batch(self, texts: list[str]):
        """Generate embeddings for multiple texts"""
        return await self.provider.embed_batch(texts)

    @property
    def dimension(self) -> int:
        """Get embedding dimension"""
        return self.provider.dimension
```

### Docker Compose Configuration

```yaml
services:
  # Ollama embedding service
  ollama:
    image: ollama/ollama:latest
    restart: always
    ports:
      - 127.0.0.1:11434:11434
    volumes:
      - ollama_models:/root/.ollama
    # Optional: GPU support
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    # Pull models on startup
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        ollama serve &
        sleep 5
        ollama pull nomic-embed-text
        wait

  # MCP Server with Ollama embeddings
  mcp:
    build: .
    depends_on:
      - ollama
    environment:
      # ... other vars ...
      - OLLAMA_URL=http://ollama:11434
      - OLLAMA_MODEL=nomic-embed-text

  # Vector sync worker
  mcp-vector-sync:
    build: .
    command: ["python", "-m", "nextcloud_mcp_server.sync.vector_indexer"]
    depends_on:
      - ollama
      - qdrant
    environment:
      # ... other vars ...
      - OLLAMA_URL=http://ollama:11434
      - OLLAMA_MODEL=nomic-embed-text

volumes:
  ollama_models:
```

## Advantages of Ollama

### 1. **Ease of Setup**

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull embedding model
ollama pull nomic-embed-text

# Done! API available at localhost:11434
```

No complex configuration, no Docker registries, no model conversion.

### 2. **Privacy & Data Sovereignty**

- All processing happens locally
- No data leaves your infrastructure
- No API keys or external dependencies
- Ideal for sensitive content (medical, legal, financial)

### 3. **Unified Platform**

- Same tool for LLMs and embeddings
- Consistent API across model types
- Single point of management
- Simplified operations

### 4. **Developer Experience**

- Simple API (similar to OpenAI)
- Good documentation
- Active community
- Framework integrations (LangChain, LlamaIndex)

### 5. **Cost**

- Free and open source
- No per-token API costs
- Only infrastructure costs (compute)

### 6. **Model Variety**

Growing library of embedding models:
- nomic-embed-text (general purpose)
- mxbai-embed-large (high quality)
- all-minilm (fast)
- More models added regularly

## Limitations of Ollama

### 1. **Performance**

- **5-9x slower** than specialized engines (TEI, Infinity)
- Not optimized specifically for embedding inference
- Batch processing issues at larger batch sizes (>16)
- Higher latency compared to alternatives

### 2. **Scalability**

- Single-instance deployment (no native clustering)
- Limited concurrent request handling
- Not designed for high-throughput production
- Resource usage per request is higher

### 3. **Batch Processing Issues**

- Quality degradation reported with large batches
- Optimal batch size: 32-64 (conservative)
- Less efficient than specialized engines
- GitHub issues tracking batch problems (#6262)

### 4. **Resource Usage**

- Models stay loaded in memory (VRAM/RAM)
- Higher memory footprint per model
- GPU context switching overhead
- Not as memory-efficient as specialized engines

### 5. **Production Features**

- No built-in load balancing
- Limited monitoring/metrics
- No automatic scaling
- Basic error handling

## Use Case Recommendations

### ✅ **Excellent For:**

1. **Development & Testing**
   - Quick setup for prototyping
   - Local development environments
   - Testing embedding pipelines

2. **Small Deployments**
   - <10 users
   - <10,000 documents
   - Infrequent searches (<100/day)
   - Hobbyist/personal projects

3. **Privacy-Critical Applications**
   - Medical/healthcare records
   - Legal documents
   - Financial data
   - Air-gapped environments

4. **Unified LLM Stack**
   - Projects already using Ollama for LLMs
   - Simplified operations
   - Consistent tooling

5. **Educational/Learning**
   - Teaching RAG concepts
   - Learning embeddings
   - Hackathons/workshops

### ⚠️ **Consider Alternatives For:**

1. **Production at Scale**
   - >100 users
   - >100,000 documents
   - High query volume (>1000/day)
   - Use: TEI or Infinity

2. **Performance-Critical**
   - Real-time search (<50ms latency)
   - High-throughput batch processing
   - Use: TEI with GPU

3. **Enterprise Deployments**
   - Need for high availability
   - Load balancing requirements
   - Advanced monitoring
   - Use: Managed services or TEI cluster

4. **Large-Scale Indexing**
   - Millions of documents
   - Continuous high-volume ingestion
   - Use: Infinity or commercial solutions

## Integration Strategy

### Recommended Tier Placement

**Update ADR-003 embedding strategy:**

```
Tier 1: OpenAI API (best quality, requires API key)
  ↓ fallback
Tier 2a: Infinity (optimized self-hosted, complex setup)
  ↓ fallback
Tier 2b: Ollama (easy self-hosted, moderate performance) ← NEW
  ↓ fallback
Tier 3: Local sentence-transformers (CPU fallback, simplest)
```

### Configuration

```bash
# Option 1: Use Infinity (if available)
INFINITY_URL=http://infinity:7997
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# Option 2: Use Ollama (if Infinity unavailable)
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=nomic-embed-text

# Option 3: Use local model (automatic fallback)
# No configuration needed
```

### When to Choose Ollama

**Choose Ollama if**:
- You're already using Ollama for LLMs
- You need privacy/data sovereignty
- You have <10k documents and <100 users
- Ease of setup is more important than max performance
- You're in development/testing phase

**Choose Infinity/TEI if**:
- You need maximum throughput (>1000 embeddings/sec)
- You have >100k documents
- Latency is critical (<50ms)
- You're in production with >100 users

**Choose OpenAI API if**:
- You're okay with cloud dependencies
- You need best-in-class quality
- Cost is not a concern (~$0.02 per 1M tokens)

## Production Deployment Guidance

### Small Production (Ollama Acceptable)

**Profile**:
- 5-20 users
- 1,000-10,000 documents
- 50-200 searches/day
- <2 sec acceptable latency

**Configuration**:
```yaml
ollama:
  image: ollama/ollama:latest
  deploy:
    resources:
      limits:
        memory: 4GB
        cpus: "2.0"
      reservations:
        devices:
          - driver: nvidia  # GPU if available
            count: 1
            capabilities: [gpu]
  environment:
    - OLLAMA_NUM_PARALLEL=2  # Concurrent requests
```

**Expected Performance**:
- Embedding latency: 100-200ms
- Throughput: 5-10 embeddings/sec
- Memory: 2-3GB (model loaded)

### Medium Production (Use Infinity/TEI)

**Profile**:
- 20-200 users
- 10,000-1M documents
- 500-5,000 searches/day
- <500ms acceptable latency

**Recommendation**: Migrate to Infinity or TEI
```yaml
infinity:
  image: michaelf34/infinity:latest
  # Better throughput and latency
```

### Large Production (Use Specialized Solution)

**Profile**:
- >200 users
- >1M documents
- >5,000 searches/day
- <100ms required latency

**Recommendation**: Use TEI cluster or commercial service

## Monitoring Considerations

### Key Metrics to Track

```python
# Add Ollama-specific metrics
from prometheus_client import Histogram, Counter, Gauge

ollama_embedding_latency = Histogram(
    'ollama_embedding_duration_seconds',
    'Ollama embedding generation time',
    ['model', 'batch_size']
)

ollama_batch_size = Gauge(
    'ollama_batch_size',
    'Current batch size being processed'
)

ollama_errors = Counter(
    'ollama_errors_total',
    'Ollama embedding errors',
    ['error_type']
)
```

### Health Checks

```python
async def ollama_health_check():
    """Check Ollama availability"""
    try:
        async with httpx.AsyncClient() as client:
            # Check server
            response = await client.get("http://ollama:11434/api/tags")
            response.raise_for_status()

            # Verify model loaded
            models = response.json().get("models", [])
            if "nomic-embed-text" not in [m["name"] for m in models]:
                return False, "Model not pulled"

            return True, "OK"
    except Exception as e:
        return False, str(e)
```

## Migration Path

### Starting with Ollama

**Phase 1: Development** (Ollama)
- Use Ollama for initial development
- Validate embedding pipeline
- Test search quality

**Phase 2: Growth** (Ollama → Infinity)
- Monitor performance metrics
- When >50 users or >10k docs, migrate to Infinity
- Simple config change, no code changes

**Phase 3: Scale** (Infinity → TEI/Commercial)
- When >200 users or performance issues
- Consider TEI cluster or managed services

### Code Compatibility

All embedding providers use the same interface:
```python
# Works with Ollama, Infinity, OpenAI, Local
embedding = await embedding_service.embed(text)
embeddings = await embedding_service.embed_batch(texts)
```

**Migration is a configuration change only** - no code rewrite needed.

## Conclusion

**Ollama is a solid choice for:**
- Early-stage projects
- Development/testing
- Privacy-critical applications
- Small deployments (<10 users, <10k docs)
- Unified LLM + embedding stack

**But recognize its limitations:**
- 5-9x slower than specialized engines
- Not designed for high-throughput production
- Batch processing can be problematic
- Limited scalability

**Recommendation**:
✅ **Include Ollama as Tier 2b** (after Infinity, before local models) in the embedding strategy. It provides a good balance of ease-of-use and privacy for small-to-medium deployments while allowing seamless migration to more performant engines as needs grow.

The key is designing the abstraction layer (as done in ADR-003) so migration between engines requires only configuration changes, not code rewrites.
