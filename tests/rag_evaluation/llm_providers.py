"""LLM provider abstraction for RAG evaluation.

Supports Ollama (local) and Anthropic (cloud) providers for both ground truth
generation and evaluation.
"""

import os
from typing import Protocol

import httpx
from anthropic import AsyncAnthropic


class LLMProvider(Protocol):
    """Protocol for LLM providers."""

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """Generate text from a prompt.

        Args:
            prompt: The prompt to generate from
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        ...


class OllamaProvider:
    """Ollama provider for local LLM inference."""

    def __init__(self, base_url: str, model: str):
        """Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL (e.g., http://localhost:11434)
            model: Model name (e.g., llama3.1:8b)
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=600.0)  # 10 min timeout for generation

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """Generate text using Ollama API."""
        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0.7,
                },
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["response"]

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class AnthropicProvider:
    """Anthropic provider for cloud LLM inference."""

    def __init__(self, api_key: str, model: str):
        """Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key
            model: Model name (e.g., claude-3-5-sonnet-20241022)
        """
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """Generate text using Anthropic API."""
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    async def close(self):
        """Close the client (no-op for Anthropic)."""
        pass


def create_llm_provider(
    provider: str | None = None,
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    anthropic_api_key: str | None = None,
    anthropic_model: str | None = None,
) -> LLMProvider:
    """Create an LLM provider from environment variables or arguments.

    Args:
        provider: Provider type ('ollama' or 'anthropic'). Defaults to RAG_EVAL_PROVIDER env var or 'ollama'
        ollama_base_url: Ollama base URL. Defaults to RAG_EVAL_OLLAMA_BASE_URL or 'http://localhost:11434'
        ollama_model: Ollama model. Defaults to RAG_EVAL_OLLAMA_MODEL or 'llama3.1:8b'
        anthropic_api_key: Anthropic API key. Defaults to RAG_EVAL_ANTHROPIC_API_KEY env var
        anthropic_model: Anthropic model. Defaults to RAG_EVAL_ANTHROPIC_MODEL or 'claude-3-5-sonnet-20241022'

    Returns:
        LLMProvider instance

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
        return OllamaProvider(base_url=base_url, model=model)

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

    else:
        raise ValueError(
            f"Invalid provider: {provider}. Must be 'ollama' or 'anthropic'."
        )
