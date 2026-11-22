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

Alternatively ``EMBEDDING_GATEWAY_TOKEN_FILE`` points at a Kubernetes projected
ServiceAccount token (audience ``astrolabe-gateway``) that the gateway checks
via TokenReview; :class:`FileTokenProvider` re-reads it per request because
kubelet rotates it.

The gateway speaks the OpenAI ``/v1/embeddings`` wire format and routes by model
name (e.g. ``mistral-embed`` → Mistral for the MVP). Embeddings-only: ``generate``
is disabled (inherited ``NotImplementedError``).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import anyio
import httpx2

from ..config import Settings
from ..providers.openai import OpenAIProvider
from .base import ImageInput, ImageInputLimits, ImageInputRejected

logger = logging.getLogger(__name__)

# Refresh the cached token this many seconds before its stated expiry, so a
# token never expires mid-flight (matches AstrolabeClient / CP CLI behavior).
_EARLY_REFRESH_SECONDS = 60

# Non-secret placeholder for AsyncOpenAI, which rejects an empty key. In
# unauthenticated mode the gateway ignores the bearer; when a token provider is
# configured, the real M2M token replaces this before each request.
_UNAUTHENTICATED_PLACEHOLDER = "unauthenticated"

# Gateway image caps (astrolabe-cloud-website#1247): 5 MiB decoded per image,
# 16 images per request. Fixed by the gateway, not advertised per model.
_GATEWAY_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_GATEWAY_IMAGE_MAX_BATCH = 16

# 422 ``detail.code`` values that name an image as unusable for this model:
# retrying the same bytes cannot succeed (ADR-042 §5). ``image_too_large`` and
# ``too_many_images`` are deliberately absent — they mean the caller skipped
# downscaling or chunking, a bug to fix, not an image to skip.
_PERMANENT_IMAGE_CODES = frozenset({"invalid_image", "unsupported_mime_type"})


class GatewayTokenProvider:
    """Caches a gateway M2M access token via the ``client_credentials`` grant.

    HTTP Basic client auth + form-encoded grant, mirroring the website's
    ``fetch_m2m_token``. Tokens are cached until ``_EARLY_REFRESH_SECONDS``
    before expiry.
    """

    # Log label for the configured auth mode (see GatewayProvider's init log).
    auth_label = "oidc-m2m"

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

            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(self.timeout, connect=5.0)
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


class FileTokenProvider:
    """Reads the gateway bearer from a file on every call.

    For a Kubernetes projected ServiceAccount token (audience
    ``astrolabe-gateway``): kubelet rotates the file in place, so it is re-read
    each time rather than cached. A small local read per gateway request.
    """

    # Log label for the configured auth mode (see GatewayProvider's init log).
    auth_label = "token-file"

    def __init__(self, path: str):
        self.path = path

    async def get_token(self, *, force_refresh: bool = False) -> str:  # NOSONAR
        """Return the file's current contents, stripped.

        ``force_refresh`` is accepted for interface parity with
        :class:`GatewayTokenProvider` (hence the S1172 suppression); every call
        re-reads, so it has no effect. The per-call read is deliberate: do not
        cache, kubelet rotates the file.

        Raises:
            OSError: the file is missing or unreadable (logged with the path).
            RuntimeError: the file is empty or whitespace-only, which would
                otherwise send a bare ``Bearer `` header and surface only as an
                opaque gateway 401.
        """
        try:
            token = (await anyio.Path(self.path).read_text()).strip()
        except OSError:
            logger.exception(
                "Cannot read embedding-gateway token file %s "
                "(EMBEDDING_GATEWAY_TOKEN_FILE); is the projected volume mounted?",
                self.path,
            )
            raise
        if not token:
            raise RuntimeError(f"Embedding-gateway token file {self.path} is empty")
        return token


# Anything with ``async get_token(*, force_refresh=False) -> str``.
TokenProvider = GatewayTokenProvider | FileTokenProvider


