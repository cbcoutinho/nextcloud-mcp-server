"""MCP sampling support for integration tests.

This module provides utilities to enable real LLM-based sampling in integration tests
using any provider that supports text generation (OpenAI, Ollama, Anthropic, Bedrock).
"""

import logging
from typing import Any

from mcp import types
from mcp.client.session import ClientSession, RequestContext

from nextcloud_mcp_server.providers.base import Provider

logger = logging.getLogger(__name__)


def create_sampling_callback(provider: Provider):
    """Factory to create a sampling callback using any generation-capable provider.

    The callback conforms to MCP's SamplingFnT protocol and can be passed
    to ClientSession for handling sampling requests from the server.

    Args:
        provider: Any Provider instance that supports generation
                  (supports_generation=True)

    Returns:
        Async callback function for MCP sampling

    Raises:
        ValueError: If provider doesn't support generation

    Example:
        ```python
        from nextcloud_mcp_server.providers import get_provider

        provider = get_provider()  # Auto-detect from environment
        if provider.supports_generation:
            callback = create_sampling_callback(provider)

            async for session in create_mcp_client_session(
                url="http://localhost:8000/mcp",
                sampling_callback=callback,
            ):
                # Session now supports sampling
                pass
        ```
    """
    if not provider.supports_generation:
        raise ValueError(
            f"Provider {provider.__class__.__name__} does not support generation"
        )

    # Get model name for logging (provider-specific attribute)
    model_name = (
        getattr(provider, "generation_model", None) or provider.__class__.__name__
    )

    async def sampling_callback(
        context: RequestContext[ClientSession, Any],
        params: types.CreateMessageRequestParams,
    ) -> types.CreateMessageResult | types.ErrorData:
        """Handle sampling requests using the configured provider."""
        logger.debug(f"Sampling callback invoked with {len(params.messages)} messages")

        # Extract messages and build prompt
        messages_text = []
        for msg in params.messages:
            if hasattr(msg.content, "text"):
                role_prefix = "User" if msg.role == "user" else "Assistant"
                messages_text.append(f"{role_prefix}: {msg.content.text}")

        prompt = "\n\n".join(messages_text)

        # Add system prompt if provided
        if params.systemPrompt:
            prompt = f"System: {params.systemPrompt}\n\n{prompt}"

        logger.debug(f"Generating response for prompt ({len(prompt)} chars)")

        try:
            # Generate response using provider
            # Note: temperature is typically hardcoded in providers at 0.7
            response = await provider.generate(
                prompt=prompt,
                max_tokens=params.maxTokens,
            )

            logger.info(f"Sampling completed: {len(response)} chars from {model_name}")

            return types.CreateMessageResult(
                role="assistant",
                content=types.TextContent(type="text", text=response),
                model=model_name,
                stopReason="endTurn",
            )
        except Exception as e:
            logger.error(f"Generation failed ({provider.__class__.__name__}): {e}")
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"Generation failed: {e!s}",
            )

    return sampling_callback


def create_openai_sampling_callback(provider: "Provider"):
    """Factory to create a sampling callback using OpenAI provider.

    This is a backward-compatible wrapper around create_sampling_callback().
    Prefer using create_sampling_callback() directly for new code.

    Args:
        provider: OpenAIProvider instance configured with a generation model

    Returns:
        Async callback function for MCP sampling
    """
    return create_sampling_callback(provider)
