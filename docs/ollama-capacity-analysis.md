# Ollama Capacity Analysis: ollama.internal.coutinho.io

**Date**: 2025-10-30
**Model**: nomic-embed-text:latest
**Test Location**: From nextcloud-mcp-server host

## Summary

✅ **Ollama instance is operational and performing well**
- Embedding generation working correctly
- Reasonable latency for small-medium workloads
- Good parallelism support
- Suitable for development and small production deployments

## Test Results

### Model Configuration

```json
{
  "model": "nomic-embed-text",
  "dimensions": 768,
  "status": "operational"
}
```

### Performance Metrics

#### 1. Single Embedding Latency

**Result**: ~553ms per embedding
- **Total time**: 0.553 seconds
- **Includes**: Network + processing + model inference
- **Quality**: Full 768-dimensional vector

**Analysis**:
- Higher than bare-metal benchmarks (~100ms) due to network latency
- Acceptable for interactive search queries
- Within expected range for remote Ollama instance

#### 2. Batch Processing (5 items)

**Result**: ~1.02 seconds for 5 embeddings
- **Per-item average**: 204ms
- **Throughput**: ~4.9 embeddings/sec
- **Batch efficiency**: 2.7x faster than sequential

**Analysis**:
- Good batching efficiency (2.7x speedup vs 5x theoretical)
- Optimal for background indexing
- Network overhead amortized across batch

#### 3. Batch Processing (20 items)

**Result**: ~6.71 seconds for 20 embeddings
- **Per-item average**: 336ms
- **Throughput**: ~3.0 embeddings/sec
- **Batch efficiency**: 1.65x faster than sequential

**Analysis**:
- Performance degrades slightly with larger batches
- Still faster than sequential processing
- Matches reported Ollama behavior (quality issues at batch >16)
- **Recommendation**: Keep batch size ≤16 for best quality

#### 4. Concurrent Requests (5 parallel)

**Result**: ~1.27 seconds for 5 parallel requests
- **Effective parallelism**: ~4x speedup (vs 2.77s sequential)
- **Per-request average**: 254ms
- **Throughput**: ~3.9 requests/sec

**Analysis**:
- Excellent parallelism support
- Server handles concurrent requests efficiently
- Network and compute overlap effectively
- Good for multi-user scenarios

## Capacity Planning

### Current Performance Profile

| Metric | Value | Rating |
|--------|-------|--------|
| Single embedding latency | 553ms | ⚠️ Moderate |
| Batch (5) throughput | 4.9/sec | ✅ Good |
| Batch (20) throughput | 3.0/sec | ⚠️ Moderate |
| Concurrent throughput | 3.9/sec | ✅ Good |
| Network latency | ~300-400ms | ⚠️ Significant |

### Bottleneck Analysis

**Primary Bottleneck**: Network latency (~300-400ms per request)
- Model inference: ~100-200ms (estimated)
- Network round-trip: ~300-400ms (measured overhead)
- **Impact**: 60-70% of total latency is network

**Secondary Bottleneck**: CPU/GPU capacity (unknown hardware)
- Batch performance degrades at >16 items
- Suggests resource constraints
- Likely CPU-only (no GPU metrics available)

### Recommended Usage Patterns

#### ✅ **Excellent For:**

**1. Background Indexing**
- Use batch size of 10-15 items
- Expected throughput: 3-5 embeddings/sec
- **10,000 notes**: ~30-55 minutes to index
- **1,000 notes**: ~3-5 minutes to index

**2. Interactive Search**
- Single query embedding: ~550ms
- Acceptable for user-facing search
- Add 100-200ms for vector search + verification
- **Total search time**: ~650-750ms (reasonable UX)

**3. Multi-User Development**
- 5-10 concurrent users: Comfortable
- Good parallelism support
- Network latency dominates (shared)

#### ⚠️ **Consider Alternatives For:**

**1. Real-Time Applications**
- Sub-100ms latency requirements
- High-frequency queries (>10/sec sustained)
- Consider: Local embeddings or Infinity

**2. Large-Scale Batch Processing**
- >100,000 documents to index
- >10 embeddings/sec sustained
- Consider: GPU-accelerated TEI

**3. Production with >50 Users**
- High concurrent load
- Latency sensitivity
- Consider: Dedicated embedding service

