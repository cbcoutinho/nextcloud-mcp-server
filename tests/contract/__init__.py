"""Pact consumer/provider contract tests (ADR-029).

This package contains the Pact contract tests that keep ``nextcloud-mcp-server``
and the ``astrolabe`` Nextcloud app wire-compatible:

- **Consumer** (this server -> astrolabe): ``test_astrolabe_credentials_consumer``
  generates a pact for the background-sync credentials API that the
  ``AstrolabeClient`` consumes.
- **Provider** (astrolabe -> this server): ``test_mcp_provider_verification``
  verifies the ``/api/v1/*`` HTTP API this server exposes against pacts that
  the astrolabe app publishes to the broker.
"""
