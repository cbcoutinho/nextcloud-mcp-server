"""Tests for configuration validation."""

import logging
import os
from unittest.mock import patch

import pytest

from nextcloud_mcp_server.config import Settings, _reload_config, get_settings


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
        _reload_config()
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
        _reload_config()
        settings = get_settings()
        assert settings.qdrant_url == "http://qdrant:6333"
        assert settings.qdrant_api_key == "test-key"
        assert settings.qdrant_location is None

    @patch.dict(
        os.environ,
        {
            "NEXTCLOUD_OIDC_TOKEN_TYPE": "jwt",
            "NEXTCLOUD_OIDC_SCOPES": "openid profile",
        },
        clear=True,
    )
    def test_get_settings_oidc_token_type_and_scopes_from_env(self):
        """NEXTCLOUD_OIDC_TOKEN_TYPE / _SCOPES must reach settings (regression).

        The settings migration first registered these under _DEFAULTS keys that
        uppercased to OIDC_* instead of NEXTCLOUD_OIDC_*, so dynaconf silently
        ignored the env vars and always returned the defaults.
        """
        _reload_config()
        settings = get_settings()
        assert settings.oidc_token_type == "jwt"
        assert settings.oidc_scopes == "openid profile"

    @patch.dict(
        os.environ,
        {
            "DOCUMENT_OCR_MODE": "batch",
            "DOCUMENT_OCR_BATCH_POLL_SECONDS": "45",
            "DOCUMENT_OCR_BATCH_MAX_WAIT_SECONDS": "3600",
            # batch routes through the gateway, so it requires a gateway URL
            # (validated in __post_init__).
            "EMBEDDING_GATEWAY_URL": "https://gw",
        },
        clear=True,
    )
    def test_get_settings_ocr_batch_mode_from_env(self):
        """DOCUMENT_OCR_MODE / batch tuning must reach settings (regression).

        These were added to _DEFAULTS + the Settings dataclass but initially
        omitted from _field_map, so dynaconf silently ignored the env vars and
        batch mode could never be enabled in production (Deck #332).
        """
        _reload_config()
        settings = get_settings()
        assert settings.document_ocr_mode == "batch"
        assert settings.document_ocr_batch_poll_seconds == 45
        assert settings.document_ocr_batch_max_wait_seconds == 3600

    @patch.dict(
        os.environ,
        {"DOCUMENT_OCR_MODE": "Batch", "EMBEDDING_GATEWAY_URL": "https://gw"},
        clear=True,
    )
    def test_document_ocr_mode_case_normalised(self):
        """DOCUMENT_OCR_MODE is case-insensitive (normalised in __post_init__ via
        _enum_fields, like DOCUMENT_OCR_PROVIDER) — "Batch" -> "batch"."""
        _reload_config()
        assert get_settings().document_ocr_mode == "batch"

    @patch.dict(os.environ, {"DOCUMENT_OCR_MODE": "bogus"}, clear=True)
    def test_document_ocr_mode_invalid_rejected(self):
        _reload_config()
        with pytest.raises(ValueError, match="DOCUMENT_OCR_MODE"):
            get_settings()

    @patch.dict(os.environ, {"DOCUMENT_OCR_MODE": "batch"}, clear=True)
    def test_document_ocr_mode_batch_requires_gateway(self):
        """batch OCR routes through the embedding gateway, so mode=batch without
        EMBEDDING_GATEWAY_URL is rejected at startup (no silent sync downgrade)."""
        _reload_config()
        with pytest.raises(ValueError, match="DOCUMENT_OCR_MODE=batch requires"):
            get_settings()

    @patch.dict(
        os.environ,
        {"QDRANT_LOCATION": "/app/data/qdrant"},
        clear=True,
    )
    def test_get_settings_persistent_mode(self):
        """Test get_settings() with persistent local mode env vars."""
        _reload_config()
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
        _reload_config()
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
        _reload_config()
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
        _reload_config()
        settings = get_settings()
        assert settings.qdrant_collection == "test_collection"
        assert settings.vector_sync_enabled is True
        assert settings.vector_sync_scan_interval == 600
        assert settings.vector_sync_processor_workers == 5
        assert settings.vector_sync_queue_max_size == 5000

    @patch.dict(os.environ, {}, clear=True)
    def test_usage_metering_disabled_by_default(self):
        """USAGE_METERING_ENABLED defaults to False (OSS doesn't self-monitor)."""
        _reload_config()
        assert get_settings().usage_metering_enabled is False

    @patch.dict(os.environ, {"USAGE_METERING_ENABLED": "true"}, clear=True)
    def test_usage_metering_enabled_via_env(self):
        """USAGE_METERING_ENABLED=true maps to settings.usage_metering_enabled."""
        _reload_config()
        assert get_settings().usage_metering_enabled is True


