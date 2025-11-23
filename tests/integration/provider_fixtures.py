"""Provider fixtures for integration tests.

This module provides pytest fixtures that configure LLM providers based on
an explicit --provider flag. Supports OpenAI, Ollama, Anthropic, and Bedrock.

Usage:
    pytest tests/integration/test_rag.py --provider=openai
    pytest tests/integration/test_rag.py --provider=ollama
    pytest tests/integration/test_rag.py --provider=anthropic
    pytest tests/integration/test_rag.py --provider=bedrock

Environment Variables by Provider:

OpenAI:
    OPENAI_API_KEY: API key (required)
    OPENAI_BASE_URL: Base URL override (e.g., "https://models.github.ai/inference")
    OPENAI_EMBEDDING_MODEL: Embedding model (default: "text-embedding-3-small")
    OPENAI_GENERATION_MODEL: Generation model (default: "gpt-4o-mini")

Ollama:
    OLLAMA_BASE_URL: API URL (required, e.g., "http://localhost:11434")
    OLLAMA_EMBEDDING_MODEL: Embedding model (default: "nomic-embed-text")
    OLLAMA_GENERATION_MODEL: Generation model (default: "llama3.2:1b")

Anthropic:
    ANTHROPIC_API_KEY: API key (required)
    ANTHROPIC_GENERATION_MODEL: Model (default: "claude-3-haiku-20240307")

Bedrock:
    AWS_REGION: AWS region (required)
    BEDROCK_EMBEDDING_MODEL: Embedding model ID
    BEDROCK_GENERATION_MODEL: Generation model ID
"""

import logging
import os
from typing import AsyncGenerator

import pytest

from nextcloud_mcp_server.providers.base import Provider

logger = logging.getLogger(__name__)

# Valid provider names (must match conftest.py)
VALID_PROVIDERS = ["openai", "ollama", "anthropic", "bedrock"]


async def create_generation_provider(provider_name: str) -> Provider:
    """Create a provider configured for text generation.

    Args:
        provider_name: One of "openai", "ollama", "anthropic", "bedrock"

    Returns:
        Provider instance configured for generation

    Raises:
        ValueError: If provider_name is invalid or required env vars missing
    """
    if provider_name == "openai":
        from nextcloud_mcp_server.providers.openai import OpenAIProvider

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable required")

        base_url = os.getenv("OPENAI_BASE_URL")
        generation_model = os.getenv("OPENAI_GENERATION_MODEL", "gpt-4o-mini")

        # GitHub Models API requires model name prefix
        if base_url and "models.github.ai" in base_url:
            if not generation_model.startswith("openai/"):
                generation_model = f"openai/{generation_model}"

        provider = OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            embedding_model=None,  # Generation only
            generation_model=generation_model,
        )
        logger.info(f"Created OpenAI generation provider: model={generation_model}")
        return provider

    elif provider_name == "ollama":
        from nextcloud_mcp_server.providers.ollama import OllamaProvider

        base_url = os.getenv("OLLAMA_BASE_URL")
        if not base_url:
            raise ValueError("OLLAMA_BASE_URL environment variable required")

        generation_model = os.getenv("OLLAMA_GENERATION_MODEL", "llama3.2:1b")

        provider = OllamaProvider(
            base_url=base_url,
            embedding_model=None,  # Generation only
            generation_model=generation_model,
        )
        logger.info(f"Created Ollama generation provider: model={generation_model}")
        return provider

    elif provider_name == "anthropic":
        from nextcloud_mcp_server.providers.anthropic import AnthropicProvider

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable required")

        generation_model = os.getenv(
            "ANTHROPIC_GENERATION_MODEL", "claude-3-haiku-20240307"
        )

        provider = AnthropicProvider(
            api_key=api_key,
            generation_model=generation_model,
        )
        logger.info(f"Created Anthropic generation provider: model={generation_model}")
        return provider

    elif provider_name == "bedrock":
        from nextcloud_mcp_server.providers.bedrock import BedrockProvider

        region = os.getenv("AWS_REGION")
        if not region:
            raise ValueError("AWS_REGION environment variable required")

        generation_model = os.getenv("BEDROCK_GENERATION_MODEL")
        if not generation_model:
            raise ValueError("BEDROCK_GENERATION_MODEL environment variable required")

        provider = BedrockProvider(
            region=region,
            embedding_model=None,  # Generation only
            generation_model=generation_model,
        )
        logger.info(f"Created Bedrock generation provider: model={generation_model}")
        return provider

    else:
        raise ValueError(f"Unknown provider: {provider_name}. Valid: {VALID_PROVIDERS}")