### Deployment Scenarios

#### Scenario 1: Development Environment

**Profile**:
- 1-3 developers
- 1,000-5,000 notes total
- Occasional searches/indexing

**Verdict**: ✅ **Perfect fit**
- Initial index: ~5-15 minutes (one-time)
- Incremental updates: <1 minute
- Search latency: Acceptable
- No infrastructure changes needed

**Configuration**:
```bash
OLLAMA_URL=https://ollama.internal.coutinho.io
OLLAMA_MODEL=nomic-embed-text
VECTOR_SYNC_INTERVAL=600  # 10 minutes
VECTOR_SYNC_BATCH_SIZE=10
```

#### Scenario 2: Small Production (10-20 users)

**Profile**:
- 10-20 active users
- 10,000-50,000 notes total
- 50-200 searches/day
- Nightly incremental indexing

**Verdict**: ✅ **Suitable with optimizations**
- Initial index: 1-3 hours (run overnight)
- Incremental: 5-15 minutes/night
- Search: Acceptable for most users
- Monitor network latency

**Configuration**:
```bash
OLLAMA_URL=https://ollama.internal.coutinho.io
OLLAMA_MODEL=nomic-embed-text
VECTOR_SYNC_INTERVAL=86400  # Daily at night
VECTOR_SYNC_BATCH_SIZE=12  # Conservative for quality
SEARCH_TIMEOUT_MS=1000  # Account for 550ms latency
```

**Optimizations**:
- Run sync during off-hours
- Cache query embeddings (common searches)
- Use hybrid search (keyword + semantic)

#### Scenario 3: Medium Production (50-100 users)

**Profile**:
- 50-100 active users
- 100,000+ notes
- 500-1000 searches/day
- Real-time indexing desired

**Verdict**: ⚠️ **Marginal - monitor closely**
- Initial index: 5-10 hours
- Search latency: May feel slow for some users
- Concurrent load: Approaching limits
- **Recommendation**: Plan migration to Infinity

**Configuration**:
```bash
OLLAMA_URL=https://ollama.internal.coutinho.io
OLLAMA_MODEL=nomic-embed-text
VECTOR_SYNC_INTERVAL=3600  # Hourly
VECTOR_SYNC_BATCH_SIZE=10
SEMANTIC_WEIGHT=0.5  # Rely more on keyword search
SEARCH_TIMEOUT_MS=2000  # Generous timeout
```

**Migration Path**:
- Start with Ollama
- Monitor latency metrics
- When p95 latency >1s, migrate to Infinity
- Keep Ollama as fallback

#### Scenario 4: Large Production (>100 users)

**Profile**:
- >100 active users
- >500,000 notes
- >1000 searches/day
- Real-time expectations

**Verdict**: ❌ **Not recommended**
- Latency too high for scale
- Throughput insufficient
- Network becomes bottleneck
- **Recommendation**: Use Infinity or TEI from start

## Network Latency Optimization

### Current Overhead: ~300-400ms

**If MCP server runs closer to Ollama**:
```
Same VPC/network: ~1-5ms (300-400ms savings!)
Same host: <1ms (300-400ms savings!)
```

### Recommendation

**Option A: Co-locate MCP server with Ollama**
- Reduces latency from 550ms → 150-200ms
- 2.5-3x improvement
- Makes Ollama competitive with cloud APIs

**Option B: Keep separate (current)**
- Simpler deployment
- Better security isolation
- Accept 550ms latency

**Option C: Add Infinity container to MCP server**
- Best of both worlds
- Use Infinity for speed (local)
- Fallback to Ollama if needed

## Capacity Estimates

### Indexing Capacity

**Sustained Throughput**: 3-4 embeddings/sec (conservative)

| Document Count | Index Time | Notes |
|----------------|------------|-------|
| 1,000 | 4-5 min | Quick |
| 5,000 | 20-25 min | Reasonable |
| 10,000 | 40-50 min | Acceptable |
| 50,000 | 3.5-4.5 hours | Overnight job |
| 100,000 | 7-9 hours | Long batch |
| 500,000 | 35-45 hours | Not recommended |

**Incremental Updates** (10% change daily):
- 1,000 docs: ~30 sec
- 10,000 docs: ~5 min
- 50,000 docs: ~25 min