class TestChunkConfigValidation:
    """Test document chunking configuration validation."""

    def test_default_chunk_settings(self):
        """Test default chunk size and overlap values."""
        settings = Settings()
        assert settings.document_chunk_size == 2048
        assert settings.document_chunk_overlap == 200

    def test_page_aware_enabled_by_default(self):
        """Page-aware chunking is on by default."""
        assert Settings().document_chunk_page_aware is True

    @patch.dict(
        os.environ,
        {"DOCUMENT_CHUNK_PAGE_AWARE": "false"},
        clear=True,
    )
    def test_page_aware_disabled_via_env(self):
        """DOCUMENT_CHUNK_PAGE_AWARE=false disables page-aware chunking."""
        _reload_config()
        assert get_settings().document_chunk_page_aware is False

    def test_ocr_timeout_default_and_env_override(self):
        """document_ocr_timeout_seconds defaults to 180 and reads its env var.

        Guards the _DEFAULTS-key-must-match-env-var footgun: a mismatch would
        leave the override silently ignored.
        """
        assert Settings().document_ocr_timeout_seconds == pytest.approx(180.0)
        with patch.dict(os.environ, {"DOCUMENT_OCR_TIMEOUT_SECONDS": "45"}, clear=True):
            _reload_config()
            assert get_settings().document_ocr_timeout_seconds == pytest.approx(45.0)

    def test_max_pdf_size_default_and_env_override(self):
        """document_max_pdf_size_mb defaults to 50 and reads its env var."""
        assert Settings().document_max_pdf_size_mb == pytest.approx(50.0)
        with patch.dict(os.environ, {"DOCUMENT_MAX_PDF_SIZE_MB": "12.5"}, clear=True):
            _reload_config()
            assert get_settings().document_max_pdf_size_mb == pytest.approx(12.5)

    def test_glyph_corruption_ratio_default_and_env_override(self):
        """document_glyph_corruption_ratio defaults to 0.02 and reads its env var.

        Guards the _DEFAULTS-key-must-match-env-var footgun.
        """
        assert Settings().document_glyph_corruption_ratio == pytest.approx(0.02)
        with patch.dict(
            os.environ, {"DOCUMENT_GLYPH_CORRUPTION_RATIO": "0.05"}, clear=True
        ):
            _reload_config()
            assert get_settings().document_glyph_corruption_ratio == pytest.approx(0.05)

    @patch.dict(
        os.environ,
        {"DOCUMENT_GLYPH_CORRUPTION_RATIO": "1.5"},
        clear=True,
    )
    def test_glyph_corruption_ratio_out_of_range_raises_error(self):
        """The ratio must be within [0, 1]."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_GLYPH_CORRUPTION_RATIO"):
            _reload_config()

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

    @patch.dict(
        os.environ,
        {"DOCUMENT_CHUNK_OVERLAP": "-10"},
        clear=True,
    )
    def test_negative_overlap_raises_error(self):
        """Test that negative overlap raises ValidationError via dynaconf."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_CHUNK_OVERLAP"):
            _reload_config()

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
        _reload_config()
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
        _reload_config()
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
        _reload_config()
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


