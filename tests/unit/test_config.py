"""Tests for configuration validation."""

import logging
import os
from dataclasses import MISSING, fields
from unittest.mock import patch

import pytest

from nextcloud_mcp_server.config import (
    _COMPUTED_FIELDS,
    _DEFAULTS,
    _ENV_OVERRIDE,
    _FIELD_MAP,
    Settings,
    _dynaconf,
    _env_key,
    _reload_config,
    _warn_unknown_env_vars,
    get_settings,
)


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

    def test_page_pack_without_page_aware_warns(self, caplog):
        """page-pack without page-aware is a silent no-op; warn at startup."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            document_chunk_page_pack=True,
            document_chunk_page_aware=False,
        )
        assert "DOCUMENT_CHUNK_PAGE_PACK is enabled" in caplog.text

    def test_page_pack_with_page_aware_no_warning(self, caplog):
        """page-pack alongside page-aware is a valid combination; no warning."""

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(
            document_chunk_page_pack=True,
            document_chunk_page_aware=True,
        )
        assert "DOCUMENT_CHUNK_PAGE_PACK is enabled" not in caplog.text


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
            # batch routes through the gateway, so it requires a gateway URL
            # (validated in __post_init__).
            "EMBEDDING_GATEWAY_URL": "https://gw",
        },
        clear=True,
    )
    def test_get_settings_ocr_batch_mode_from_env(self):
        """DOCUMENT_OCR_MODE / batch tuning must reach settings (regression).

        These were added to _DEFAULTS + the Settings dataclass but initially
        omitted from _FIELD_MAP, so dynaconf silently ignored the env vars and
        batch mode could never be enabled in production (Deck #332).
        """
        _reload_config()
        settings = get_settings()
        assert settings.document_ocr_mode == "batch"
        assert settings.document_ocr_batch_poll_seconds == 45

    @patch.dict(
        os.environ,
        {"VECTOR_SYNC_EMPTY_DISCOVERY_DELETE_THRESHOLD": "5"},
        clear=True,
    )
    def test_get_settings_empty_discovery_threshold_from_env(self):
        """VECTOR_SYNC_EMPTY_DISCOVERY_DELETE_THRESHOLD must reach settings.

        Guards against the _DEFAULTS / _FIELD_MAP omission that has silently
        dropped env vars before (cf. OCR batch mode #332): the setting is added
        in all three places (defaults, dataclass, field map).
        """
        _reload_config()
        settings = get_settings()
        assert settings.vector_sync_empty_discovery_delete_threshold == 5

    @patch.dict(os.environ, {"EMBEDDING_DIMENSIONS": "512"}, clear=True)
    def test_get_settings_embedding_dimensions_from_env(self):
        """EMBEDDING_DIMENSIONS must reach settings as an int.

        Same _DEFAULTS / _FIELD_MAP omission guard as the settings above — a key
        declared on the dataclass but missing from the field map is silently
        ignored, which here would mean indexing at full width while the operator
        believes truncation is on.
        """
        _reload_config()
        settings = get_settings()
        assert settings.embedding_dimensions == 512

    @patch.dict(os.environ, {}, clear=True)
    def test_embedding_dimensions_unset_by_default(self):
        """Unset means the model's full width, not zero."""
        _reload_config()
        settings = get_settings()
        assert settings.embedding_dimensions is None
        assert settings.get_embedding_identity() == settings.get_embedding_model_name()

    @patch.dict(os.environ, {}, clear=True)
    def test_empty_discovery_threshold_default(self):
        """Default is 3 consecutive empty cycles before deletions are believed."""
        _reload_config()
        settings = get_settings()
        assert settings.vector_sync_empty_discovery_delete_threshold == 3

    @patch.dict(
        os.environ,
        {
            "PYROSCOPE_ENABLED": "true",
            "PYROSCOPE_SERVER_ADDRESS": "alloy.alloy.svc.cluster.local:4041",
        },
        clear=True,
    )
    def test_get_settings_pyroscope_from_env(self):
        """PYROSCOPE_ENABLED / _SERVER_ADDRESS must reach settings (Deck #655).

        Guards against the _DEFAULTS / _FIELD_MAP omission that has silently
        dropped other observability env vars before (cf. OCR batch mode, #332).
        """
        _reload_config()
        settings = get_settings()
        assert settings.pyroscope_enabled is True
        assert settings.pyroscope_server_address == "alloy.alloy.svc.cluster.local:4041"

    @patch.dict(
        os.environ,
        {"POD_NAMESPACE": "tenant-example", "POD_NAME": "backend-7c95d96fd9-mh2d7"},
        clear=True,
    )
    def test_get_settings_pod_identity_from_env(self):
        """POD_NAMESPACE / POD_NAME must reach settings (Deck #48).

        Same guard as the pyroscope pair above: these are what tag profiles and
        make a tenant's profiles separable, and a missing _DEFAULTS / _FIELD_MAP
        entry would silently drop them with no other test noticing.
        """
        _reload_config()
        settings = get_settings()
        assert settings.pod_namespace == "tenant-example"
        assert settings.pod_name == "backend-7c95d96fd9-mh2d7"

    @patch.dict(
        os.environ,
        {"FORWARDED_ALLOW_IPS": "10.42.0.0/16,192.168.1.5"},
        clear=True,
    )
    def test_get_settings_forwarded_allow_ips_from_env(self):
        """FORWARDED_ALLOW_IPS must reach settings (GH #1284).

        The env var is uvicorn's own, and uvicorn reads it directly — but only
        a _DEFAULTS + _FIELD_MAP entry makes it settable from settings.toml
        too, which is how the helm chart carries non-secret config.
        """
        _reload_config()
        settings = get_settings()
        assert settings.forwarded_allow_ips == "10.42.0.0/16,192.168.1.5"

    @patch.dict(os.environ, {}, clear=True)
    def test_forwarded_allow_ips_unset_by_default(self):
        """None means "don't touch uvicorn's own resolution" (127.0.0.1).

        Defaulting to anything wider would silently make client IPs spoofable,
        and the DCR rate limiter in auth/oauth_routes.py keys on them.
        """
        _reload_config()
        assert get_settings().forwarded_allow_ips is None

    @patch.dict(os.environ, {}, clear=True)
    def test_pyroscope_disabled_by_default(self):
        """Profiling is opt-in: default off with no server address."""
        _reload_config()
        settings = get_settings()
        assert settings.pyroscope_enabled is False
        assert settings.pyroscope_server_address is None

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

    def test_markdown_max_pages_default_and_env_override(self):
        """document_markdown_max_pages defaults to 150 and reads its env var."""
        assert Settings().document_markdown_max_pages == 150
        with patch.dict(os.environ, {"DOCUMENT_MARKDOWN_MAX_PAGES": "40"}, clear=True):
            _reload_config()
            assert get_settings().document_markdown_max_pages == 40

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

    def test_tier_concurrency_defaults_to_none(self):
        """Unset per-tier overrides fall through to VECTOR_SYNC_PROCESSOR_WORKERS."""
        with patch.dict(os.environ, {}, clear=True):
            _reload_config()
            settings = get_settings()
            assert settings.vector_sync_fast_concurrency is None
            assert settings.vector_sync_structured_concurrency is None

    def test_tier_concurrency_valid_value_accepted(self):
        """A positive per-tier override loads normally."""
        with patch.dict(
            os.environ,
            {
                "VECTOR_SYNC_FAST_CONCURRENCY": "2",
                "VECTOR_SYNC_STRUCTURED_CONCURRENCY": "3",
            },
            clear=True,
        ):
            _reload_config()
            settings = get_settings()
            assert settings.vector_sync_fast_concurrency == 2
            assert settings.vector_sync_structured_concurrency == 3

    @patch.dict(
        os.environ,
        {"VECTOR_SYNC_FAST_CONCURRENCY": "0"},
        clear=True,
    )
    def test_zero_fast_concurrency_raises_error(self):
        """0 is rejected at startup rather than reaching the worker (>=1 when set)."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="VECTOR_SYNC_FAST_CONCURRENCY"):
            _reload_config()

    @patch.dict(
        os.environ,
        {"VECTOR_SYNC_STRUCTURED_CONCURRENCY": "-1"},
        clear=True,
    )
    def test_negative_structured_concurrency_raises_error(self):
        """A negative per-tier override raises ValidationError via dynaconf."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="VECTOR_SYNC_STRUCTURED_CONCURRENCY"):
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


class TestEmbeddingIdentityWithMatryoshkaWidth:
    """A requested Matryoshka width is part of the embedding identity.

    Vectors of one model at two widths are different lengths, so a collection
    cannot hold both and a dedup hit across widths would be wrong. Folding the
    width into the identity makes a width change behave like a model change.
    """

    def _settings(self, **overrides):
        return Settings(
            openai_api_key="test-key",
            openai_embedding_model="text-embedding-3-large",
            otel_service_name="my-deployment",
            **overrides,
        )

    def test_identity_is_bare_model_at_full_width(self):
        assert self._settings().get_embedding_identity() == "text-embedding-3-large"

    def test_identity_carries_the_requested_width(self):
        settings = self._settings(embedding_dimensions=512)
        assert settings.get_embedding_identity() == "text-embedding-3-large-512"

    def test_collection_name_separates_widths(self):
        """The dimension-mismatch guard in qdrant_client only fires on a REUSED
        collection, so the two widths must not resolve to the same name."""
        full = self._settings().get_collection_name()
        truncated = self._settings(embedding_dimensions=512).get_collection_name()
        assert full == "my-deployment-text-embedding-3-large"
        assert truncated == "my-deployment-text-embedding-3-large-512"

    def test_model_name_stays_bare_for_metering_and_tracing(self):
        """``get_embedding_model_name`` feeds the usage-metering ``model`` field
        and the embedding span attribute — both record which model ran, which the
        width does not change. Widening it there would rewrite a billing
        dimension's values."""
        settings = self._settings(embedding_dimensions=512)
        assert settings.get_embedding_model_name() == "text-embedding-3-large"


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

    @patch.dict(os.environ, {"OIDC_DISCOVERY_MAX_ATTEMPTS": "0"}, clear=True)
    def test_oidc_discovery_max_attempts_zero_rejected(self):
        """OIDC_DISCOVERY_MAX_ATTEMPTS must be >= 1 (0 disables discovery)."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="OIDC_DISCOVERY_MAX_ATTEMPTS"):
            _reload_config()

    @patch.dict(os.environ, {"OIDC_DISCOVERY_BACKOFF_BASE": "-1"}, clear=True)
    def test_oidc_discovery_backoff_base_negative_rejected(self):
        """OIDC_DISCOVERY_BACKOFF_BASE must be non-negative."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="OIDC_DISCOVERY_BACKOFF_BASE"):
            _reload_config()

    @patch.dict(os.environ, {"OIDC_DISCOVERY_BACKOFF_MAX": "-1"}, clear=True)
    def test_oidc_discovery_backoff_max_negative_rejected(self):
        """OIDC_DISCOVERY_BACKOFF_MAX must be non-negative."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="OIDC_DISCOVERY_BACKOFF_MAX"):
            _reload_config()

    @patch.dict(
        os.environ,
        {
            "OIDC_DISCOVERY_MAX_ATTEMPTS": "3",
            "OIDC_DISCOVERY_BACKOFF_BASE": "0.5",
            "OIDC_DISCOVERY_BACKOFF_MAX": "10",
        },
        clear=True,
    )
    def test_oidc_discovery_retry_settings_valid(self):
        """Valid OIDC discovery retry knobs load and coerce to numbers."""
        _reload_config()
        settings = get_settings()

        assert settings.oidc_discovery_max_attempts == 3
        assert settings.oidc_discovery_backoff_base == pytest.approx(0.5)
        assert settings.oidc_discovery_backoff_max == pytest.approx(10.0)

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

    @patch.dict(os.environ, {"VECTOR_SEARCH_RRF_K": "0"}, clear=True)
    def test_rrf_k_zero_rejected(self):
        """VECTOR_SEARCH_RRF_K=0 fails the gte=1 validator.

        The fused score is 1/(rank + k) with rank 0-indexed, so k=0 divides by
        zero on the top-ranked point. Guards the bound itself against being
        loosened or dropped.
        """
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="VECTOR_SEARCH_RRF_K"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_MAX_PDF_SIZE_MB": "-1"}, clear=True)
    def test_max_pdf_size_negative_rejected(self):
        """DOCUMENT_MAX_PDF_SIZE_MB=-1 fails the gte=0 validator (0 = disabled)."""
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_MAX_PDF_SIZE_MB"):
            _reload_config()

    @patch.dict(os.environ, {"DOCUMENT_MARKDOWN_MAX_PAGES": "-1"}, clear=True)
    def test_markdown_max_pages_negative_rejected(self):
        """DOCUMENT_MARKDOWN_MAX_PAGES=-1 fails the gte=0 validator (0 = disabled).

        Without this, a typo silently disables markdown reconstruction for the
        whole fleet instead of failing fast -- the same silent-disarm class the
        page gate exists to fix.
        """
        from dynaconf import ValidationError

        with pytest.raises(ValidationError, match="DOCUMENT_MARKDOWN_MAX_PAGES"):
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


class TestNextcloudBrowserUrl:
    """Test the ``nextcloud_browser_url`` resolver property (Login Flow v2 rewrite)."""

    def test_prefers_public_url(self):
        """nextcloud_public_url wins — the external-IdP (Keycloak) case."""
        settings = Settings(
            nextcloud_public_url="https://nc.example.com",
            nextcloud_public_issuer_url="https://keycloak.example.com/realms/x",
            nextcloud_host="https://app.internal",
        )
        assert settings.nextcloud_browser_url == "https://nc.example.com"

    def test_falls_back_to_public_issuer(self):
        """Without public_url, the OAuth issuer URL is used (single-IdP case)."""
        settings = Settings(
            nextcloud_public_issuer_url="https://nc.example.com",
            nextcloud_host="https://app.internal",
        )
        assert settings.nextcloud_browser_url == "https://nc.example.com"

    def test_falls_back_to_host(self):
        """With neither public URL set, the internal host is used."""
        settings = Settings(nextcloud_host="https://app.internal")
        assert settings.nextcloud_browser_url == "https://app.internal"

    def test_none_when_nothing_set(self):
        """Returns None when no Nextcloud URL is configured at all."""
        settings = Settings()
        assert settings.nextcloud_browser_url is None


class TestFieldMapDerivation:
    """``_FIELD_MAP`` is derived from ``Settings`` rather than restated.

    These guard the derivation that replaced a 145-entry literal. The failure
    they exist to catch is silent: add a ``Settings`` field whose env var is not
    ``FIELD.upper()`` and forget ``_ENV_OVERRIDE``, and the field simply never
    picks up its env var — no error, just a setting that ignores configuration.
    """

    def test_covers_every_non_computed_field(self):
        """Every Settings field is mapped unless it is explicitly computed."""
        expected = {f.name for f in fields(Settings)} - _COMPUTED_FIELDS
        assert set(_FIELD_MAP) == expected

    def test_computed_fields_are_never_mapped(self):
        """Computed fields must not be fillable from a same-named env var.

        ``_build_settings`` assigns these from the semantic-search /
        background-operations resolution; a mapping entry would let a stray env
        var win over that logic.
        """
        assert _COMPUTED_FIELDS.isdisjoint(_FIELD_MAP)

    def test_overrides_are_the_only_non_identity_mappings(self):
        """Anything not in _ENV_OVERRIDE maps to the upper-cased field name."""
        non_identity = {f: k for f, k in _FIELD_MAP.items() if k != f.upper()}
        assert non_identity == _ENV_OVERRIDE

    def test_every_override_names_a_real_field(self):
        """A renamed or deleted field must not leave a stale override behind."""
        assert set(_ENV_OVERRIDE) <= {f.name for f in fields(Settings)}

    def test_every_computed_field_is_a_real_field(self):
        """Same for the computed-field exclusions."""
        assert _COMPUTED_FIELDS <= {f.name for f in fields(Settings)}

    def test_every_mapped_key_is_declared_to_dynaconf(self):
        """A mapped field must also be a key dynaconf knows about.

        The other half of the same silent failure: we run with
        ``ignore_unknown_envvars=True``, so a key missing from ``_DEFAULTS`` is
        dropped from the environment without a word. Deriving ``_FIELD_MAP``
        from ``Settings`` does not help if the key was never declared.
        """
        undeclared = [k for k in _FIELD_MAP.values() if k not in _dynaconf]
        assert not undeclared, (
            f"Settings fields with no _DEFAULTS entry — their env vars are "
            f"silently ignored: {sorted(undeclared)}"
        )

    def test_defaults_match_dataclass_defaults(self):
        """The declared default must not drift from the dataclass default."""
        mismatched = {
            f.name: (_DEFAULTS[_env_key(f.name).lower()], f.default)
            for f in fields(Settings)
            if f.name in _FIELD_MAP and _DEFAULTS[_env_key(f.name).lower()] != f.default
        }
        assert not mismatched

    def test_every_mapped_field_has_a_plain_default(self):
        """``_DEFAULTS`` can only mirror a plain default, not a factory."""
        assert not [
            f.name
            for f in fields(Settings)
            if f.name in _FIELD_MAP and f.default is MISSING
        ]


class TestUnknownEnvVarWarning:
    """``_warn_unknown_env_vars`` surfaces typo'd settings (Deck #870).

    ``ignore_unknown_envvars=True`` drops anything undeclared, so a misspelled
    ``VECTOR_SYNC_ENABLE`` used to do nothing at all, silently.
    """

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        _warn_unknown_env_vars.cache_clear()
        yield
        _warn_unknown_env_vars.cache_clear()

    def test_warns_with_did_you_mean(self, monkeypatch, caplog):
        monkeypatch.setenv("VECTOR_SYNC_ENABLE", "true")
        with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.config"):
            _warn_unknown_env_vars()
        assert "VECTOR_SYNC_ENABLE is not a recognized setting" in caplog.text
        assert "did you mean VECTOR_SYNC_ENABLED?" in caplog.text

    @pytest.mark.parametrize(
        "name",
        [
            "LS_COLORS",
            # Kubernetes injects a pair like this into every pod; at cutoff 0.80
            # they matched NEXTCLOUD_MCP_SERVICE_NAME / NEXTCLOUD_MCP_PORT.
            "NEXTCLOUD_MCP_SERVICE_HOST",
            "NEXTCLOUD_MCP_PORT_8000_TCP",
            # Read by the OpenTelemetry SDK itself, not declared by us.
            "OTEL_TRACES_SAMPLER",
        ],
    )
    def test_unrelated_env_vars_stay_quiet(self, monkeypatch, caplog, name):
        monkeypatch.setenv(name, "1")
        with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.config"):
            _warn_unknown_env_vars()
        assert name not in caplog.text

    def test_declared_keys_never_warn(self, monkeypatch, caplog):
        monkeypatch.setenv("VECTOR_SYNC_ENABLED", "true")
        with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.config"):
            _warn_unknown_env_vars()
        assert "not a recognized setting" not in caplog.text

    def test_warns_once_per_process(self, monkeypatch, caplog):
        """``@functools.cache`` — the worker must not re-log this per job."""
        monkeypatch.setenv("VECTOR_SYNC_ENABLE", "true")
        with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.config"):
            _warn_unknown_env_vars()
            _warn_unknown_env_vars()
        assert caplog.text.count("did you mean") == 1
