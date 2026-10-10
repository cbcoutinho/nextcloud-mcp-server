"""Consumer contract: nextcloud-mcp-server -> embedding-gateway NER.

Redaction (:mod:`nextcloud_mcp_sar.redaction`, ADR-040) POSTs text to
``POST /v1/ner`` and reads back ``results[].index`` plus, per entity, ``start``,
``end`` and ``label``. Those offsets are the fields the client depends on: it
takes each name from the SUBMITTED text by offset, so the provider must keep
returning character offsets into the text it was sent.

Deliberately NOT pinned: the client's strict handling of missing, duplicate and
out-of-range indices (``packages/nextcloud-mcp-sar/tests/test_sar_ner_client.py``). The gateway is
unauthenticated today, so no bearer is sent. See ADR-029.
"""

import pytest
from nextcloud_mcp_sar.ner import NerClient
from pact import match

pytestmark = pytest.mark.contract

_MODEL = "local/urchade/gliner_multi_pii-v1"
_TEXTS = ["Dear Karen Smith, 72 High Street.", "Quarterly revenue rose."]


async def test_ner_returns_person_and_address_offsets(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving(
            "an NER request for people and addresses in two texts"
        )
        .given("the gateway detects person names and addresses")
        .with_request("POST", "/v1/ner")
        .with_body(
            {
                "model": _MODEL,
                "texts": _TEXTS,
                "labels": ["person", "address"],
                "threshold": 0.5,
            },
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                "results": [
                    {
                        "index": match.integer(0),
                        "entities": [
                            {
                                "start": match.integer(5),
                                "end": match.integer(16),
                                "label": "person",
                                "score": match.number(0.93),
                            },
                            {
                                "start": match.integer(18),
                                "end": match.integer(32),
                                "label": "address",
                                "score": match.number(0.88),
                            },
                        ],
                    },
                    {"index": match.integer(1), "entities": []},
                ]
            }
        )
    )

    with gateway_consumer_pact.serve() as srv:
        client = NerClient(f"{str(srv.url).rstrip('/')}/v1/ner", _MODEL)
        found = await client.detect(_TEXTS, ("person", "address"))

    assert found == [
        {("person", "Karen Smith"), ("address", "72 High Street")},
        set(),
    ]