class TestDynaconfValidators:
    """Test dynaconf declarative validators (ADR-024 Phase 3)."""

    @patch.dict(os.environ, {"METRICS_PORT": "0"}, clear=True)
    def test_metrics_port_too_low(self):
        """Test METRICS_PORT below minimum raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="METRICS_PORT"):
            _reload_config()

    @patch.dict(os.environ, {"METRICS_PORT": "99999"}, clear=True)
    def test_metrics_port_too_high(self):
        """Test METRICS_PORT above maximum raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="METRICS_PORT"):
            _reload_config()

    @patch.dict(os.environ, {"LOG_FORMAT": "xml"}, clear=True)
    def test_invalid_log_format(self):
        """Test invalid LOG_FORMAT raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="LOG_FORMAT"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_OCR_MIN_TEXT_QUALITY": "1.5"}, clear=True)
    def test_ocr_min_text_quality_out_of_range(self):
        """DOCUMENT_OCR_MIN_TEXT_QUALITY must be in [0, 1]."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_OCR_MIN_TEXT_QUALITY"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_OCR_PAGE_FRACTION": "2"}, clear=True)
    def test_ocr_page_fraction_out_of_range(self):
        """DOCUMENT_OCR_PAGE_FRACTION must be in [0, 1]."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_OCR_PAGE_FRACTION"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_OCR_MIN_PAGE_CHARS": "-1"}, clear=True)
    def test_ocr_min_page_chars_negative(self):
        """DOCUMENT_OCR_MIN_PAGE_CHARS must be non-negative."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_OCR_MIN_PAGE_CHARS"):
            _reload_config()

    @patch.dict(os.environ, {"LOG_LEVEL": "VERBOSE"}, clear=True)
    def test_invalid_log_level(self):
        """Test invalid LOG_LEVEL raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="LOG_LEVEL"):
            _reload_config()

    @patch.dict(os.environ, {"OTEL_TRACES_SAMPLER": "random"}, clear=True)
    def test_invalid_otel_sampler(self):
        """Test invalid OTEL_TRACES_SAMPLER raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="OTEL_TRACES_SAMPLER"):
            _reload_config()

    @patch.dict(os.environ, {"WEBHOOK_SECRET": "short"}, clear=True)
    def test_webhook_secret_too_short(self):
        """A set WEBHOOK_SECRET shorter than 16 chars raises ValidationError
        (GHSA-8vh3-g2qg-2h2c hardening — reject weak/placeholder secrets at
        startup)."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
            _reload_config()

    @patch.dict(
        os.environ, {"WEBHOOK_SECRET": "a-sufficiently-long-secret"}, clear=True
    )
    def test_webhook_secret_long_enough_is_accepted(self):
        """A WEBHOOK_SECRET of >=16 chars passes validation."""
        _reload_config()
        assert get_settings().webhook_secret == "a-sufficiently-long-secret"

    @patch.dict(os.environ, {"OTEL_TRACES_SAMPLER_ARG": "2.0"}, clear=True)
    def test_sampler_arg_too_high(self):
        """Test OTEL_TRACES_SAMPLER_ARG above 1.0 raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="OTEL_TRACES_SAMPLER_ARG"):
            _reload_config()

    @patch.dict(os.environ, {"VECTOR_SYNC_SCAN_INTERVAL": "0"}, clear=True)
    def test_vector_sync_interval_zero(self):
        """Test zero VECTOR_SYNC_SCAN_INTERVAL raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="VECTOR_SYNC_SCAN_INTERVAL"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_CHUNK_SIZE": "0"}, clear=True)
    def test_chunk_size_zero(self):
        """Test zero DOCUMENT_CHUNK_SIZE raises ValidationError."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_CHUNK_SIZE"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_OCR_TIMEOUT_SECONDS": "0"}, clear=True)
    def test_ocr_timeout_zero_rejected(self):
        """DOCUMENT_OCR_TIMEOUT_SECONDS=0 fails the gte=1 validator."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_OCR_TIMEOUT_SECONDS"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_MAX_PDF_SIZE_MB": "-1"}, clear=True)
    def test_max_pdf_size_negative_rejected(self):
        """DOCUMENT_MAX_PDF_SIZE_MB=-1 fails the gte=0 validator (0 = disabled)."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_MAX_PDF_SIZE_MB"):
            _reload_config()

    @patch.dict(os.environ, {"METRICS_PORT": "8080"}, clear=True)
    def test_valid_metrics_port(self):
        """Test valid METRICS_PORT passes validation."""
        _reload_config()
        settings = get_settings()
        assert settings.metrics_port == 8080

    @patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=True)
    def test_valid_log_format_json(self):
        """Test valid LOG_FORMAT=json passes validation."""
        _reload_config()
        settings = get_settings()
        assert settings.log_format == "json"
