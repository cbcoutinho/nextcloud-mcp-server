"""Every provider must pin an explicit timeout.

The house convention is 120s read / 5s connect (ollama.py, openai.py). Without
it the underlying SDKs fall back to their own defaults — 600s for the Anthropic
SDK, 60s read with 3 retries for botocore — long enough that a wedged endpoint
looks like a hang rather than a failure.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_anthropic_provider_pins_a_timeout(mocker):
    from nextcloud_mcp_server.providers.anthropic import AnthropicProvider

    ctor = mocker.patch("nextcloud_mcp_server.providers.anthropic.AsyncAnthropic")

    AnthropicProvider(api_key="k")

    timeout = ctor.call_args.kwargs["timeout"]
    assert timeout.read == AnthropicProvider.DEFAULT_TIMEOUT_SECONDS
    assert timeout.connect == AnthropicProvider.DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_anthropic_provider_accepts_an_explicit_timeout(mocker):
    import httpx

    from nextcloud_mcp_server.providers.anthropic import AnthropicProvider

    ctor = mocker.patch("nextcloud_mcp_server.providers.anthropic.AsyncAnthropic")
    supplied = httpx.Timeout(timeout=7, connect=1)

    AnthropicProvider(api_key="k", timeout=supplied)

    assert ctor.call_args.kwargs["timeout"] is supplied


def test_bedrock_provider_pins_botocore_timeouts(mocker):
    bedrock = pytest.importorskip("nextcloud_mcp_server.providers.bedrock")
    if not bedrock.BOTO3_AVAILABLE:
        pytest.skip("boto3 not installed")

    client = mocker.patch.object(bedrock.boto3, "client")

    bedrock.BedrockProvider(region_name="us-east-1")

    config = client.call_args.kwargs["config"]
    assert (
        config.connect_timeout
        == bedrock.BedrockProvider.DEFAULT_CONNECT_TIMEOUT_SECONDS
    )
    assert config.read_timeout == bedrock.BedrockProvider.DEFAULT_TIMEOUT_SECONDS
