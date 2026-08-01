"""Consumer contract: nextcloud-mcp-server -> embedding-gateway RERANK.

The optional rerank stage (:mod:`nextcloud_mcp_server.search.rerank`) POSTs the
query plus the candidate texts to ``POST /v1/rerank`` and reads back
``results[].index`` and ``results[].relevance_score``.

This pact pins that wire shape for the ``astrolabe-cloud-gateway`` provider. Only
the fields the client actually reads are asserted, so the contract stays
additive-safe.

Deliberately NOT pinned here: the client's defensive handling of out-of-range,
duplicate, partial and malformed index sets. A pact fixes ONE example response;
branching on response *content* belongs a tier down in
``tests/unit/providers/test_gateway_rerank.py`` — same split the models pact
documents. What matters at this boundary is that the provider keeps returning
``index`` as an integer and ``relevance_score`` as a number.

The gateway is unauthenticated today, so no bearer is sent. See ADR-029.
"""

import pytest
from pact import match

from nextcloud_mcp_server.providers.gateway_rerank import GatewayRerankClient

pytestmark = pytest.mark.contract

_MODEL = "BAAI/bge-reranker-v2-m3"
_QUERY = "who signed in at reception"
_DOCS = [
    "Quarterly revenue increased by twelve percent year over year.",
    "Visitor sign-in log for the north reception desk.",
]


async def test_rerank_returns_indices_and_scores(gateway_consumer_pact):
    """The reranker returns a ranking over the SUBMITTED list as indices.

    The second document is the relevant one, so the response ranks index 1
    first — which also makes the contract state the thing the client depends on:
    ``results`` is ordered best-first and ``index`` refers to the position in the
    request's ``documents`` array, not to any identifier of ours.
    """
    (
        gateway_consumer_pact.upon_receiving("a rerank request for two candidates")
        .given("the gateway reranks candidates with a cross-encoder")
        .with_request("POST", "/v1/rerank")
        .with_body(
            {
                "model": _MODEL,
                "query": _QUERY,
                "documents": _DOCS,
                "top_n": 2,
            },
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                "results": [
                    {
                        "index": match.integer(1),
                        "relevance_score": match.number(0.87),
                    },
                    {
                        "index": match.integer(0),
                        "relevance_score": match.number(0.02),
                    },
                ]
            }
        )
    )

    with gateway_consumer_pact.serve() as srv:
        client = GatewayRerankClient(str(srv.url), _MODEL)
        ranked = await client.rerank(_QUERY, _DOCS)

    assert [r.index for r in ranked] == [1, 0]
    assert ranked[0].score > ranked[1].score
