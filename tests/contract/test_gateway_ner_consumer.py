"""Consumer contract: nextcloud-mcp-server -> embedding-gateway NER.

Redaction (:mod:`nextcloud_mcp_server.redaction`, ADR-038) POSTs text to
``POST /v1/ner`` and reads back ``results[].index`` plus, per entity, ``start``,
``end`` and ``label``. Those offsets are the fields the client depends on: it
takes each name from the SUBMITTED text by offset, so the provider must keep
returning character offsets into the text it was sent.

Deliberately NOT pinned: the client's strict handling of missing, duplicate and
out-of-range indices (``tests/unit/providers/test_ner_client.py``). The gateway is
unauthenticated today, so no bearer is sent. See ADR-029.
"""

import pytest
from pact import match

from nextcloud_mcp_server.providers.ner import NerClient

pytestmark = pytest.mark.contract

_MODEL = "local/urchade/gliner_multi_pii-v1"
_TEXTS = ["Dear Karen Smith, thank you.", "Quarterly revenue rose."]


async def test_ner_returns_person_offsets(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving("an NER request for two texts")
        .given("the gateway detects person names")
        .with_request("POST", "/v1/ner")
        .with_body(
            {
                "model": _MODEL,
                "texts": _TEXTS,
                "labels": ["person"],
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
                            }
                        ],
                    },
                    {"index": match.integer(1), "entities": []},
                ]
            }
        )
    )

    with gateway_consumer_pact.serve() as srv:
        client = NerClient(f"{str(srv.url).rstrip('/')}/v1/ner", _MODEL)
        found = await client.detect(_TEXTS)

    assert found == [{"Karen Smith"}, set()]
