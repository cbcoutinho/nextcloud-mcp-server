"""OpenAI-compatible embedding provider targeting the Astrolabe Cloud embedding
gateway (design §10.2).

Active only when ``EMBEDDING_PROVIDER=gateway``. Registered *manually* in
``providers/registry.py`` — never part of the autodetect chain — so self-hosters
who don't opt in are unaffected.

**Auth model.** The MCP server is an OIDC *client* in the gateway's own
machine-to-machine realm — a realm *parallel to, and distinct from*, the tenant
realm the MCP server already serves as a client (Nextcloud user_oidc). It
obtains a ``client_credentials`` token and presents it as a Bearer; the gateway
maps the token's client-id → the tenant's underlying provider API key. This
mirrors the control-plane CLI's ``fetch_m2m_token`` pattern
(astrolabe-cloud-website ``services/control-plane/.../cli/_common.py``). When no
M2M creds are configured the client calls the gateway unauthenticated — matching
the gateway's current (not-yet-authenticated) state.

The gateway speaks the OpenAI ``/v1/embeddings`` wire format and routes by model
name (e.g. ``mistral-embed`` → Mistral for the MVP). Embeddings-only: ``generate``
is disabled (inherited ``NotImplementedError``).
"""

from __future__ import annotations

import logging
import time

import anyio
import httpx

from ..providers.openai import OpenAIProvider

logger = logging.getLogger(__name__)

# Refresh the cached token this many seconds before its stated expiry, so a
# token never expires mid-flight (matches AstrolabeClient / CP CLI behavior).
_EARLY_REFRESH_SECONDS = 60

# Non-secret placeholder for AsyncOpenAI, which rejects an empty key. In
# unauthenticated mode the gateway ignores the bearer; when a token provider is
# configured, the real M2M token replaces this before each request.
_UNAUTHENTICATED_PLACEHOLDER = "unauthenticated"


