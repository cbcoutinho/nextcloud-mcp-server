"""Provider registry and factory for auto-detection and instantiation."""

import logging
import os

from .base import Provider
from .bedrock import BedrockProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .simple import SimpleProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Registry for provider auto-detection and instantiation.

    Checks environment variables in priority order and creates appropriate provider:
    1. Bedrock (AWS_REGION + BEDROCK_*_MODEL)
    2. OpenAI (OPENAI_API_KEY)
    3. Ollama (OLLAMA_BASE_URL)
    4. Simple (fallback for testing/development)
    """

    @staticmethod
    def create_provider() -> Provider:
        """
        Auto-detect and create provider based on environment variables.

        Priority order:
        1. Bedrock - if AWS_REGION or BEDROCK_EMBEDDING_MODEL is set
        2. OpenAI - if OPENAI_API_KEY is set
        3. Ollama - if OLLAMA_BASE_URL is set
        4. Simple - fallback for testing/development

        Returns:
            Provider instance

        Environment Variables:
            Bedrock:
                - AWS_REGION: AWS region (e.g., "us-east-1")
                - AWS_ACCESS_KEY_ID: AWS access key (optional, uses credential chain)
                - AWS_SECRET_ACCESS_KEY: AWS secret key (optional)
                - BEDROCK_EMBEDDING_MODEL: Model ID for embeddings (e.g., "amazon.titan-embed-text-v2:0")
                - BEDROCK_GENERATION_MODEL: Model ID for text generation (e.g., "anthropic.claude-3-sonnet-20240229-v1:0")

            OpenAI:
                - OPENAI_API_KEY: OpenAI API key (or GITHUB_TOKEN for GitHub Models)
                - OPENAI_BASE_URL: Base URL override (e.g., "https://models.github.ai/inference")
                - OPENAI_EMBEDDING_MODEL: Model for embeddings (default: "text-embedding-3-small")
                - OPENAI_GENERATION_MODEL: Model for text generation (e.g., "gpt-4o-mini")

            Ollama:
                - OLLAMA_BASE_URL: Ollama API base URL (e.g., "http://localhost:11434")
                - OLLAMA_EMBEDDING_MODEL: Model for embeddings (default: "nomic-embed-text")
                - OLLAMA_GENERATION_MODEL: Model for text generation (e.g., "llama3.2:1b")
                - OLLAMA_VERIFY_SSL: Verify SSL certificates (default: "true")

            Simple (no configuration needed, fallback):
                - SIMPLE_EMBEDDING_DIMENSION: Embedding dimension (default: 384)
        """
        # 1. Check for Bedrock
        aws_region = os.getenv("AWS_REGION")
        bedrock_embedding_model = os.getenv("BEDROCK_EMBEDDING_MODEL")
        bedrock_generation_model = os.getenv("BEDROCK_GENERATION_MODEL")

        if aws_region or bedrock_embedding_model or bedrock_generation_model:
            logger.info(
                f"Using Bedrock provider: region={aws_region}, "
                f"embedding_model={bedrock_embedding_model}, "
                f"generation_model={bedrock_generation_model}"
            )
            return BedrockProvider(
                region_name=aws_region,
                embedding_model=bedrock_embedding_model,
                generation_model=bedrock_generation_model,
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            )

        # 2. Check for OpenAI
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if openai_api_key:
            base_url = os.getenv("OPENAI_BASE_URL")
            embedding_model = os.getenv(
                "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
            )
            generation_model = os.getenv("OPENAI_GENERATION_MODEL")

            logger.info(
                f"Using OpenAI provider: base_url={base_url or 'default'}, "
                f"embedding_model={embedding_model}, "
                f"generation_model={generation_model}"
            )
            return OpenAIProvider(
                api_key=openai_api_key,
                base_url=base_url,
                embedding_model=embedding_model,
                generation_model=generation_model,
            )

        # 3. Check for Ollama (local LLM)
        ollama_url = os.getenv("OLLAMA_BASE_URL")
        if ollama_url:
            embedding_model = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
            generation_model = os.getenv("OLLAMA_GENERATION_MODEL")
            verify_ssl = os.getenv("OLLAMA_VERIFY_SSL", "true").lower() == "true"

            logger.info(
                f"Using Ollama provider: {ollama_url}, "
                f"embedding_model={embedding_model}, "
                f"generation_model={generation_model}"
            )
            return OllamaProvider(
                base_url=ollama_url,
                embedding_model=embedding_model,
                generation_model=generation_model,
                verify_ssl=verify_ssl,
            )

        # 4. Fallback to Simple provider for development/testing
        dimension = int(os.getenv("SIMPLE_EMBEDDING_DIMENSION", "384"))
        logger.warning(
            "No provider configured (AWS_REGION, OPENAI_API_KEY, OLLAMA_BASE_URL not set). "
            "Using SimpleProvider for testing/development. "
            "For production, configure Bedrock, OpenAI, or Ollama."
        )
        return SimpleProvider(dimension=dimension)


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
