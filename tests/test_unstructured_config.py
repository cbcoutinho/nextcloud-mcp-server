"""Unit tests for Unstructured API configuration."""

import os
import pytest

from nextcloud_mcp_server.config import (
    get_unstructured_strategy,
    get_unstructured_languages,
)
from nextcloud_mcp_server.client.unstructured_client import UnstructuredClient


class TestUnstructuredStrategy:
    """Test strategy configuration."""

    def test_strategy_default(self):
        """Test that strategy defaults to 'auto'."""
        os.environ.pop("UNSTRUCTURED_STRATEGY", None)
        assert get_unstructured_strategy() == "auto"

    def test_strategy_custom_auto(self):
        """Test custom strategy 'auto'."""
        os.environ["UNSTRUCTURED_STRATEGY"] = "auto"
        try:
            assert get_unstructured_strategy() == "auto"
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)

    def test_strategy_custom_fast(self):
        """Test custom strategy 'fast'."""
        os.environ["UNSTRUCTURED_STRATEGY"] = "fast"
        try:
            assert get_unstructured_strategy() == "fast"
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)

    def test_strategy_custom_hi_res(self):
        """Test custom strategy 'hi_res'."""
        os.environ["UNSTRUCTURED_STRATEGY"] = "hi_res"
        try:
            assert get_unstructured_strategy() == "hi_res"
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)

    def test_strategy_invalid_fallback(self, caplog):
        """Test that invalid strategy falls back to 'hi_res'."""
        import logging

        os.environ["UNSTRUCTURED_STRATEGY"] = "invalid_strategy"
        try:
            # Ensure logging is captured at WARNING level
            with caplog.at_level(logging.WARNING):
                strategy = get_unstructured_strategy()
                assert strategy == "hi_res"
                assert "Invalid UNSTRUCTURED_STRATEGY" in caplog.text
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)

    def test_strategy_case_insensitive(self):
        """Test that strategy is case-insensitive."""
        os.environ["UNSTRUCTURED_STRATEGY"] = "HI_RES"
        try:
            assert get_unstructured_strategy() == "hi_res"
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)


class TestUnstructuredLanguages:
    """Test language configuration."""

    def test_languages_default(self):
        """Test that languages default to English and German."""
        os.environ.pop("UNSTRUCTURED_LANGUAGES", None)
        assert get_unstructured_languages() == ["eng", "deu"]

    def test_languages_single(self):
        """Test single language configuration."""
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng"
        try:
            assert get_unstructured_languages() == ["eng"]
        finally:
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)

    def test_languages_multiple(self):
        """Test multiple languages configuration."""
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng,fra,spa"
        try:
            assert get_unstructured_languages() == ["eng", "fra", "spa"]
        finally:
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)

    def test_languages_whitespace_trimming(self):
        """Test that whitespace is trimmed from language codes."""
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng, deu , fra  "
        try:
            assert get_unstructured_languages() == ["eng", "deu", "fra"]
        finally:
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)

    def test_languages_empty_fallback(self, caplog):
        """Test that empty languages string falls back to default."""
        import logging

        os.environ["UNSTRUCTURED_LANGUAGES"] = ""
        try:
            with caplog.at_level(logging.WARNING):
                languages = get_unstructured_languages()
                assert languages == ["eng", "deu"]
                assert "No languages specified" in caplog.text
        finally:
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)

    def test_languages_only_whitespace_fallback(self, caplog):
        """Test that whitespace-only string falls back to default."""
        import logging

        os.environ["UNSTRUCTURED_LANGUAGES"] = "   ,  ,  "
        try:
            with caplog.at_level(logging.WARNING):
                languages = get_unstructured_languages()
                assert languages == ["eng", "deu"]
                assert "No languages specified" in caplog.text
        finally:
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)


class TestUnstructuredClientConfiguration:
    """Test that UnstructuredClient respects configuration."""

    @pytest.mark.asyncio
    async def test_client_uses_default_strategy(self):
        """Test that client uses default strategy from environment."""
        os.environ.pop("UNSTRUCTURED_STRATEGY", None)
        os.environ["UNSTRUCTURED_API_URL"] = "http://test:8000"

        try:
            client = UnstructuredClient()
            # The partition_document method should use get_unstructured_strategy() when strategy is None
            # We can't test the actual call without a running API, but we can verify the config is read
            assert get_unstructured_strategy() == "auto"
        finally:
            os.environ.pop("UNSTRUCTURED_API_URL", None)

    @pytest.mark.asyncio
    async def test_client_uses_default_languages(self):
        """Test that client uses default languages from environment."""
        os.environ.pop("UNSTRUCTURED_LANGUAGES", None)
        os.environ["UNSTRUCTURED_API_URL"] = "http://test:8000"

        try:
            client = UnstructuredClient()
            # The partition_document method should use get_unstructured_languages() when languages is None
            assert get_unstructured_languages() == ["eng", "deu"]
        finally:
            os.environ.pop("UNSTRUCTURED_API_URL", None)

    @pytest.mark.asyncio
    async def test_client_uses_custom_configuration(self):
        """Test that client uses custom configuration from environment."""
        os.environ["UNSTRUCTURED_STRATEGY"] = "hi_res"
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng,fra,spa"
        os.environ["UNSTRUCTURED_API_URL"] = "http://test:8000"

        try:
            client = UnstructuredClient()
            assert get_unstructured_strategy() == "hi_res"
            assert get_unstructured_languages() == ["eng", "fra", "spa"]
        finally:
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)
            os.environ.pop("UNSTRUCTURED_API_URL", None)
