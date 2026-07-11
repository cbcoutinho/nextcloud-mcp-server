"""Unit tests for document processor configuration."""

import os

import pytest

from nextcloud_mcp_server.config import _reload_config, get_document_processor_config

pytestmark = pytest.mark.unit


class TestDocumentProcessorConfig:
    """Test document processor configuration system."""

    def test_config_disabled_by_default(self):
        """Test that document processing is disabled by default."""
        os.environ.pop("ENABLE_DOCUMENT_PROCESSING", None)
        _reload_config()
        config = get_document_processor_config()
        assert config["enabled"] is False

    def test_config_enabled(self):
        """Test enabling document processing."""
        os.environ["ENABLE_DOCUMENT_PROCESSING"] = "true"
        try:
            _reload_config()
            config = get_document_processor_config()
            assert config["enabled"] is True
        finally:
            os.environ.pop("ENABLE_DOCUMENT_PROCESSING", None)

    def test_unstructured_processor_config(self):
        """Test Unstructured processor configuration."""
        os.environ["ENABLE_UNSTRUCTURED"] = "true"
        os.environ["UNSTRUCTURED_API_URL"] = "http://test:8000"
        os.environ["UNSTRUCTURED_STRATEGY"] = "hi_res"
        os.environ["UNSTRUCTURED_LANGUAGES"] = "eng,fra"
        os.environ["UNSTRUCTURED_TIMEOUT"] = "60"

        try:
            _reload_config()
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
        os.environ["ENABLE_TESSERACT"] = "true"
        os.environ["TESSERACT_LANG"] = "eng+deu"
        os.environ["TESSERACT_CMD"] = "/usr/local/bin/tesseract"

        try:
            _reload_config()
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
        os.environ["ENABLE_CUSTOM_PROCESSOR"] = "true"
        os.environ["CUSTOM_PROCESSOR_NAME"] = "my_ocr"
        os.environ["CUSTOM_PROCESSOR_URL"] = "http://localhost:9000/process"
        os.environ["CUSTOM_PROCESSOR_API_KEY"] = "secret"
        os.environ["CUSTOM_PROCESSOR_TIMEOUT"] = "30"
        os.environ["CUSTOM_PROCESSOR_TYPES"] = "application/pdf,image/jpeg"

        try:
            _reload_config()
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

    def test_docling_processor_config(self):
        """Docling registers only when a URL is set; values are parsed correctly."""
        os.environ["ENABLE_DOCLING"] = "true"
        os.environ["DOCLING_API_URL"] = "https://docling:5001"
        os.environ["DOCLING_OCR_LANG"] = "en,de"
        os.environ["DOCLING_TIMEOUT"] = "90"
        os.environ["DOCLING_DO_OCR"] = "true"

        try:
            _reload_config()
            config = get_document_processor_config()
            assert "docling" in config["processors"]
            dcfg = config["processors"]["docling"]
            assert dcfg["api_url"] == "https://docling:5001"
            assert dcfg["ocr_lang"] == ["en", "de"]
            assert dcfg["timeout"] == 90
            assert dcfg["do_ocr"] is True
            # VLM opt-in defaults: standard pipeline, no preset.
            assert dcfg["pipeline"] == "standard"
            assert dcfg["vlm_preset"] is None
        finally:
            for key in (
                "ENABLE_DOCLING",
                "DOCLING_API_URL",
                "DOCLING_OCR_LANG",
                "DOCLING_TIMEOUT",
                "DOCLING_DO_OCR",
            ):
                os.environ.pop(key, None)

    def test_docling_vlm_pipeline_config(self):
        """DOCLING_PIPELINE/DOCLING_VLM_PRESET flow into the docling processor
        config so the image path can drive the VLM pipeline."""
        os.environ["ENABLE_DOCLING"] = "true"
        os.environ["DOCLING_API_URL"] = "https://docling:5001"
        # Uppercase to prove the image path normalizes like Settings does; otherwise
        # convert_file's `pipeline == "vlm"` check would silently use standard.
        os.environ["DOCLING_PIPELINE"] = "VLM"
        os.environ["DOCLING_VLM_PRESET"] = "glm_ocr"

        try:
            _reload_config()
            config = get_document_processor_config()
            dcfg = config["processors"]["docling"]
            assert dcfg["pipeline"] == "vlm"
            # Preset stays verbatim -- server-defined and case-sensitive (D6).
            assert dcfg["vlm_preset"] == "glm_ocr"
        finally:
            for key in (
                "ENABLE_DOCLING",
                "DOCLING_API_URL",
                "DOCLING_PIPELINE",
                "DOCLING_VLM_PRESET",
            ):
                os.environ.pop(key, None)

    def test_docling_absent_without_url(self):
        """A bare ENABLE_DOCLING (no URL) must NOT register the processor, so it
        can't shadow other image processors with a dead endpoint."""
        os.environ["ENABLE_DOCLING"] = "true"
        os.environ.pop("DOCLING_API_URL", None)
        try:
            _reload_config()
            config = get_document_processor_config()
            assert "docling" not in config["processors"]
        finally:
            os.environ.pop("ENABLE_DOCLING", None)

    def test_multiple_processors(self):
        """Test configuration with multiple processors enabled."""
        os.environ["ENABLE_DOCUMENT_PROCESSING"] = "true"
        os.environ["ENABLE_UNSTRUCTURED"] = "true"
        os.environ["ENABLE_TESSERACT"] = "true"

        try:
            _reload_config()
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
        os.environ.pop("DOCUMENT_PROCESSOR", None)
        _reload_config()
        config = get_document_processor_config()
        assert config["default_processor"] == "unstructured"

        os.environ["DOCUMENT_PROCESSOR"] = "tesseract"
        try:
            _reload_config()
            config = get_document_processor_config()
            assert config["default_processor"] == "tesseract"
        finally:
            os.environ.pop("DOCUMENT_PROCESSOR", None)
