"""Pytest configuration for integration tests.

This conftest.py provides hooks and fixtures specific to integration tests,
including the --provider flag for RAG tests.
"""

import logging

import pytest

logger = logging.getLogger(__name__)

# Valid provider names
VALID_PROVIDERS = ["openai", "ollama", "anthropic", "bedrock"]


def pytest_addoption(parser):
    """Add --provider command line option for RAG tests."""
    parser.addoption(
        "--provider",
        action="store",
        default=None,
        choices=VALID_PROVIDERS,
        help="LLM provider for RAG tests: openai, ollama, anthropic, bedrock",
    )


def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers", "rag: mark test as RAG integration test (requires --provider flag)"
    )


@pytest.fixture(autouse=True, scope="module")
async def reset_all_singletons():
    """Reset ALL global singletons between test modules.

    Prevents anyio.WouldBlock errors caused by stale singleton state
    from previous test modules holding references to dead event loops
    or closed memory streams.
    """
    # Import all modules with singletons
    import nextcloud_mcp_server.app as app_module
    import nextcloud_mcp_server.auth.client_registry as client_registry_module
    import nextcloud_mcp_server.auth.token_exchange as token_exchange_module
    import nextcloud_mcp_server.embedding.service as embedding_module
    import nextcloud_mcp_server.observability.tracing as tracing_module
    import nextcloud_mcp_server.providers.registry as registry_module
    import nextcloud_mcp_server.vector.qdrant_client as qdrant_module

    # Store originals for restoration after test
    originals = {
        "qdrant_client": qdrant_module._qdrant_client,
        "embedding_service": embedding_module._embedding_service,
        "bm25_service": embedding_module._bm25_service,
        "provider": registry_module._provider,
        "vector_sync_state": (
            app_module._vector_sync_state.document_send_stream,
            app_module._vector_sync_state.document_receive_stream,
            app_module._vector_sync_state.shutdown_event,
            app_module._vector_sync_state.scanner_wake_event,
        ),
        "tracer": tracing_module._tracer,
        "registry": client_registry_module._registry,
        "token_exchange_service": token_exchange_module._token_exchange_service,
    }

    # Close any open memory streams before reset
    if app_module._vector_sync_state.document_send_stream is not None:
        try:
            await app_module._vector_sync_state.document_send_stream.aclose()
        except Exception:
            pass
    if app_module._vector_sync_state.document_receive_stream is not None:
        try:
            await app_module._vector_sync_state.document_receive_stream.aclose()
        except Exception:
            pass

    # Reset all singletons to None/fresh state
    qdrant_module._qdrant_client = None
    embedding_module._embedding_service = None
    embedding_module._bm25_service = None
    registry_module._provider = None
    app_module._vector_sync_state.document_send_stream = None
    app_module._vector_sync_state.document_receive_stream = None
    app_module._vector_sync_state.shutdown_event = None
    app_module._vector_sync_state.scanner_wake_event = None
    tracing_module._tracer = None
    client_registry_module._registry = None
    token_exchange_module._token_exchange_service = None

    logger.debug("All singletons reset for test module")

    yield

    # Cleanup: Close async resources created during test
    if qdrant_module._qdrant_client is not None:
        try:
            await qdrant_module._qdrant_client.close()
        except Exception:
            pass

    # Restore originals
    qdrant_module._qdrant_client = originals["qdrant_client"]
    embedding_module._embedding_service = originals["embedding_service"]
    embedding_module._bm25_service = originals["bm25_service"]
    registry_module._provider = originals["provider"]
    (
        app_module._vector_sync_state.document_send_stream,
        app_module._vector_sync_state.document_receive_stream,
        app_module._vector_sync_state.shutdown_event,
        app_module._vector_sync_state.scanner_wake_event,
    ) = originals["vector_sync_state"]
    tracing_module._tracer = originals["tracer"]
    client_registry_module._registry = originals["registry"]
    token_exchange_module._token_exchange_service = originals["token_exchange_service"]
