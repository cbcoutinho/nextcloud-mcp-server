"""Whether optional features are configured, answerable without importing them.

The status endpoint, DCR scope registration and tool registration all need to
know whether optional features such as reranking are on. The modules that *implement* those
features pull in the optional semantic stack (qdrant-client, provider SDKs), so
the settings-only predicates live here, where the core server can import them
on an install without that stack. Everything in this module depends on settings
alone.
"""

from importlib.util import find_spec
from typing import Any


def semantic_installed() -> bool:
    """Whether the ``semantic`` extra is installed.

    qdrant-client stands in for the whole extra: every semantic feature needs
    it, and the extra installs as a unit.
    """
    return find_spec("qdrant_client") is not None


def documents_installed() -> bool:
    """Whether the ``documents`` extra (PDF/Office text extraction) is
    installed. PyMuPDF stands in for it, as qdrant-client does for semantic."""
    return find_spec("pymupdf") is not None


def gateway_v1_url(settings: Any) -> str | None:
    """``EMBEDDING_GATEWAY_URL`` normalised to end in ``/v1``, or ``None``."""
    gateway = getattr(settings, "embedding_gateway_url", None)
    if not gateway:
        return None
    base = gateway.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def rerank_endpoint(settings: Any) -> str | None:
    """The rerank URL this deployment should POST to, or ``None`` if it has
    none configured.

    Two ways to get here, and they are not symmetric:

    * ``SEARCH_RERANK_URL`` is used **verbatim** — a full endpoint, path and
      all. Backends disagree on the path (Infinity ``/rerank``, vLLM
      ``/v1/rerank``, Cohere ``/v2/rerank``) and a wrong guess degrades to
      retrieval order rather than erroring, so guessing is worse than asking.
    * Otherwise it is derived from ``EMBEDDING_GATEWAY_URL``, which is a bare
      origin in some deployments and already ``/v1``-suffixed in others. That
      normalisation lives here rather than in the client so the client stays a
      plain Cohere-protocol client with no gateway knowledge.
    """
    url = getattr(settings, "search_rerank_url", None)
    if url:
        return url
    base = gateway_v1_url(settings)
    return f"{base}/rerank" if base else None


def rerank_available(settings: Any) -> bool:
    """Whether reranking can run at all on this deployment.

    The capability gate the request parameter is checked against, and what
    ``/api/v1/status`` advertises — so a caller can discover the feature instead
    of probing it and eating an error.
    """
    return bool(
        getattr(settings, "search_rerank_enabled", False) and rerank_endpoint(settings)
    )