class GatewayTokenProvider:
    """Caches a gateway M2M access token via the ``client_credentials`` grant.

    HTTP Basic client auth + form-encoded grant, mirroring the website's
    ``fetch_m2m_token``. Tokens are cached until ``_EARLY_REFRESH_SECONDS``
    before expiry.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        timeout: float = 10.0,
    ):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.timeout = timeout
        self._cache: tuple[str, float] | None = None  # (token, expires_at)
        # Serialises the check-then-fetch cycle so concurrent embed calls don't
        # each issue a token request (and silently discard all-but-one token).
        # Lazy-init: anyio primitives must not be created at import time (trio).
        self._lock: anyio.Lock | None = None

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            # Re-check inside the lock: a concurrent caller may have just
            # refreshed the cache while we waited to acquire it.
            if (
                self._cache is not None
                and not force_refresh
                and time.time() < self._cache[1]
            ):
                return self._cache[0]

            data = {"grant_type": "client_credentials"}
            if self.scope:
                data["scope"] = self.scope

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=5.0)
            ) as client:
                resp = await client.post(
                    self.token_url,
                    data=data,
                    auth=(self.client_id, self.client_secret),
                )
                resp.raise_for_status()
                body = resp.json()

            expires_in = body.get("expires_in", 3600)
            self._cache = (
                body["access_token"],
                time.time() + expires_in - _EARLY_REFRESH_SECONDS,
            )
            logger.info(
                "Obtained embedding-gateway M2M token (expires in %ss)", expires_in
            )
            return self._cache[0]


class GatewayProvider(OpenAIProvider):
    """Embeddings-only OpenAI-compatible provider pointed at the gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        embedding_model: str,
        token_provider: GatewayTokenProvider | None = None,
        timeout: float = 120.0,
    ):
        # The gateway exposes its OpenAI-compatible API under the /v1 base path
        # (/v1/embeddings, /v1/models). Callers configure EMBEDDING_GATEWAY_URL
        # as a bare origin (scheme://host:port) — the deployment's Service URL —
        # so we append /v1 here. This base path is then used uniformly: the
        # OpenAI SDK posts embeds to {base_url}/embeddings and _detect_dimension
        # GETs {base_url}/models, both correctly landing under /v1. Idempotent —
        # a URL already ending in /v1 (or /v1/) is left as-is.
        normalized_base_url = base_url.rstrip("/")
        if not normalized_base_url.endswith("/v1"):
            normalized_base_url = f"{normalized_base_url}/v1"
        # AsyncOpenAI rejects an empty key; use a non-secret placeholder when
        # the gateway is unauthenticated. When a token provider is configured,
        # the real Bearer is set on the client before each request. The bare
        # suppression marker below silences the hard-coded-credential hotspot —
        # this is a public placeholder string, not a secret.
        super().__init__(
            api_key=_UNAUTHENTICATED_PLACEHOLDER,  # NOSONAR
            base_url=normalized_base_url,
            embedding_model=embedding_model,
            generation_model=None,  # gateway never generates
            timeout=timeout,
        )
        self._token_provider = token_provider
        logger.info(
            "Initialized gateway embedding provider: base_url=%s, model=%s, auth=%s",
            normalized_base_url,
            embedding_model,
            "oidc-m2m" if token_provider else "none",
        )

    async def _ensure_bearer(self) -> None:
        """Refresh the OIDC M2M token onto the OpenAI client (no-op when
        unauthenticated). AsyncOpenAI reads ``api_key`` per request to build
        the Authorization header, so updating it here applies to the next call.
        """
        if self._token_provider is not None:
            self.client.api_key = await self._token_provider.get_token()

    async def _detect_dimension(self) -> None:
        """Resolve the embedding dimension from the gateway's ``GET /v1/models``
        before the first embed.

        Qdrant collection init needs the vector size at startup (it calls
        ``get_dimension()`` before any ``embed()``); the vector-sync bootstrap
        invokes this hook first (``vector/qdrant_client.py`` —
        ``hasattr(provider, "_detect_dimension")``). The gateway is the
        authority on the dimensions of the models it serves, so we read it from
        there rather than hardcoding (``mistral-embed`` isn't an OpenAI model,
        so the OpenAI-wire base class can't know its size statically).

        Best-effort: any failure (old gateway without /v1/models, model absent,
        network) leaves ``_dimension`` unset so the inherited lazy
        detect-on-first-embed path still applies. Never raises.
        """
        if self._dimension is not None:
            return  # already known (e.g. an OpenAI model in the static map)

        # ``models`` is a sibling of ``embeddings`` under the gateway's base —
        # derive it from the same base_url the OpenAI client uses for embeds so
        # the two stay consistent (str(base_url) has a trailing slash).
        models_url = str(self.client.base_url).rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if self._token_provider is not None:
            headers["Authorization"] = (
                f"Bearer {await self._token_provider.get_token()}"
            )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0)
            ) as client:
                resp = await client.get(models_url, headers=headers)
                resp.raise_for_status()
                catalog = resp.json().get("data", [])
            for entry in catalog:
                if entry.get("id") == self.embedding_model:
                    dim = entry.get("dimension")
                    if isinstance(dim, int):
                        self._dimension = dim
                        logger.info(
                            "Resolved embedding dimension %d for model %s via "
                            "gateway /v1/models",
                            dim,
                            self.embedding_model,
                        )
                        return
            logger.warning(
                "Gateway /v1/models reported no dimension for model %s; "
                "falling back to lazy detection on first embed",
                self.embedding_model,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
            logger.warning(
                "Could not fetch model dimensions from gateway %s: %s; "
                "falling back to lazy detection on first embed",
                models_url,
                exc,
            )

    # Bearer-refresh override topology. OpenAIProvider routes embed_batch(),
    # embed_with_usage() and embed_batch_with_usage() all through
    # embed_batch_with_usage(); only embed() (single) is self-contained. So we
    # override exactly two methods to refresh the bearer exactly once on every
    # path: embed() (its own entrypoint) and embed_batch_with_usage() (the
    # shared funnel for the other three). Overriding embed_batch() as well would
    # double-call _ensure_bearer() (override → super().embed_batch() →
    # self.embed_batch_with_usage() → override again).

    async def embed(self, text: str) -> list[float]:
        await self._ensure_bearer()
        return await super().embed(text)

    async def embed_batch_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        await self._ensure_bearer()
        return await super().embed_batch_with_usage(texts)
