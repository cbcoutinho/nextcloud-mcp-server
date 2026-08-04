"""The rerank model default must carry a ``<provider>/`` prefix (Deck #983).

The gateway routes ``/v1/rerank`` by splitting the model id on the FIRST slash and
using the leading segment to pick a backend. A bare ``BAAI/bge-reranker-v2-m3``
therefore asks for a provider named ``BAAI``, which does not exist, and the gateway
returns 503.

What makes this worth a dedicated guard rather than trusting review: the failure is
NOT loud at the search surface. ``search/rerank.py`` catches ``RerankError`` and
degrades to retrieval order with ``reranked: false``, so a de-namespaced default
presents as "reranking does nothing" — a silent quality regression — rather than as a
misconfiguration. That is exactly what happened when the ``vllm/*`` -> ``local/*``
rename (Deck #931) updated the gateway and the models pact but not this default; it
survived from then until the gateway's own provider-verification job went red.

Pinned on the DEFAULT specifically, not on an operator-supplied value: an operator
naming a model the gateway does not serve is their business, but the value we ship has
to work against the gateway we ship alongside.
"""

import pytest

from nextcloud_mcp_server.config import _DEFAULTS, Settings

pytestmark = pytest.mark.unit

_FIELD = "search_rerank_model"


def _default() -> str:
    return getattr(Settings, _FIELD)


def test_the_two_declarations_of_the_default_agree() -> None:
    """The default is written twice — the ``_DEFAULTS`` dict and the dataclass field.
    They drifted apart is not the failure mode here (both were wrong together), but a
    fix applied to only one would be, so pin them equal."""
    assert _DEFAULTS[_FIELD] == _default()


def test_default_rerank_model_is_namespaced() -> None:
    default = _default()
    provider, slash, model = default.partition("/")
    # Mirrors the gateway's own `_split_model` (`model.partition("/")`): the leading
    # segment is the provider key, everything after the first slash is the upstream id.
    assert slash, f"rerank model default {default!r} has no '<provider>/' prefix"
    assert provider, f"rerank model default {default!r} has an empty provider segment"
    assert model, f"rerank model default {default!r} has an empty model segment"


def test_default_rerank_model_names_a_provider_the_gateway_can_serve() -> None:
    """A namespace alone isn't enough — `BAAI/bge-reranker-v2-m3` is "namespaced" by
    the split rule while naming a provider that will never exist. Pin the leading
    segment to a gateway rerank provider namespace."""
    provider = _default().partition("/")[0]
    # The gateway's rerank providers: `local` (self-hosted cross-encoder), plus the
    # hosted `openrouter` and `bedrock` namespaces.
    assert provider in {"local", "openrouter", "bedrock"}, (
        f"rerank model default names provider {provider!r}, which is not a gateway "
        "rerank provider namespace"
    )
