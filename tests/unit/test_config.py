"""Tests for configuration validation."""

import logging
import os
from unittest.mock import patch

import pytest

from nextcloud_mcp_server.config import Settings, get_settings


class TestQdrantConfigValidation:
    """Test Qdrant configuration validation."""

    def test_mutually_exclusive_url_and_location(self):
        """Test that setting both QDRANT_URL and QDRANT_LOCATION raises ValueError."""
        with pytest.raises(
            ValueError,
            match="Cannot set both QDRANT_URL and QDRANT_LOCATION",
        ):
            Settings(
                qdrant_url="http://qdrant:6333",
                qdrant_location="/app/data/qdrant",
            )

    def test_default_to_memory_mode(self):
        """Test that :memory: is used when neither URL nor location is set."""
        settings = Settings()
        assert settings.qdrant_location == ":memory:"
        assert settings.qdrant_url is None

    def test_network_mode_only(self):
        """Test network mode with only URL set."""
        settings = Settings(qdrant_url="http://qdrant:6333")
        assert settings.qdrant_url == "http://qdrant:6333"
        assert settings.qdrant_location is None

    def test_local_mode_only(self):
        """Test local mode with only location set."""
        settings = Settings(qdrant_location="/app/data/qdrant")
        assert settings.qdrant_location == "/app/data/qdrant"
        assert settings.qdrant_url is None

    def test_in_memory_mode_explicit(self):
        """Test explicit in-memory mode."""
        settings = Settings(qdrant_location=":memory:")
        assert settings.qdrant_location == ":memory:"
        assert settings.qdrant_url is None

    def test_api_key_warning_in_local_mode(self, caplog):
        """Test that API key in local mode triggers warning."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            qdrant_location=":memory:",
            qdrant_api_key="test-api-key",
        )
        assert "API key is only relevant for network mode" in caplog.text

    def test_api_key_no_warning_in_network_mode(self, caplog):
        """Test that API key in network mode doesn't trigger warning."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="test-api-key",
        )
        assert "API key is only relevant for network mode" not in caplog.text


class TestGetSettings:
    """Test get_settings() function with environment variables."""

    @patch.dict(os.environ, {}, clear=True)
    def test_get_settings_defaults_to_memory(self):
        """Test get_settings() defaults to :memory: when no env vars set."""
        settings = get_settings()
        assert settings.qdrant_location == ":memory:"
        assert settings.qdrant_url is None

    @patch.dict(
        os.environ,
        {
            "QDRANT_URL": "http://qdrant:6333",
            "QDRANT_API_KEY": "test-key",
        },
        clear=True,
    )
    def test_get_settings_network_mode(self):
        """Test get_settings() with network mode env vars."""
        settings = get_settings()
        assert settings.qdrant_url == "http://qdrant:6333"
        assert settings.qdrant_api_key == "test-key"
        assert settings.qdrant_location is None

    @patch.dict(
        os.environ,
        {"QDRANT_LOCATION": "/app/data/qdrant"},
        clear=True,
    )
    def test_get_settings_persistent_mode(self):
        """Test get_settings() with persistent local mode env vars."""
        settings = get_settings()
        assert settings.qdrant_location == "/app/data/qdrant"
        assert settings.qdrant_url is None

    @patch.dict(
        os.environ,
        {"QDRANT_LOCATION": ":memory:"},
        clear=True,
    )
    def test_get_settings_explicit_memory(self):
        """Test get_settings() with explicit :memory: env var."""
        settings = get_settings()
        assert settings.qdrant_location == ":memory:"
        assert settings.qdrant_url is None

    @patch.dict(
        os.environ,
        {
            "QDRANT_URL": "http://qdrant:6333",
            "QDRANT_LOCATION": "/app/data/qdrant",
        },
        clear=True,
    )
    def test_get_settings_mutual_exclusion_error(self):
        """Test get_settings() raises error when both URL and location set."""
        with pytest.raises(
            ValueError,
            match="Cannot set both QDRANT_URL and QDRANT_LOCATION",
        ):
            get_settings()

    @patch.dict(
        os.environ,
        {
            "QDRANT_COLLECTION": "test_collection",
            "VECTOR_SYNC_ENABLED": "true",
            "VECTOR_SYNC_SCAN_INTERVAL": "600",
            "VECTOR_SYNC_PROCESSOR_WORKERS": "5",
            "VECTOR_SYNC_QUEUE_MAX_SIZE": "5000",
        },
        clear=True,
    )
    def test_get_settings_vector_sync_config(self):
        """Test get_settings() with vector sync configuration."""
        settings = get_settings()
        assert settings.qdrant_collection == "test_collection"
        assert settings.vector_sync_enabled is True
        assert settings.vector_sync_scan_interval == 600
        assert settings.vector_sync_processor_workers == 5
        assert settings.vector_sync_queue_max_size == 5000


