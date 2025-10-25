"""Unit tests for document processor configuration."""

import os

import pytest

pytestmark = pytest.mark.unit


class TestDocumentProcessorConfig:
    """Test document processor configuration system."""

    def test_config_disabled_by_default(self):
        """Test that document processing is disabled by default."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ.pop("ENABLE_DOCUMENT_PROCESSING", None)
        config = get_document_processor_config()
        assert config["enabled"] is False

    def test_config_enabled(self):
        """Test enabling document processing."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ["ENABLE_DOCUMENT_PROCESSING"] = "true"
        try:
            config = get_document_processor_config()
            assert config["enabled"] is True
        finally:
            os.environ.pop("ENABLE_DOCUMENT_PROCESSING", None)

    def test_unstructured_processor_config(self):
        """Test Unstructured processor configuration."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ["ENABLE_UNSTRUCTURED"] = "true"
        os.environ["UNSTRUCTURED_API_URL"] = "http://test:8000"
        os.environ["UNSTRUCTURED_STRATEGY"] = "hi_res"
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng,fra"
        os.environ["UNSTRUCTURED_TIMEOUT"] = "60"

        try:
            config = get_document_processor_config()
            assert "unstructured" in config["processors"]
            unst_config = config["processors"]["unstructured"]
            assert unst_config["api_url"] == "http://test:8000"
            assert unst_config["strategy"] == "hi_res"
            assert unst_config["languages"] == ["eng", "fra"]
            assert unst_config["timeout"] == 60
        finally:
            os.environ.pop("ENABLE_UNSTRUCTURED", None)
            os.environ.pop("UNSTRUCTURED_API_URL", None)
            os.environ.pop("UNSTRUCTURED_STRATEGY", None)
            os.environ.pop("UNSTRUCTURED_LANGUAGES", None)
            os.environ.pop("UNSTRUCTURED_TIMEOUT", None)

    def test_tesseract_processor_config(self):
        """Test Tesseract processor configuration."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ["ENABLE_TESSERACT"] = "true"
        os.environ["TESSERACT_LANG"] = "eng+deu"
        os.environ["TESSERACT_CMD"] = "/usr/local/bin/tesseract"

        try:
            config = get_document_processor_config()
            assert "tesseract" in config["processors"]
            tess_config = config["processors"]["tesseract"]
            assert tess_config["lang"] == "eng+deu"
            assert tess_config["tesseract_cmd"] == "/usr/local/bin/tesseract"
        finally:
            os.environ.pop("ENABLE_TESSERACT", None)
            os.environ.pop("TESSERACT_LANG", None)
            os.environ.pop("TESSERACT_CMD", None)

    def test_custom_processor_config(self):
        """Test custom processor configuration."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ["ENABLE_CUSTOM_PROCESSOR"] = "true"
        os.environ["CUSTOM_PROCESSOR_NAME"] = "my_ocr"
        os.environ["CUSTOM_PROCESSOR_URL"] = "http://localhost:9000/process"
        os.environ["CUSTOM_PROCESSOR_API_KEY"] = "secret"
        os.environ["CUSTOM_PROCESSOR_TIMEOUT"] = "30"
        os.environ["CUSTOM_PROCESSOR_TYPES"] = "application/pdf,image/jpeg"

        try:
            config = get_document_processor_config()
            assert "custom" in config["processors"]
            custom_config = config["processors"]["custom"]
            assert custom_config["name"] == "my_ocr"
            assert custom_config["api_url"] == "http://localhost:9000/process"
            assert custom_config["api_key"] == "secret"
            assert custom_config["timeout"] == 30
            assert "application/pdf" in custom_config["supported_types"]
            assert "image/jpeg" in custom_config["supported_types"]
        finally:
            os.environ.pop("ENABLE_CUSTOM_PROCESSOR", None)
            os.environ.pop("CUSTOM_PROCESSOR_NAME", None)
            os.environ.pop("CUSTOM_PROCESSOR_URL", None)
            os.environ.pop("CUSTOM_PROCESSOR_API_KEY", None)
            os.environ.pop("CUSTOM_PROCESSOR_TIMEOUT", None)
            os.environ.pop("CUSTOM_PROCESSOR_TYPES", None)

    def test_multiple_processors(self):
        """Test configuration with multiple processors enabled."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ["ENABLE_DOCUMENT_PROCESSING"] = "true"
        os.environ["ENABLE_UNSTRUCTURED"] = "true"
        os.environ["ENABLE_TESSERACT"] = "true"

        try:
            config = get_document_processor_config()
            assert config["enabled"] is True
            assert "unstructured" in config["processors"]
            assert "tesseract" in config["processors"]
        finally:
            os.environ.pop("ENABLE_DOCUMENT_PROCESSING", None)
            os.environ.pop("ENABLE_UNSTRUCTURED", None)
            os.environ.pop("ENABLE_TESSERACT", None)

    def test_default_processor_selection(self):
        """Test default processor configuration."""
        from nextcloud_mcp_server.config import get_document_processor_config

        os.environ.pop("DOCUMENT_PROCESSOR", None)
        config = get_document_processor_config()
        assert config["default_processor"] == "unstructured"

        os.environ["DOCUMENT_PROCESSOR"] = "tesseract"
        try:
            config = get_document_processor_config()
            assert config["default_processor"] == "tesseract"
        finally:
            os.environ.pop("DOCUMENT_PROCESSOR", None)
