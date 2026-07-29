"""Unified provider infrastructure for embeddings."""

from .base import Provider
from .bedrock import BedrockProvider
from .bm25 import BM25SparseEmbeddingProvider, get_bm25_service
from .mistral import MistralProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .registry import create_provider, get_provider, reset_provider
from .simple import SimpleProvider

__all__ = [
    "Provider",
    "OllamaProvider",
    "OpenAIProvider",
    "MistralProvider",
    "SimpleProvider",
    "BedrockProvider",
    "BM25SparseEmbeddingProvider",
    "get_bm25_service",
    "create_provider",
    "get_provider",
    "reset_provider",
]
