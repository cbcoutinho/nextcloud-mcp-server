"""Unit tests for embedding observability.

Covers:
1. ``Settings.get_embedding_provider_family()`` — the single source of truth for
   the ``provider`` metric label / span attribute — across provider configs.
2. The ``record_embedding`` helper — that it increments the right
   ``astrolabe_embedding_*`` series and skips the throughput counters on error.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.observability.metrics import record_embedding

pytestmark = pytest.mark.unit


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


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
            embedding_gateway_url="http://gateway:8080",
            embedding_gateway_model="mistral/mistral-embed",
        )
        assert settings.get_embedding_provider_family() == "mistral"


class TestRecordEmbedding:
    def test_dense_success_increments_throughput(self):
        labels = {"kind": "dense", "provider": "uttest-prov"}
        before_chunks = _sample("astrolabe_embedding_chunks_total", labels)
        before_chars = _sample("astrolabe_embedding_chars_total", labels)
        before_req = _sample(
            "astrolabe_embedding_requests_total", {**labels, "status": "success"}
        )

        record_embedding("dense", "uttest-prov", 0.42, chunks=12, chars=3400)

        assert _sample("astrolabe_embedding_chunks_total", labels) == (
            before_chunks + 12
        )
        assert _sample("astrolabe_embedding_chars_total", labels) == (
            before_chars + 3400
        )
        assert _sample(
            "astrolabe_embedding_requests_total", {**labels, "status": "success"}
        ) == (before_req + 1)
        assert (
            _sample(
                "astrolabe_embedding_duration_seconds_count",
                {**labels, "status": "success"},
            )
            >= 1
        )

    def test_sparse_error_skips_throughput(self):
        labels = {"kind": "sparse", "provider": "bm25-uttest"}
        record_embedding(
            "sparse", "bm25-uttest", 0.1, chunks=5, chars=100, status="error"
        )
        assert _sample("astrolabe_embedding_chunks_total", labels) == 0.0
        assert _sample("astrolabe_embedding_chars_total", labels) == 0.0
        assert (
            _sample("astrolabe_embedding_requests_total", {**labels, "status": "error"})
            == 1.0
        )