async def create_embedding_provider(provider_name: str) -> Provider:
    """Create a provider configured for embeddings.

    Args:
        provider_name: One of "openai", "ollama", "bedrock"
                      (Anthropic does not support embeddings)

    Returns:
        Provider instance configured for embeddings

    Raises:
        ValueError: If provider_name is invalid, doesn't support embeddings,
                   or required env vars missing
    """
    if provider_name == "anthropic":
        raise ValueError("Anthropic does not support embeddings")

    if provider_name == "openai":
        from nextcloud_mcp_server.providers.openai import OpenAIProvider

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable required")

        base_url = os.getenv("OPENAI_BASE_URL")
        embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        # GitHub Models API requires model name prefix
        if base_url and "models.github.ai" in base_url:
            if not embedding_model.startswith("openai/"):
                embedding_model = f"openai/{embedding_model}"

        provider = OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            embedding_model=embedding_model,
            generation_model=None,  # Embeddings only
        )
        logger.info(f"Created OpenAI embedding provider: model={embedding_model}")
        return provider

    elif provider_name == "ollama":
        from nextcloud_mcp_server.providers.ollama import OllamaProvider

        base_url = os.getenv("OLLAMA_BASE_URL")
        if not base_url:
            raise ValueError("OLLAMA_BASE_URL environment variable required")

        embedding_model = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")

        provider = OllamaProvider(
            base_url=base_url,
            embedding_model=embedding_model,
            generation_model=None,  # Embeddings only
        )
        logger.info(f"Created Ollama embedding provider: model={embedding_model}")
        return provider

    elif provider_name == "bedrock":
        from nextcloud_mcp_server.providers.bedrock import BedrockProvider

        region = os.getenv("AWS_REGION")
        if not region:
            raise ValueError("AWS_REGION environment variable required")

        embedding_model = os.getenv("BEDROCK_EMBEDDING_MODEL")
        if not embedding_model:
            raise ValueError("BEDROCK_EMBEDDING_MODEL environment variable required")

        provider = BedrockProvider(
            region=region,
            embedding_model=embedding_model,
            generation_model=None,  # Embeddings only
        )
        logger.info(f"Created Bedrock embedding provider: model={embedding_model}")
        return provider

    else:
        raise ValueError(f"Unknown provider: {provider_name}. Valid: {VALID_PROVIDERS}")


# =============================================================================
# Pytest Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def provider_name(request) -> str:
    """Get the provider name from --provider flag.

    Raises pytest.skip if --provider not specified.
    """
    name = request.config.getoption("--provider")
    if not name:
        pytest.skip("--provider flag required (openai, ollama, anthropic, bedrock)")
    return name


@pytest.fixture(scope="module")
async def generation_provider(provider_name: str) -> AsyncGenerator[Provider, None]:
    """Fixture providing a generation-capable provider.

    Requires --provider flag to be set.
    """
    provider = await create_generation_provider(provider_name)
    yield provider
    await provider.close()


@pytest.fixture(scope="module")
async def embedding_provider(provider_name: str) -> AsyncGenerator[Provider, None]:
    """Fixture providing an embedding-capable provider.

    Requires --provider flag to be set.
    Note: Anthropic does not support embeddings - test will fail if used.
    """
    if provider_name == "anthropic":
        pytest.skip("Anthropic does not support embeddings")

    provider = await create_embedding_provider(provider_name)
    yield provider
    await provider.close()
