"""ADR-042 step 3: the ``image`` named vector and ``image_vector_ready``.

Runs against a real in-memory Qdrant, which supports ``create_vector_name``
like a >= 1.18 server, with the deterministic ``SimpleProvider``.
"""

import logging

import pytest
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.providers.simple import SIMPLE_IMAGE_MODEL, SimpleProvider
from nextcloud_mcp_server.vector import qdrant_client as qc

pytestmark = pytest.mark.unit

COLLECTION = "c"
WIDTH = 8
LOGGER = "nextcloud_mcp_server.vector.qdrant_client"


@pytest.fixture
async def client():
    c = AsyncQdrantClient(":memory:")
    await c.create_collection(
        COLLECTION,
        vectors_config={
            "dense": models.VectorParams(size=4, distance=models.Distance.COSINE)
        },
    )
    yield c
    await c.close()


def _configure(monkeypatch, *, model, threshold=0.3, provider_model=None):
    settings = Settings(
        image_embedding_model=model, image_search_score_threshold=threshold
    )
    provider = SimpleProvider(
        dimension=WIDTH,
        image_embedding_model=model if provider_model is None else provider_model,
    )
    monkeypatch.setattr(qc, "get_settings", lambda: settings)
    monkeypatch.setattr(qc, "get_provider", lambda: provider)


async def _vectors(client) -> dict:
    return (await client.get_collection(COLLECTION)).config.params.vectors


async def _ensure(client) -> bool:
    return await qc._ensure_image_vector(client, COLLECTION, await _vectors(client))


async def test_unset_model_adds_nothing(client, monkeypatch):
    _configure(monkeypatch, model=None)
    assert await _ensure(client) is False
    assert "image" not in await _vectors(client)


async def test_confirmed_model_adds_the_vector_in_place(client, monkeypatch):
    _configure(monkeypatch, model=SIMPLE_IMAGE_MODEL)
    assert await _ensure(client) is True
    assert (await _vectors(client))["image"].size == WIDTH
    # A restart sees the vector already there and stays ready.
    assert await _ensure(client) is True


async def test_missing_floor_still_adds_the_vector_but_stays_off(
    client, monkeypatch, caplog
):
    """No floor is a warning, not an error, and doesn't block the schema."""
    _configure(monkeypatch, model=SIMPLE_IMAGE_MODEL, threshold=None)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await _ensure(client) is False
    assert (await _vectors(client))["image"].size == WIDTH
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "IMAGE_SEARCH_SCORE_THRESHOLD" in record.getMessage()


async def test_every_reason_is_listed_in_one_error(client, monkeypatch, caplog):
    _configure(monkeypatch, model="not-an-image-model", threshold=None)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await _ensure(client) is False
    assert "image" not in await _vectors(client)
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert "did not confirm 'not-an-image-model'" in message
    assert "IMAGE_SEARCH_SCORE_THRESHOLD" in message


async def test_wrong_size_is_refused_with_the_fix(client, monkeypatch, caplog):
    await client.create_vector_name(
        COLLECTION,
        "image",
        models.DenseVectorNameConfig(
            dense=models.DenseVectorConfig(size=16, distance=models.Distance.COSINE)
        ),
    )
    _configure(monkeypatch, model=SIMPLE_IMAGE_MODEL)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert await _ensure(client) is False
    assert "has size 16" in caplog.text
    assert "delete_vector_name" in caplog.text


async def test_an_old_server_degrades_instead_of_raising(
    client, monkeypatch, mocker, caplog
):
    """Qdrant < 1.18 has no create_vector_name: text search must still start."""
    _configure(monkeypatch, model=SIMPLE_IMAGE_MODEL)
    mocker.patch.object(
        client, "create_vector_name", side_effect=RuntimeError("404 Not Found")
    )
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert await _ensure(client) is False
    assert "needs Qdrant >= 1.18; restart to retry" in caplog.text


async def test_a_confirmed_model_with_no_width_stays_off(client, monkeypatch, caplog):
    """Never ready over a vector that was never created."""
    _configure(monkeypatch, model=SIMPLE_IMAGE_MODEL)
    monkeypatch.setattr(SimpleProvider, "get_image_dimension", lambda self: None)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert await _ensure(client) is False
    assert "image" not in await _vectors(client)
    assert "reported no width" in caplog.text
