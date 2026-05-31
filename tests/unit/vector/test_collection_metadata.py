"""Collection metadata source + sentinel (design §10.1)."""

from types import SimpleNamespace

import httpx

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.vector import collection_metadata as cm
from nextcloud_mcp_server.vector.payload_keys import EMBEDDING_IDENTITY


async def test_qdrant_read_hit(mocker):
    client = mocker.AsyncMock()
    client.retrieve.return_value = [
        SimpleNamespace(
            payload={
                EMBEDDING_IDENTITY: "mistral-embed",
                cm.CHUNKING_CONFIG: {"chunk_size": 1024, "chunk_overlap": 100},
                cm.IS_SENTINEL: True,
            }
        )
    ]
    meta = await cm.read_collection_metadata(client, "col", Settings())
    assert meta["embedding_identity"] == "mistral-embed"
    assert meta["chunking_config"]["chunk_size"] == 1024


async def test_qdrant_miss_falls_back_to_env(mocker):
    client = mocker.AsyncMock()
    client.retrieve.return_value = []  # no sentinel
    settings = Settings(document_chunk_size=2048, document_chunk_overlap=200)
    meta = await cm.read_collection_metadata(client, "col", settings)
    assert meta == cm.env_default_metadata(settings)
    assert meta["chunking_config"]["chunk_size"] == 2048


async def test_qdrant_error_falls_back_to_env(mocker):
    client = mocker.AsyncMock()
    client.retrieve.side_effect = RuntimeError("qdrant down")
    settings = Settings()
    meta = await cm.read_collection_metadata(client, "col", settings)
    assert meta == cm.env_default_metadata(settings)


async def test_api_source(mocker):
    settings = Settings(
        collection_metadata_source="api",
        collection_metadata_api_url="https://cp",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/qdrant-collections/col/metadata"
        return httpx.Response(
            200,
            json={
                "embedding_identity": "amazon.titan-embed-text-v2:0",
                "chunking_config": {"chunk_size": 512, "chunk_overlap": 50},
            },
        )

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    mocker.patch.object(
        httpx,
        "AsyncClient",
        lambda *a, **k: orig(*a, **{**k, "transport": transport}),
    )

    meta = await cm.read_collection_metadata(mocker.AsyncMock(), "col", settings)
    assert meta["embedding_identity"] == "amazon.titan-embed-text-v2:0"


async def test_api_source_uses_shared_http_client():
    """When a caller passes http_client, the read reuses it (no own client)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/qdrant-collections/col/metadata"
        return httpx.Response(200, json={"embedding_identity": "mistral-embed"})

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with shared:
        meta = await cm._read_from_api("https://cp", "col", client=shared)
    assert meta["embedding_identity"] == "mistral-embed"


async def test_api_missing_url_falls_back_to_env(mocker):
    """COLLECTION_METADATA_SOURCE=api with no URL must not crash the query path."""
    # Bypass Settings validation to simulate a -O / mutated-state edge case.
    settings = Settings()
    settings.collection_metadata_source = "api"
    settings.collection_metadata_api_url = None
    meta = await cm.read_collection_metadata(mocker.AsyncMock(), "col", settings)
    assert meta == cm.env_default_metadata(settings)


async def test_upsert_sentinel_builds_point(mocker):
    client = mocker.AsyncMock()
    await cm.upsert_sentinel(
        client,
        "col",
        embedding_identity="mistral-embed",
        chunking_config={"chunk_size": 2048, "chunk_overlap": 200},
        dimension=4,
    )
    client.upsert.assert_awaited_once()
    kwargs = client.upsert.await_args.kwargs
    assert kwargs["collection_name"] == "col"
    point = kwargs["points"][0]
    assert str(point.id) == cm.SENTINEL_POINT_ID
    # Non-zero dense (cosine-safe), empty sparse.
    assert point.vector["dense"][0] > 0.0
    assert len(point.vector["dense"]) == 4
    assert point.payload[EMBEDDING_IDENTITY] == "mistral-embed"
    assert point.payload[cm.IS_SENTINEL] is True
