"""Unit tests for embedding observability.

Covers:
1. ``Settings.get_embedding_provider_family()`` — the single source of truth for
   the ``provider`` metric label / span attribute — across provider configs.
2. The ``record_embedding`` helper — that it increments the right
   ``astrolabe_embedding_*`` series and skips the throughput counters on error.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.observability.metrics import record_embedding

pytestmark = pytest.mark.unit

# ``metric_sample`` is provided as a shared fixture in tests/unit/conftest.py.


class TestProviderFamily:
    """Provider-family detection mirrors ProviderRegistry priority."""

    def test_bedrock(self):
        assert (
            Settings(aws_region="us-east-1").get_embedding_provider_family()
            == "bedrock"
        )

    def test_openai(self):
        settings = Settings(
            openai_api_key="sk-test",
            aws_region=None,
            bedrock_embedding_model=None,
            bedrock_generation_model=None,
        )
        assert settings.get_embedding_provider_family() == "openai"

    def test_mistral(self):
        settings = Settings(
            mistral_api_key="m-test",
            aws_region=None,
            bedrock_embedding_model=None,
            bedrock_generation_model=None,
            openai_api_key=None,
        )
        assert settings.get_embedding_provider_family() == "mistral"

    def test_ollama(self):
        settings = Settings(
            ollama_base_url="http://localhost:11434",
            aws_region=None,
            bedrock_embedding_model=None,
            bedrock_generation_model=None,
            openai_api_key=None,
            mistral_api_key=None,
        )
        assert settings.get_embedding_provider_family() == "ollama"

    def test_simple_fallback(self):
        settings = Settings(
            aws_region=None,
            bedrock_embedding_model=None,
            bedrock_generation_model=None,
            openai_api_key=None,
            mistral_api_key=None,
            ollama_base_url=None,
        )
        assert settings.get_embedding_provider_family() == "simple"

    def test_gateway_uses_model_prefix(self):
        settings = Settings(
            embedding_provider="gateway",
            embedding_gateway_url="https://gateway:8080",
            embedding_gateway_model="mistral/mistral-embed",
        )
        assert settings.get_embedding_provider_family() == "mistral"


class TestRecordEmbedding:
    def test_dense_success_increments_throughput(self, metric_sample):
        labels = {"kind": "dense", "provider": "uttest-prov"}
        before_chunks = metric_sample("astrolabe_embedding_chunks_total", labels)
        before_chars = metric_sample("astrolabe_embedding_chars_total", labels)
        before_req = metric_sample(
            "astrolabe_embedding_requests_total", {**labels, "status": "success"}
        )

        record_embedding("dense", "uttest-prov", 0.42, chunks=12, chars=3400)

        assert metric_sample(
            "astrolabe_embedding_chunks_total", labels
        ) == pytest.approx(before_chunks + 12)
        assert metric_sample(
            "astrolabe_embedding_chars_total", labels
        ) == pytest.approx(before_chars + 3400)
        assert metric_sample(
            "astrolabe_embedding_requests_total", {**labels, "status": "success"}
        ) == pytest.approx(before_req + 1)
        assert (
            metric_sample(
                "astrolabe_embedding_duration_seconds_count",
                {**labels, "status": "success"},
            )
            >= 1
        )

    def test_sparse_error_skips_throughput(self, metric_sample):
        labels = {"kind": "sparse", "provider": "bm25-uttest"}
        record_embedding(
            "sparse", "bm25-uttest", 0.1, chunks=5, chars=100, status="error"
        )
        assert metric_sample(
            "astrolabe_embedding_chunks_total", labels
        ) == pytest.approx(0.0)
        assert metric_sample(
            "astrolabe_embedding_chars_total", labels
        ) == pytest.approx(0.0)
        assert metric_sample(
            "astrolabe_embedding_requests_total", {**labels, "status": "error"}
        ) == pytest.approx(1.0)
