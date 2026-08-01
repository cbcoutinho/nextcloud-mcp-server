"""Consumer contract: nextcloud-mcp-server -> embedding-gateway GET /v1/models.

:meth:`GatewayProvider._detect_dimension` reads the gateway's model catalogue at
startup to size the Qdrant collection, because the gateway is the authority on the
dimensions of the models it serves (``mistral-embed`` is not an OpenAI model, so the
OpenAI-wire base class cannot know its size statically).

This pact pins the shape that lookup depends on: a ``data`` array of objects with an
``id`` and — for embedding models — an integer ``dimension``.

**Why this pact exists now (astrolabe-cloud-website, Deck #931).** The gateway merged
its rerank catalogue into this same endpoint and removed ``GET /v1/rerank/models``, so
``/v1/models`` now returns entries that carry NO ``dimension`` (a cross-encoder emits
a score, not a vector) alongside the embedding entries this client wants. That is a
response-shape change to a surface we consume, and nothing pinned it before.

The interaction therefore deliberately includes a rerank entry the consumer must
*skip*, so verification exercises a mixed catalogue rather than a uniform one.
``_detect_dimension`` tolerates it by matching on ``id`` and reading
``entry.get("dimension")`` behind an ``isinstance(dim, int)`` guard.

**What this pact does and does not guarantee.** Pact matches objects leniently — extra
keys in the actual response are allowed, which is what makes contracts additive-safe.
Verified empirically: a provider emitting ``dimension: 0`` on the rerank entry still
passes this pact. So the *omission* of ``dimension`` for a scoring model is NOT
enforced here; it is enforced provider-side by
``test_models_route.py::test_rerank_models_omit_dimension_entirely`` in
astrolabe-cloud-website. What this pact does pin is the part the consumer genuinely
depends on: ``data`` is a list, entries carry ``id``, and an embedding entry carries
an integer ``dimension``.

The gateway is unauthenticated today, so no bearer is sent. See ADR-029.
"""

import pytest
from pact import match

from nextcloud_mcp_server.providers.gateway import GatewayProvider

pytestmark = pytest.mark.contract

_EMBED_MODEL = "mistral/mistral-embed"
_RERANK_MODEL = "local/BAAI/bge-reranker-v2-m3"
_DIMENSION = 1024


async def test_detect_dimension_reads_the_model_catalogue(gateway_consumer_pact):
    """The happy path: the configured embedding model's dimension is resolved from
    ``/v1/models``, ignoring entries for other surfaces."""
    (
        gateway_consumer_pact.upon_receiving("a model catalogue request")
        .given("the gateway serves an embedding model and a rerank model")
        .with_request("GET", "/v1/models")
        .will_respond_with(200)
        .with_body(
            {
                "object": match.string("list"),
                "data": [
                    {
                        "id": _EMBED_MODEL,
                        # The one field this client actually consumes. Matched as an
                        # integer TYPE — the value is the model's business.
                        "dimension": match.integer(_DIMENSION),
                        "object": match.string("model"),
                    },
                    {
                        # A rerank entry, present so verification runs against a
                        # MIXED catalogue — the realistic shape since Deck #931 —
                        # rather than a list where every entry happens to be usable.
                        # No "dimension" is declared because this client never reads
                        # one here; see the module docstring on why that omission is
                        # not itself enforceable at this tier.
                        "id": _RERANK_MODEL,
                        "object": match.string("model"),
                    },
                ],
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        provider = GatewayProvider(base_url=str(srv.url), embedding_model=_EMBED_MODEL)
        await provider._detect_dimension()

    assert provider._dimension == _DIMENSION


# NOTE — deliberately ONE interaction. The obvious second case ("the configured model
# is absent → _dimension stays unset") is CLIENT BRANCHING, not wire shape: the
# request is byte-identical and the gateway's response under this provider state is
# the same either way, so expressing it as a pact would mean two interactions sharing
# one state while declaring different `data` array lengths — which fails verification,
# since pact compares array length strictly. That behaviour is already covered a tier
# down by tests/unit/providers/test_gateway_provider.py::test_detect_dimension_model_absent
# (alongside the http-error and already-known paths). Contracts pin what crosses the
# wire; how this client reacts to it is its own business.
