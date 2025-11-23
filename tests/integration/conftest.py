"""Pytest configuration for integration tests.

This conftest.py provides hooks and fixtures specific to integration tests,
including the --provider flag for RAG tests.
"""

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
