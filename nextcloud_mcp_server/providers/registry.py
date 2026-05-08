"""Provider registry and factory for auto-detection and instantiation."""

import logging

from ..config import get_settings
from .base import Provider
from .bedrock import BedrockProvider
from .mistral import MistralProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .simple import SimpleProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Registry for provider auto-detection and instantiation.

    Reads configuration via dynaconf-backed Settings (see ``config.py``).
    Checks provider settings in priority order and creates the appropriate
    provider:

    1. Bedrock (``AWS_REGION`` or ``BEDROCK_*_MODEL``)
    2. OpenAI (``OPENAI_API_KEY``)
    3. Mistral (``MISTRAL_API_KEY``)
    4. Ollama (``OLLAMA_BASE_URL``)
    5. Simple (fallback for testing/development)
    """

    @staticmethod
    def create_provider() -> Provider:
        """
        Auto-detect and create provider based on configured settings.

        Settings are sourced via :func:`nextcloud_mcp_server.config.get_settings`,
        which reads from settings files and environment variables (env vars
        always win, see ADR-024/025).

        Priority order:

        1. Bedrock - if ``aws_region`` or ``bedrock_embedding_model`` is set
        2. OpenAI - if ``openai_api_key`` is set
        3. Mistral - if ``mistral_api_key`` is set
        4. Ollama - if ``ollama_base_url`` is set
        5. Simple - fallback for testing/development

        Returns:
            Provider instance
        """
        settings = get_settings()

        # 1. Bedrock
        if (
            settings.aws_region
            or settings.bedrock_embedding_model
            or settings.bedrock_generation_model
        ):
            logger.info(
                "Using Bedrock provider: region=%s, embedding_model=%s, "
                "generation_model=%s",
                settings.aws_region,
                settings.bedrock_embedding_model,
                settings.bedrock_generation_model,
            )
            return BedrockProvider(
                region_name=settings.aws_region,
                embedding_model=settings.bedrock_embedding_model,
                generation_model=settings.bedrock_generation_model,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )

        # 2. OpenAI
        if settings.openai_api_key:
            logger.info(
                "Using OpenAI provider: base_url=%s, embedding_model=%s, "
                "generation_model=%s",
                settings.openai_base_url or "default",
                settings.openai_embedding_model,
                settings.openai_generation_model,
            )
            return OpenAIProvider(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                embedding_model=settings.openai_embedding_model,
                generation_model=settings.openai_generation_model,
            )

        # 3. Mistral
        if settings.mistral_api_key:
            logger.info(
                "Using Mistral provider: base_url=%s, embedding_model=%s",
                settings.mistral_base_url or "default",
                settings.mistral_embedding_model,
            )
            return MistralProvider(
                api_key=settings.mistral_api_key,
                base_url=settings.mistral_base_url,
                embedding_model=settings.mistral_embedding_model,
            )

        # 4. Ollama
        if settings.ollama_base_url:
            logger.info(
                "Using Ollama provider: %s, embedding_model=%s, generation_model=%s",
                settings.ollama_base_url,
                settings.ollama_embedding_model,
                settings.ollama_generation_model,
            )
            return OllamaProvider(
                base_url=settings.ollama_base_url,
                embedding_model=settings.ollama_embedding_model,
                generation_model=settings.ollama_generation_model,
                verify_ssl=settings.ollama_verify_ssl,
            )

        # 5. Simple (fallback)
        logger.warning(
            "No provider configured (AWS_REGION, OPENAI_API_KEY, "
            "MISTRAL_API_KEY, OLLAMA_BASE_URL not set). "
            "Using SimpleProvider for testing/development. "
            "For production, configure Bedrock, OpenAI, Mistral, or Ollama."
        )
        return SimpleProvider(dimension=settings.simple_embedding_dimension)


# Singleton instance
_provider: Provider | None = None


def get_provider() -> Provider:
    """
    Get singleton provider instance.

    Returns:
        Global Provider instance (auto-detected on first call)
    """
    global _provider
    if _provider is None:
        _provider = ProviderRegistry.create_provider()
    return _provider


def reset_provider():
    """
    Reset singleton provider instance.

    Useful for testing or reconfiguration.
    """
    global _provider
    _provider = None