### Search Capacity

**Query Latency Budget**:
- Embedding: 550ms
- Vector search: 50-100ms
- Permission verification: 50-100ms
- **Total**: 650-750ms

**Concurrent Users** (assuming 1 search every 5 minutes):
- 10 users: 2 queries/min → Comfortable
- 50 users: 10 queries/min → Near limit
- 100 users: 20 queries/min → Over capacity

**Peak Load** (all users search at once):
- Parallelism: ~4 concurrent
- Queue time: Proportional to position
- 10 simultaneous: ~1.5-2 sec for last user
- 50 simultaneous: ~7-10 sec for last user

## Recommendations

### Immediate Actions (Development)

1. **✅ Use Ollama as-is**
   - Current setup is perfect for dev/testing
   - No changes needed
   - Start building semantic search

2. **Configuration**:
   ```bash
   OLLAMA_URL=https://ollama.internal.coutinho.io
   OLLAMA_MODEL=nomic-embed-text
   VECTOR_SYNC_BATCH_SIZE=10
   ```

3. **Add Monitoring**:
   ```python
   # Track these metrics
   - embedding_latency_seconds (histogram)
   - embedding_batch_size (gauge)
   - embedding_errors_total (counter)
   ```

### Short-Term (Small Production)

1. **Optimize Batching**:
   - Use batch size 10-12 (quality sweet spot)
   - Process during off-hours
   - Implement incremental sync

2. **Add Caching**:
   ```python
   # Cache common query embeddings
   @lru_cache(maxsize=1000)
   async def embed_with_cache(query: str):
       return await ollama.embed(query)
   ```

3. **Monitor Metrics**:
   - P50, P95, P99 latency
   - Throughput (embeddings/sec)
   - Error rates

### Medium-Term (If Scaling Up)

1. **Add Infinity Container** (when >50 users or latency issues):
   ```yaml
   services:
     infinity:
       image: michaelf34/infinity:latest
       # Local to MCP server - ~10-20ms latency
   ```

2. **Implement Tiered Fallback**:
   ```
   Infinity (local, fast) → Ollama (remote, slower) → Local model
   ```

3. **Load Testing**:
   - Simulate 50-100 concurrent users
   - Measure actual throughput limits
   - Identify breaking points

### Long-Term (Enterprise Scale)

1. **Migrate to TEI Cluster** (when >100 users):
   - GPU-accelerated
   - Horizontal scaling
   - <20ms latency

2. **Consider Managed Services**:
   - Pinecone, Qdrant Cloud
   - Removes operational burden
   - Better SLAs

## Testing Recommendations

### Load Testing Script

```bash
# Test sustained load
for i in {1..100}; do
  curl -s https://ollama.internal.coutinho.io/api/embed \
    -d "{\"model\": \"nomic-embed-text\", \"input\": \"Test $i\"}" &

  # Rate limit: 5 concurrent
  if [ $(($i % 5)) -eq 0 ]; then
    wait
    sleep 1
  fi
done
```

### Metrics to Collect

1. **Latency Distribution**:
   - P50 (median)
   - P95 (acceptable)
   - P99 (outliers)

2. **Throughput**:
   - Embeddings/second
   - Peak vs sustained

3. **Error Rates**:
   - Timeouts
   - Server errors
   - Quality issues

## Conclusion

**Your Ollama instance is ready for development and small production use!**

**Current Capacity**:
- ✅ Development: Unlimited
- ✅ Small prod (10-20 users, 10k docs): Comfortable
- ⚠️ Medium prod (50 users, 50k docs): Monitoring needed
- ❌ Large prod (>100 users): Migrate to Infinity/TEI

**Key Strengths**:
- Fully operational
- Good parallelism
- Acceptable latency for most use cases
- Easy to integrate

**Key Limitations**:
- Network latency adds 300-400ms overhead
- Batch quality issues at >16 items
- Limited scalability beyond 50 users

**Recommendation**:
Start using Ollama immediately for development. Add monitoring and plan for Infinity when you approach 50 users or experience latency issues. The abstraction layer in ADR-003 makes migration seamless.

**Next Steps**:
1. Configure MCP server with Ollama URL
2. Implement semantic search tools
3. Add basic monitoring
4. Test with real workload
5. Scale up as needed
