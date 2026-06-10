"""Fixtures shared by the Pact contract tests (ADR-029).

The consumer tests build a fresh ``Pact`` per test and merge their interaction
into a single pact file under ``tests/contract/pacts/``. A session-scoped
autouse fixture wipes that directory once at the start of a run so merged pacts
never accumulate stale interactions across runs (``write_file(overwrite=False)``
merges into whatever is already on disk).
"""

import shutil
from pathlib import Path

import pytest
from pact import Pact

# Pact participant names. These MUST match the names used on the astrolabe side
# and in the broker, so keep them in sync with the astrolabe repo's pact tests.
CONSUMER = "nextcloud-mcp-server"
PROVIDER = "astrolabe"

PACT_DIR = Path(__file__).parent / "pacts"


@pytest.fixture(scope="session", autouse=True)
def _clean_pact_dir():
    """Start each session from an empty pacts directory."""
    if PACT_DIR.exists():
        shutil.rmtree(PACT_DIR)
    PACT_DIR.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def consumer_pact():
    """A fresh Pact (consumer=nextcloud-mcp-server, provider=astrolabe).

    The interaction added by each test is merged into the shared pact file on
    teardown.
    """
    pact = Pact(CONSUMER, PROVIDER).with_specification("V4")
    yield pact
    pact.write_file(PACT_DIR, overwrite=False)
