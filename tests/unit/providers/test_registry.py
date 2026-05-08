"""Unit tests for ProviderRegistry — dynaconf-driven auto-detection."""

import pytest

from nextcloud_mcp_server.config import _reload_config
from nextcloud_mcp_server.providers import (
    BedrockProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    SimpleProvider,
    get_provider,
    reset_provider,
)
from nextcloud_mcp_server.providers.bedrock import BOTO3_AVAILABLE


def _clear_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every provider-selection env var so each test starts clean."""
    for name in (
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "BEDROCK_EMBEDDING_MODEL",
        "BEDROCK_GENERATION_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_GENERATION_MODEL",
        "MISTRAL_API_KEY",
        "MISTRAL_BASE_URL",
        "MISTRAL_EMBEDDING_MODEL",
        "OLLAMA_BASE_URL",
        "OLLAMA_EMBEDDING_MODEL",
        "OLLAMA_GENERATION_MODEL",
        "OLLAMA_VERIFY_SSL",
        "SIMPLE_EMBEDDING_DIMENSION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clean_provider_env(monkeypatch):
    """Reset provider singleton + dynaconf cache around each test."""
    _clear_provider_envs(monkeypatch)
    reset_provider()
    _reload_config()
    yield monkeypatch
    reset_provider()


@pytest.mark.unit
def test_registry_falls_back_to_simple(clean_provider_env):
    """No provider env set → SimpleProvider (with default dimension)."""
    provider = get_provider()
    assert isinstance(provider, SimpleProvider)
    assert provider.get_dimension() == 384


@pytest.mark.unit
def test_registry_picks_simple_with_custom_dimension(clean_provider_env):
    """SIMPLE_EMBEDDING_DIMENSION flows through dynaconf to SimpleProvider."""
    clean_provider_env.setenv("SIMPLE_EMBEDDING_DIMENSION", "512")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, SimpleProvider)
    assert provider.get_dimension() == 512


@pytest.mark.unit
def test_registry_picks_mistral_when_api_key_set(clean_provider_env, mocker):
    """MISTRAL_API_KEY alone is enough to select MistralProvider."""
    # MistralProvider eagerly constructs the SDK client in __init__; stub it
    # so the test doesn't depend on the SDK accepting arbitrary keys.
    mocker.patch("nextcloud_mcp_server.providers.mistral.Mistral")
    clean_provider_env.setenv("MISTRAL_API_KEY", "test-key")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, MistralProvider)


@pytest.mark.unit
def test_registry_picks_ollama_when_base_url_set(clean_provider_env, mocker):
    """OLLAMA_BASE_URL selects OllamaProvider."""
    # OllamaProvider eagerly probes /api/tags in __init__; stub it out.
    mocker.patch(
        "nextcloud_mcp_server.providers.ollama.OllamaProvider._check_model_is_loaded"
    )
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, OllamaProvider)


@pytest.mark.unit
def test_registry_openai_wins_over_mistral_and_ollama(clean_provider_env):
    """OpenAI takes priority when multiple provider env vars are set."""
    clean_provider_env.setenv("OPENAI_API_KEY", "openai-key")
    clean_provider_env.setenv("MISTRAL_API_KEY", "mistral-key")
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, OpenAIProvider)


@pytest.mark.unit
def test_registry_mistral_wins_over_ollama(clean_provider_env, mocker):
    """Mistral takes priority over Ollama when both are configured."""
    # Stub the Mistral SDK constructor for the same reason as the sibling
    # picker test — keeps the registry test independent of SDK key validation.
    mocker.patch("nextcloud_mcp_server.providers.mistral.Mistral")
    clean_provider_env.setenv("MISTRAL_API_KEY", "mistral-key")
    clean_provider_env.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, MistralProvider)


@pytest.mark.unit
def test_registry_bedrock_wins_when_aws_region_set(clean_provider_env):
    """AWS_REGION alone routes to Bedrock, even with other providers configured."""
    if not BOTO3_AVAILABLE:
        pytest.skip("boto3 not installed")

    clean_provider_env.setenv("AWS_REGION", "us-east-1")
    clean_provider_env.setenv("OPENAI_API_KEY", "openai-key")
    clean_provider_env.setenv("MISTRAL_API_KEY", "mistral-key")
    _reload_config()

    provider = get_provider()
    assert isinstance(provider, BedrockProvider)
