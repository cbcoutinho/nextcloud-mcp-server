"""Consumer contract: nextcloud-mcp-server -> embedding-gateway POST /v1/embeddings.

This is the embeddings half of the gateway boundary; until now only OCR, rerank
and the model catalogue were pacted, so the endpoint the whole indexing and query
path depends on had no contract at all.

Two things it pins that nothing else does:

**The wire format is base64, not a float array.** The OpenAI SDK sets
``encoding_format="base64"`` whenever the caller does not pass one
(``openai/resources/embeddings.py``: ``if not is_given(encoding_format):
params["encoding_format"] = "base64"``) and decodes the payload client-side. Every
embedding this service has ever requested crossed the wire that way, and no test
said so. A gateway that only implemented the float form would break this client
while looking OpenAI-compatible.

**``dimensions`` must be honoured, not merely accepted.** Matryoshka-capable
models return a truncated, re-normalised prefix when asked for a narrower output
(``EMBEDDING_DIMENSIONS``). Measured 2026-08-15, the gateway accepts the parameter
and silently ignores it on all three of its backend paths — OpenRouter, Bedrock
and local — returning the model's full width with ``error: null``. That is the
failure this interaction exists to catch, so the width is asserted rather than
left to a type matcher: the expected body is a base64 string of exactly the
length a 256-float32 vector encodes to, and a provider returning any other width
fails verification.

Deliberately NOT pinned here: which models support truncation. That is catalogue
metadata (``GET /v1/models``, see test_gateway_models_consumer.py) and a rejection
rule the provider owns — a 400 for an unsupported width is a *different*
interaction under a different provider state, and this client has no branch for
it beyond failing loudly. Consumer-side handling of a width that comes back wrong
lives a tier down in tests/unit/providers/test_embedding_dimensions.py.

The gateway is unauthenticated today, so no bearer is sent. See ADR-029.
"""

import array
import base64

import pytest
from pact import match

from nextcloud_mcp_server.providers.gateway import GatewayProvider

pytestmark = pytest.mark.contract

_MODEL = "openrouter/openai/text-embedding-3-small"
_INPUT = "the quarterly report was filed late"
_TRUNCATED = 256

# The example the mock server replays, and the exact shape the provider must
# match. float32 little-endian is what the SDK decodes with
# ``array.array("f", base64.b64decode(...))``, so 256 dims is 1024 bytes.
_EXAMPLE_EMBEDDING = base64.b64encode(
    array.array("f", [0.0123] * _TRUNCATED).tobytes()
).decode()
# Length is the assertion: a full-width 1536-dim vector encodes to a very
# different number of characters, so an ignored ``dimensions`` cannot pass.
_EXACT_WIDTH = rf"^[A-Za-z0-9+/=]{{{len(_EXAMPLE_EMBEDDING)}}}$"


async def test_embeddings_honour_the_requested_output_width(gateway_consumer_pact):
    """A truncated embedding request returns a vector of exactly that width."""
    (
        gateway_consumer_pact.upon_receiving("a truncated embedding request")
        .given("the gateway serves a Matryoshka-capable embedding model")
        .with_request("POST", "/v1/embeddings")
        .with_body(
            {
                "input": _INPUT,
                "model": _MODEL,
                # Sent by the SDK on every request, not by this client's choice.
                "encoding_format": "base64",
                "dimensions": _TRUNCATED,
            },
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                "object": match.string("list"),
                "data": [
                    {
                        "object": match.string("embedding"),
                        "index": match.integer(0),
                        "embedding": match.regex(
                            _EXAMPLE_EMBEDDING, regex=_EXACT_WIDTH
                        ),
                    }
                ],
                "model": match.string(_MODEL),
                # Read by _embed_batch_request for usage metering; the same
                # response shape serves the single-input path used here.
                "usage": {
                    "prompt_tokens": match.integer(7),
                    "total_tokens": match.integer(7),
                },
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        provider = GatewayProvider(
            base_url=str(srv.url),
            embedding_model=_MODEL,
            embedding_dimensions=_TRUNCATED,
        )
        embedding = await provider.embed(_INPUT)

    # Decoded client-side by the SDK, and accepted by _record_dimension — which
    # would have raised had the width disagreed with the request.
    assert len(embedding) == _TRUNCATED
    assert provider.get_dimension() == _TRUNCATED