class TestChunkConfigValidation:
    """Test document chunking configuration validation."""

    def test_default_chunk_settings(self):
        """Test default chunk size and overlap values."""
        settings = Settings()
        assert settings.document_chunk_size == 2048
        assert settings.document_chunk_overlap == 200

    def test_valid_chunk_settings(self):
        """Test valid chunk size and overlap configuration."""
        settings = Settings(
            document_chunk_size=1024,
            document_chunk_overlap=100,
        )
        assert settings.document_chunk_size == 1024
        assert settings.document_chunk_overlap == 100

    def test_overlap_greater_than_or_equal_to_chunk_size_raises_error(self):
        """Test that overlap >= chunk size raises ValueError."""
        with pytest.raises(
            ValueError,
            match="DOCUMENT_CHUNK_OVERLAP .* must be less than DOCUMENT_CHUNK_SIZE",
        ):
            Settings(
                document_chunk_size=512,
                document_chunk_overlap=512,
            )

    def test_overlap_larger_than_chunk_size_raises_error(self):
        """Test that overlap > chunk size raises ValueError."""
        with pytest.raises(
            ValueError,
            match="DOCUMENT_CHUNK_OVERLAP .* must be less than DOCUMENT_CHUNK_SIZE",
        ):
            Settings(
                document_chunk_size=256,
                document_chunk_overlap=300,
            )

    def test_negative_overlap_raises_error(self):
        """Test that negative overlap raises ValueError."""
        with pytest.raises(
            ValueError,
            match="DOCUMENT_CHUNK_OVERLAP .* cannot be negative",
        ):
            Settings(
                document_chunk_size=512,
                document_chunk_overlap=-10,
            )

    def test_small_chunk_size_warning(self, caplog):
        """Test that chunk size < 512 triggers warning."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            document_chunk_size=64,
            document_chunk_overlap=10,
        )
        assert (
            "DOCUMENT_CHUNK_SIZE is set to 64 characters, which is quite small"
            in caplog.text
        )
        assert "Consider using at least 1024 characters" in caplog.text

    def test_reasonable_chunk_size_no_warning(self, caplog):
        """Test that chunk size >= 512 doesn't trigger warning."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            document_chunk_size=1024,
            document_chunk_overlap=100,
        )
        assert "DOCUMENT_CHUNK_SIZE" not in caplog.text

    @patch.dict(
        os.environ,
        {
            "DOCUMENT_CHUNK_SIZE": "1024",
            "DOCUMENT_CHUNK_OVERLAP": "102",
        },
        clear=True,
    )
    def test_get_settings_chunk_config(self):
        """Test get_settings() with chunk configuration."""
        settings = get_settings()
        assert settings.document_chunk_size == 1024
        assert settings.document_chunk_overlap == 102

    @patch.dict(
        os.environ,
        {
            "DOCUMENT_CHUNK_SIZE": "256",
            "DOCUMENT_CHUNK_OVERLAP": "256",
        },
        clear=True,
    )
    def test_get_settings_invalid_chunk_config_raises_error(self):
        """Test get_settings() raises error for invalid chunk config."""
        with pytest.raises(
            ValueError,
            match="DOCUMENT_CHUNK_OVERLAP .* must be less than DOCUMENT_CHUNK_SIZE",
        ):
            get_settings()


class TestEmbeddingModelName:
    """Test get_embedding_model_name() method."""

    def test_openai_takes_priority(self):
        """Test that OpenAI model is returned when OPENAI_API_KEY is set."""
        settings = Settings(
            openai_api_key="test-key",
            openai_embedding_model="text-embedding-3-large",
            ollama_base_url="http://ollama:11434",
            ollama_embedding_model="nomic-embed-text",
        )
        assert settings.get_embedding_model_name() == "text-embedding-3-large"

    def test_ollama_used_when_no_openai(self):
        """Test that Ollama model is returned when no OpenAI configured."""
        settings = Settings(
            ollama_base_url="http://ollama:11434",
            ollama_embedding_model="all-minilm",
        )
        assert settings.get_embedding_model_name() == "all-minilm"

    def test_simple_fallback(self):
        """Test fallback to simple provider when nothing configured."""
        settings = Settings()
        assert settings.get_embedding_model_name() == "simple-384"

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test-openai-key",
            "OPENAI_EMBEDDING_MODEL": "openai/text-embedding-3-small",
        },
        clear=True,
    )
    def test_get_settings_openai_model(self):
        """Test get_settings() loads OpenAI embedding model."""
        settings = get_settings()
        assert settings.openai_api_key == "test-openai-key"
        assert settings.openai_embedding_model == "openai/text-embedding-3-small"
        assert settings.get_embedding_model_name() == "openai/text-embedding-3-small"


class TestCollectionNameWithProviders:
    """Test get_collection_name() with different providers."""

    def test_collection_name_with_openai(self):
        """Test collection name uses OpenAI model when configured."""
        settings = Settings(
            openai_api_key="test-key",
            openai_embedding_model="text-embedding-3-small",
            otel_service_name="my-deployment",
        )
        assert settings.get_collection_name() == "my-deployment-text-embedding-3-small"

    def test_collection_name_with_github_models(self):
        """Test collection name sanitizes GitHub Models prefix."""
        settings = Settings(
            openai_api_key="ghp_test",
            openai_embedding_model="openai/text-embedding-3-small",
            otel_service_name="my-deployment",
        )
        # Slashes should be replaced with dashes
        assert (
            settings.get_collection_name()
            == "my-deployment-openai-text-embedding-3-small"
        )

    def test_collection_name_with_ollama(self):
        """Test collection name uses Ollama model when no OpenAI."""
        settings = Settings(
            ollama_base_url="http://ollama:11434",
            ollama_embedding_model="nomic-embed-text",
            otel_service_name="my-deployment",
        )
        assert settings.get_collection_name() == "my-deployment-nomic-embed-text"

    def test_collection_name_explicit_override(self):
        """Test explicit QDRANT_COLLECTION overrides auto-generation."""
        settings = Settings(
            qdrant_collection="custom-collection",
            openai_api_key="test-key",
            openai_embedding_model="text-embedding-3-large",
        )
        assert settings.get_collection_name() == "custom-collection"