class GatewayProvider(OpenAIProvider):
    """Embeddings-only OpenAI-compatible provider pointed at the gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        embedding_model: str,
        token_provider: TokenProvider | None = None,
        timeout: float = 120.0,
        embedding_dimensions: int | None = None,
        image_embedding_model: str | None = None,
        image_embedding_dimensions: int | None = None,
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
            timeout=timeout,
            embedding_dimensions=embedding_dimensions,
        )
        self._token_provider = token_provider
        self._timeout = timeout
        self.image_embedding_model = image_embedding_model
        self._requested_image_dimensions = image_embedding_dimensions
        self._image_limits: ImageInputLimits | None = None
        self._image_width: int | None = None
        logger.info(
            "Initialized gateway embedding provider: base_url=%s, model=%s, "
            "auth=%s, requested_dimensions=%s",
            normalized_base_url,
            embedding_model,
            token_provider.auth_label if token_provider else "none",
            embedding_dimensions,
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

        if self._requested_dimensions is not None:
            # The catalogue reports each model's FULL width, so it cannot answer
            # for a truncated request. Fall through to the inherited probe,
            # which reads the width off an actual embedding — and therefore also
            # catches a gateway that accepted `dimensions` and ignored it
            # (measured 2026-08-15: it does, silently, on every backend path).
            await super()._detect_dimension()
            return

        try:
            for entry in await self._fetch_models():
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
        except Exception as exc:  # noqa: BLE001
            # Best-effort, never fatal.
            logger.warning(
                "Could not fetch model dimensions from gateway %s: %s; "
                "falling back to lazy detection on first embed",
                self._url("models"),
                exc,
            )

    def _url(self, path: str) -> str:
        # Siblings of ``embeddings`` under the base the OpenAI client uses, so
        # the two stay consistent (str(base_url) has a trailing slash).
        return str(self.client.base_url).rstrip("/") + "/" + path

    async def _auth_headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def _fetch_models(self) -> list[dict[str, Any]]:
        """``GET /v1/models`` → its ``data`` list. Raises on any failure."""
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(10.0, connect=5.0)
        ) as client:
            resp = await client.get(
                self._url("models"), headers=await self._auth_headers()
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    # ------------------------------------------------------------------
    # Image embeddings (ADR-042 §8)
    # ------------------------------------------------------------------

    async def confirm_image_embeddings(self) -> bool:
        """Confirm from the gateway's model advertisement.

        Fails closed: an unreachable catalogue, a model without ``"image"`` in
        ``input_modalities``, or an unsupported width leaves the feature off.
        Enabling on configuration alone would write image points the gateway
        then refuses to produce.
        """
        self._image_confirmed = False
        model = self.image_embedding_model
        if model is None:
            return False
        try:
            catalog = await self._fetch_models()
        except Exception:  # noqa: BLE001 — any failure means "not confirmed"
            logger.error(
                "Could not read gateway /v1/models to confirm image model %s; "
                "image embeddings stay off",
                model,
                exc_info=True,
            )
            return False
        entry = next((e for e in catalog if e.get("id") == model), None)
        if entry is None or "image" not in (entry.get("input_modalities") or []):
            logger.error(
                "Gateway does not advertise %s as accepting images "
                "(input_modalities); image embeddings stay off",
                model,
            )
            return False
        requested = self._requested_image_dimensions
        if requested is not None and requested not in (entry.get("mrl_dims") or []):
            logger.error(
                "IMAGE_EMBEDDING_DIMENSIONS=%s is not among %s's mrl_dims %s; "
                "image embeddings stay off",
                requested,
                model,
                entry.get("mrl_dims"),
            )
            return False
        width = requested or entry.get("dimension")
        if not isinstance(width, int):
            # ``dimension`` is absent when the gateway's live probe failed.
            logger.error(
                "Gateway reported no dimension for %s; set "
                "IMAGE_EMBEDDING_DIMENSIONS or retry once the gateway is healthy",
                model,
            )
            return False
        mime_types = frozenset(entry.get("image_mime_types") or ())
        if not mime_types:
            # An empty set would reject every image pre-flight: enabled in
            # name, useless in fact.
            logger.error(
                "Gateway advertises %s as image-capable but lists no "
                "image_mime_types; image embeddings stay off",
                model,
            )
            return False
        self._image_width = width
        # Expected, not learned: the first vector is checked against the width
        # the collection will be sized from (get_image_dimension), so a gateway
        # whose default width differs from its advertisement fails loudly.
        self._image_dimension = width
        self._image_limits = ImageInputLimits(
            mime_types=mime_types,
            max_bytes=_GATEWAY_IMAGE_MAX_BYTES,
            max_batch=_GATEWAY_IMAGE_MAX_BATCH,
        )
        self._image_confirmed = True
        return True

    @property
    def image_input_limits(self) -> ImageInputLimits | None:
        return self._image_limits

    def get_image_dimension(self) -> int:
        self._require_image_support()
        if self._image_width is None:
            raise RuntimeError("Image width unknown although confirmation succeeded")
        return self._image_width

    async def _embed_into_image_space(
        self, items: list[str | dict[str, str]]
    ) -> list[list[float]]:
        """POST ``/v1/embeddings`` against the image model, floats on the wire.

        Bypasses the OpenAI SDK: its ``input`` type has no image items, and the
        422 ``detail.code`` the classification needs is easier to read raw.
        """
        body: dict[str, Any] = {
            "model": self.image_embedding_model,
            "input": items,
            "encoding_format": "float",
        }
        if self._requested_image_dimensions is not None:
            body["dimensions"] = self._requested_image_dimensions
        # ponytail: one client per request; image ingest is one call per
        # document, so pooling only matters if that rate grows.
        async with httpx2.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url("embeddings"), json=body, headers=await self._auth_headers()
            )
        if resp.status_code == 422:
            _raise_for_image_rejection(resp)
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def _embed_image_chunk(self, images: list[ImageInput]) -> list[list[float]]:
        return await self._embed_into_image_space(
            [
                {
                    "type": "image",
                    "data": base64.b64encode(img.data).decode(),
                    "mime_type": img.mime_type,
                }
                for img in images
            ]
        )

    async def embed_for_image_space(self, text: str) -> list[float]:
        self._require_image_support()
        # A plain string sent to the image model id is embedded into image space.
        vector = (await self._embed_into_image_space([text]))[0]
        self._record_image_dimension(len(vector))
        return vector

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


def _raise_for_image_rejection(resp: httpx2.Response) -> None:
    """Turn a dict-shaped image 422 into :class:`ImageInputRejected`.

    Image 422s carry ``{"detail": {"code", "index", "message"}}``; every other
    422 (pydantic validation) is list-shaped and falls through to
    ``raise_for_status`` — retryable, because nothing names the input.
    """
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return  # not JSON: nothing names the input
    if not (isinstance(detail, dict) and "code" in detail):
        return
    code = detail["code"]
    message = detail.get("message") or code
    if code in _PERMANENT_IMAGE_CODES:
        index = detail.get("index")
        raise ImageInputRejected(
            message, index=index if isinstance(index, int) else None
        )
    # image_too_large / too_many_images: our own downscaling or chunking is
    # broken. Loud, but retryable — a fix and redeploy recovers the backlog.
    logger.error(
        "Gateway rejected an image request as a client bug: %s (%s)", code, message
    )


def build_gateway_token_provider(settings: Settings) -> TokenProvider | None:
    """Build the gateway bearer source from settings, or ``None`` when neither a
    token file nor a client-id is configured (unauthenticated gateway).

    ``EMBEDDING_GATEWAY_TOKEN_FILE`` (projected SA token) wins; Settings
    rejects it combined with the M2M triple, so at most one is set.

    Lives beside ``GatewayTokenProvider`` because every gateway sub-client
    needs it (embeddings, sync OCR, batch OCR, rerank and NER), so the
    all-or-nothing M2M-triple validation has exactly one home.
    ``document_processors.ocr`` re-exports it under its historical private name.
    """
    if settings.embedding_gateway_token_file:
        return FileTokenProvider(settings.embedding_gateway_token_file)
    if not settings.embedding_gateway_client_id:
        return None

    # Explicit (not assert -- assert is stripped under `python -O`): the M2M
    # triple is all-or-nothing.
    if not settings.embedding_gateway_token_url:
        raise ValueError(
            "EMBEDDING_GATEWAY_TOKEN_URL is required when "
            "EMBEDDING_GATEWAY_CLIENT_ID is set"
        )
    if not settings.embedding_gateway_client_secret:
        raise ValueError(
            "EMBEDDING_GATEWAY_CLIENT_SECRET is required when "
            "EMBEDDING_GATEWAY_CLIENT_ID is set"
        )
    return GatewayTokenProvider(
        token_url=settings.embedding_gateway_token_url,
        client_id=settings.embedding_gateway_client_id,
        client_secret=settings.embedding_gateway_client_secret,
        scope=settings.embedding_gateway_scope,
    )
