"""Provider verification: astrolabe -> nextcloud-mcp-server /api/v1 API.

The astrolabe Nextcloud app consumes this server's ``/api/v1/*`` HTTP API
(``lib/Service/McpServerClient.php``: ``search``, ``webhooks`` CRUD, ``apps``,
``status``, ``vector-sync/status``, ``chunk-context``, ``pdf-preview``,
``vector-viz/search``). This test plays the **provider** role: it pulls the
pacts astrolabe published to the broker and replays each interaction against a
running MCP server, failing if a response no longer matches the contract.

It is **environment-gated** and skips unless a running provider and a pact
source are configured, so it is a no-op in the consumer-only job and in local
unit runs. Wire it into CI against the integration docker stack (see
``.github/workflows/pact.yml``).

Required environment:
- ``PACT_PROVIDER_URL`` — base URL of the running MCP server to verify against
  (e.g. ``http://localhost:8000``).
- One pact source, either:
  - ``PACT_BROKER`` (+ ``PACT_USERNAME`` / ``PACT_PASSWORD``) — verify against
    pacts in the broker, or
  - ``PACT_PROVIDER_PACT_DIR`` — verify against a local directory of pacts.

Optional:
- ``PACT_PROVIDER_VERSION`` — provider version (git SHA) to publish results under.
- ``PACT_PROVIDER_BRANCH`` — provider branch for the published results.
- ``PACT_PUBLISH_RESULTS=true`` — publish verification results to the broker.

See ADR-029.
"""

import logging
import os
from collections.abc import Callable

import pytest
from pact import Verifier

logger = logging.getLogger(__name__)

PROVIDER_NAME = "nextcloud-mcp-server"

_PROVIDER_URL = os.environ.get("PACT_PROVIDER_URL")
_BROKER_URL = os.environ.get("PACT_BROKER")
_BROKER_USERNAME = os.environ.get("PACT_USERNAME")
_BROKER_PASSWORD = os.environ.get("PACT_PASSWORD")
_LOCAL_PACT_DIR = os.environ.get("PACT_PROVIDER_PACT_DIR")

# A usable broker source needs the URL *and* its basic-auth credentials; gating
# on all three keeps a misconfigured CI (broker set, creds missing) a clean skip
# rather than a confusing KeyError at verify time.
_BROKER_READY = bool(_BROKER_URL and _BROKER_USERNAME and _BROKER_PASSWORD)

# Skip the whole module unless we have a provider to hit AND a pact source.
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not _PROVIDER_URL or not (_BROKER_READY or _LOCAL_PACT_DIR),
        reason=(
            "Provider verification needs PACT_PROVIDER_URL and a pact source: "
            "PACT_BROKER (+ PACT_USERNAME/PACT_PASSWORD) or "
            "PACT_PROVIDER_PACT_DIR. Skipped outside CI."
        ),
    ),
]


# Map astrolabe-side provider-state strings -> setup callables. astrolabe's
# consumer pacts declare the ``given(...)`` provider states; add one handler per
# state name here as those pacts are written (seeding webhooks DB, qdrant
# fixtures, etc.). Keep the keys identical to the astrolabe ``given(...)``
# strings. Unhandled states fall through to ``_dispatch_state`` which logs and
# no-ops, so state-less interactions still verify.
_PROVIDER_STATES: dict[str, Callable[[], None]] = {
    # "a webhook is registered for user alice": _state_webhook_registered,
    # "vector sync has indexed documents": _state_vector_sync_ran,
    # "the search index returns a hit for 'budget'": _state_search_has_hit,
}


def _dispatch_state(state: str, **kwargs) -> None:
    """Provider-state dispatcher passed to the verifier.

    Looks up a registered handler by state name; logs and no-ops for unknown
    states so contracts that don't require seeded state (``/api/v1/status``,
    ``/api/v1/vector-sync/status``) verify without a handler.
    """
    handler = _PROVIDER_STATES.get(state)
    if handler is None:
        # Log any params astrolabe passed so they're visible once real handlers
        # need them (e.g. given("user X exists", params={"user_id": ...})).
        logger.warning(
            "No provider-state handler registered for %r (params=%s); no-op",
            state,
            kwargs,
        )
        return
    handler()


def test_verify_astrolabe_consumer_pacts() -> None:
    """Verify the MCP server honours every interaction astrolabe published."""
    verifier = Verifier(PROVIDER_NAME).add_transport(url=_PROVIDER_URL)
    verifier.state_handler(_dispatch_state, teardown=True)

    if _BROKER_URL and _BROKER_USERNAME and _BROKER_PASSWORD:
        verifier.broker_source(
            _BROKER_URL, username=_BROKER_USERNAME, password=_BROKER_PASSWORD
        )
    else:
        assert _LOCAL_PACT_DIR is not None  # guaranteed by module skipif
        verifier.add_source(_LOCAL_PACT_DIR)

    if os.environ.get("PACT_PUBLISH_RESULTS", "").lower() == "true":
        version = os.environ.get("PACT_PROVIDER_VERSION", "dev")
        verifier.set_publish_options(
            version=version,
            branch=os.environ.get("PACT_PROVIDER_BRANCH"),
        )

    # Raises (failing the test) if any interaction does not match.
    verifier.verify()
