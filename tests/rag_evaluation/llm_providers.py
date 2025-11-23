"""LLM provider abstraction for RAG evaluation.

DEPRECATED: This module is maintained for backward compatibility with RAG evaluation tests.
New code should use nextcloud_mcp_server.providers directly.

Supports Ollama (local), Anthropic (cloud), Bedrock (AWS), and OpenAI (cloud) providers
for both ground truth generation and evaluation.
"""

import os

from nextcloud_mcp_server.providers import (
    AnthropicProvider,
    BedrockProvider,
    OllamaProvider,
    OpenAIProvider,
    Provider,
)


def create_llm_provider(
    provider: str | None = None,
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    anthropic_api_key: str | None = None,
    anthropic_model: str | None = None,
    bedrock_region: str | None = None,
    bedrock_model: str | None = None,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
    openai_model: str | None = None,
) -> Provider:
    """Create an LLM provider from environment variables or arguments.

    Args:
        provider: Provider type ('ollama', 'anthropic', 'bedrock', or 'openai').
            Defaults to RAG_EVAL_PROVIDER env var or 'ollama'
        ollama_base_url: Ollama base URL. Defaults to RAG_EVAL_OLLAMA_BASE_URL or 'http://localhost:11434'
        ollama_model: Ollama model. Defaults to RAG_EVAL_OLLAMA_MODEL or 'llama3.2:1b'
        anthropic_api_key: Anthropic API key. Defaults to RAG_EVAL_ANTHROPIC_API_KEY env var
        anthropic_model: Anthropic model. Defaults to RAG_EVAL_ANTHROPIC_MODEL or 'claude-3-5-sonnet-20241022'
        bedrock_region: AWS region. Defaults to RAG_EVAL_BEDROCK_REGION or AWS_REGION env var
        bedrock_model: Bedrock model ID. Defaults to RAG_EVAL_BEDROCK_MODEL or
            'anthropic.claude-3-sonnet-20240229-v1:0'
        openai_api_key: OpenAI API key. Defaults to OPENAI_API_KEY env var
        openai_base_url: OpenAI base URL. Defaults to OPENAI_BASE_URL (for GitHub Models API)
        openai_model: OpenAI model. Defaults to OPENAI_GENERATION_MODEL or 'gpt-4o-mini'

    Returns:
        Provider instance

    Raises:
        ValueError: If provider is invalid or required credentials are missing
    """
    # Get provider from args or env
    provider = provider or os.environ.get("RAG_EVAL_PROVIDER", "ollama")

    if provider == "ollama":
        # Try RAG_EVAL_OLLAMA_BASE_URL, then OLLAMA_HOST, then default
        base_url = (
            ollama_base_url
            or os.environ.get("RAG_EVAL_OLLAMA_BASE_URL")
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        )
        model = ollama_model or os.environ.get("RAG_EVAL_OLLAMA_MODEL", "llama3.2:1b")
        return OllamaProvider(
            base_url=base_url, embedding_model=None, generation_model=model
        )

    elif provider == "anthropic":
        api_key = anthropic_api_key or os.environ.get("RAG_EVAL_ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Anthropic API key required. Set RAG_EVAL_ANTHROPIC_API_KEY environment variable."
            )
        model = anthropic_model or os.environ.get(
            "RAG_EVAL_ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"
        )
        return AnthropicProvider(api_key=api_key, model=model)

    elif provider == "bedrock":
        region = bedrock_region or os.environ.get(
            "RAG_EVAL_BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1")
        )
        model = bedrock_model or os.environ.get(
            "RAG_EVAL_BEDROCK_MODEL", "anthropic.claude-3-sonnet-20240229-v1:0"
        )
        return BedrockProvider(
            region_name=region, embedding_model=None, generation_model=model
        )

    elif provider == "openai":
        api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OpenAI API key required. Set OPENAI_API_KEY environment variable."
            )
        base_url = openai_base_url or os.environ.get("OPENAI_BASE_URL")
        model = openai_model or os.environ.get("OPENAI_GENERATION_MODEL", "gpt-4o-mini")
        return OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            embedding_model=None,
            generation_model=model,
        )

    else:
        raise ValueError(
            f"Invalid provider: {provider}. Must be 'ollama', 'anthropic', 'bedrock', or 'openai'."
        )
