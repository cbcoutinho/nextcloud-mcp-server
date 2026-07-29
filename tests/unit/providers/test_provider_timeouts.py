"""Every provider must pin an explicit timeout.

The house convention is 120s read / 5s connect (ollama.py, openai.py). Without
it the underlying SDKs fall back to their own defaults — 600s for the Anthropic
SDK, 60s read with 3 retries for botocore — long enough that a wedged endpoint
looks like a hang rather than a failure.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


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
