"""Unit tests for the Matryoshka output-width request (``EMBEDDING_DIMENSIONS``).

Three things are worth pinning, and they are all about the *silent* failure mode
rather than the happy path:

1. The parameter is sent only when configured — an explicit ``dimensions: null``
   is rejected by several OpenAI-compatible endpoints.
2. A returned width that disagrees with the requested one is FATAL. Endpoints
   that do not support truncation ignore the parameter without erroring
   (measured 2026-08-15 against the astrolabe embedding gateway on all three of
   its backend paths), which would otherwise size the Qdrant collection from a
   name claiming one width while it holds another.
3. The gateway must not answer a truncated request from its ``/v1/models``
   catalogue, which reports each model's full width.

The complementary risk — truncating a model that is not Matryoshka-trained — is
not detectable here. Nothing upstream validates it either (Ollama truncates
``snowflake-arctic-embed:110m`` just as happily as ``nomic-embed-text``), so it
stays an operator decision; see the note on ``Settings.embedding_dimensions``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import omit

from nextcloud_mcp_server.providers.gateway import GatewayProvider
from nextcloud_mcp_server.providers.ollama import OllamaProvider
from nextcloud_mcp_server.providers.openai import OpenAIProvider

pytestmark = pytest.mark.unit

_TRUNCATED = 256
_FULL = 1536


@pytest.fixture
def mock_openai_client(mocker):
    mock_client = MagicMock()
    mock_client.embeddings = MagicMock()
    mocker.patch(
        "nextcloud_mcp_server.providers.openai.AsyncOpenAI", return_value=mock_client
    )
    return mock_client


def _openai_response(dimension: int):
    datum = MagicMock()
    datum.embedding = [0.01] * dimension
    datum.index = 0
    response = MagicMock()
    response.data = [datum]
    response.usage = MagicMock(total_tokens=3)
    return response


def _ollama_response(dimension: int):
    resp = MagicMock()
    resp.json = MagicMock(return_value={"embeddings": [[0.01] * dimension]})
    resp.raise_for_status = MagicMock()
    return resp


async def test_openai_sends_dimensions_when_configured(mock_openai_client):
    """The configured width is sent, and the observed width is what sticks."""
    mock_openai_client.embeddings.create = AsyncMock(
        return_value=_openai_response(_TRUNCATED)
    )
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=_TRUNCATED,
    )

    embedding = await provider.embed("probe")

    assert len(embedding) == _TRUNCATED
    assert mock_openai_client.embeddings.create.await_args.kwargs["dimensions"] == (
        _TRUNCATED
    )
    # The observed width wins over the static OPENAI_EMBEDDING_DIMENSIONS entry,
    # which records the model's full 1536.
    assert provider.get_dimension() == _TRUNCATED


async def test_openai_omits_dimensions_when_unset(mock_openai_client):
    """Not ``dimensions: None`` — the key must leave the request body entirely.

    ``omit`` is the SDK's sentinel for that; a literal None would serialise as an
    explicit null, which several OpenAI-compatible endpoints reject.
    """
    mock_openai_client.embeddings.create = AsyncMock(
        return_value=_openai_response(_FULL)
    )
    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )

    await provider.embed("probe")

    assert mock_openai_client.embeddings.create.await_args.kwargs["dimensions"] is omit


async def test_openai_known_model_dimension_not_prefilled_when_truncating(
    mock_openai_client,
):
    """A requested width must not be pre-empted by the static full-width map.

    Pre-filling would let ``get_dimension()`` answer 1536 before any request,
    sizing the collection at full width for a truncated deployment.
    """
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=_TRUNCATED,
    )

    assert provider._dimension is None


async def test_openai_raises_when_endpoint_ignores_dimensions(mock_openai_client):
    """The measured gateway behaviour: parameter accepted, full width returned."""
    mock_openai_client.embeddings.create = AsyncMock(
        return_value=_openai_response(_FULL)
    )
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=_TRUNCATED,
    )

    with pytest.raises(RuntimeError, match="ignored"):
        await provider.embed("probe")


async def test_ollama_sends_dimensions_when_configured():
    """Ollama takes ``dimensions`` in the /api/embed body and truncates server-side."""
    provider = OllamaProvider(
        base_url="https://ollama:11434", embedding_dimensions=_TRUNCATED
    )
    provider.embedding_model = "nomic-embed-text"
    provider.client.post = AsyncMock(return_value=_ollama_response(_TRUNCATED))

    embeddings, _ = await provider.embed_batch_with_usage(["probe"])

    assert len(embeddings[0]) == _TRUNCATED
    assert provider.client.post.await_args.kwargs["json"]["dimensions"] == _TRUNCATED
    assert provider.get_dimension() == _TRUNCATED


async def test_ollama_omits_dimensions_when_unset():
    provider = OllamaProvider(base_url="https://ollama:11434")
    provider.embedding_model = "nomic-embed-text"
    provider.client.post = AsyncMock(return_value=_ollama_response(768))

    await provider.embed_batch_with_usage(["probe"])

    assert "dimensions" not in provider.client.post.await_args.kwargs["json"]


async def test_ollama_raises_when_endpoint_ignores_dimensions():
    provider = OllamaProvider(
        base_url="https://ollama:11434", embedding_dimensions=_TRUNCATED
    )
    provider.embedding_model = "nomic-embed-text"
    provider.client.post = AsyncMock(return_value=_ollama_response(768))

    with pytest.raises(RuntimeError, match="ignored"):
        await provider.embed_batch_with_usage(["probe"])


async def test_width_is_revalidated_on_every_batch_not_just_the_first():
    """The guard is not a first-call-only check.

    Caching short-circuits once the width is known, but the comparison does not,
    so a backend that changes width part-way through a run — a failover to a
    different model behind one endpoint — is caught rather than quietly mixing
    widths inside one collection. The first batch here is well-formed; the
    second is not.
    """
    provider = OllamaProvider(
        base_url="https://ollama:11434", embedding_dimensions=_TRUNCATED
    )
    provider.embedding_model = "nomic-embed-text"
    provider.client.post = AsyncMock(
        side_effect=[_ollama_response(_TRUNCATED), _ollama_response(768)]
    )

    with pytest.raises(RuntimeError, match="ignored"):
        await provider.embed_batch_with_usage(["first", "second"], batch_size=1)

    # The width learned from the good batch is still cached; the guard fired on
    # the comparison, not on a re-detection.
    assert provider.get_dimension() == _TRUNCATED


async def test_gateway_detect_dimension_ignores_catalogue_when_truncating(mocker):
    """``/v1/models`` reports the model's full width, so it cannot answer for a
    truncated request — the probe path must be used instead."""
    provider = GatewayProvider(
        base_url="https://gateway.example",
        embedding_model="openrouter/openai/text-embedding-3-small",
        embedding_dimensions=_TRUNCATED,
    )
    catalogue = mocker.patch("httpx.AsyncClient.get")
    # Patch the wire, not ``embed`` — the width is recorded by embed() itself,
    # so mocking that away would test nothing.
    provider.client.embeddings.create = AsyncMock(
        return_value=_openai_response(_TRUNCATED)
    )

    await provider._detect_dimension()

    catalogue.assert_not_called()
    assert provider.get_dimension() == _TRUNCATED
