import atexit
import logging
import logging.config
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynaconf import Dynaconf, Validator

logger = logging.getLogger(__name__)

# Sentinel for "key not in dynaconf at all" vs "explicitly set to None".
_UNSET = object()

# Built-in defaults — declared in Python so env vars work without any settings
# file being present (e.g., `uvx` / `pip install` deployments). Mirrors the
# [default] section that used to live in settings.toml. Keys set here are
# "known" to dynaconf, which is required because we run with
# ignore_unknown_envvars=True. See ADR-024/025.
_DEFAULTS: dict[str, Any] = {
    # Deployment mode (ADR-021)
    "mcp_deployment_mode": None,
    # Nextcloud core
    "nextcloud_host": None,
    "nextcloud_username": None,
    "nextcloud_password": None,
    "nextcloud_app_password": None,
    "nextcloud_verify_ssl": True,
    "nextcloud_ca_bundle": None,
    "nextcloud_http_keepalive": True,
    "nextcloud_mcp_server_url": None,
    "nextcloud_resource_uri": None,
    "nextcloud_public_issuer_url": None,
    "cookie_secure": None,
    # OAuth/OIDC
    "oidc_discovery_url": None,
    # Keys must uppercase to the env var dynaconf reads (ignore_unknown_envvars):
    # NEXTCLOUD_OIDC_TOKEN_TYPE / NEXTCLOUD_OIDC_SCOPES, matching _field_map.
    "nextcloud_oidc_token_type": "Bearer",
    "nextcloud_oidc_scopes": "",
    "port": 8000,
    "nextcloud_oidc_client_id": None,
    "nextcloud_oidc_client_secret": None,
    "oidc_issuer": None,
    "jwks_uri": None,
    "introspection_uri": None,
    "userinfo_uri": None,
    "oidc_resource_server_id": None,
    # M2M / DCR / management — these MUST be declared here (lowercase) so dynaconf
    # reads the matching UPPERCASE env var; with ignore_unknown_envvars=True an
    # undeclared key is silently dropped (env value ignored), so they are read
    # via cfg("ENV_NAME"). Defaults mirror the prior os.getenv(..., default).
    "mcp_server_client_id": None,
    "mcp_server_client_secret": None,
    "allowed_mcp_clients": "",
    "allowed_mgmt_client": "",
    "enable_dcr": False,
    # Container-runtime / webhook self-URL overrides (local-dev docker-compose).
    "docker_container": False,
    "nextcloud_mcp_service_name": "mcp",
    "nextcloud_mcp_port": 8000,
    # Mode flags
    # NOTE: `enable_multi_user_basic_auth` and `enable_login_flow` are
    # intentionally absent — they are derived from MCP_DEPLOYMENT_MODE in
    # Settings.__post_init__ (ADR-022) and not read from the dynaconf store.
    "enable_semantic_search": False,
    "enable_background_operations": False,
    "vector_sync_enabled": False,
    "enable_offline_access": False,
    # Token storage
    "token_encryption_key": None,
    # None = ephemeral per-process tempfile (see get_token_db_path()).
    # Set TOKEN_STORAGE_DB to persist tokens across restarts.
    "token_storage_db": None,
    # Centralized backend (any SQLAlchemy URL). Wins over TOKEN_STORAGE_DB
    # when set. Use postgresql+asyncpg://user:pw@host/db for HA k8s
    # deployments so pods can be stateless. See ADR-026.
    "database_url": None,
    # TLS for the Postgres backend (mirror NEXTCLOUD_VERIFY_SSL pattern).
    # Default is None — preserve asyncpg's `prefer` mode so cluster-local
    # Postgres without TLS works out of the box. Set to True for full
    # verification or False to silence cert errors against self-signed
    # homelab servers. DATABASE_CA_BUNDLE points at a private-CA PEM.
    "database_verify_ssl": None,
    "database_ca_bundle": None,
    # Postgres connection pool sizing (ADR-026 → "Concurrency model and
    # pool sizing"). Per-pod defaults to 2 + 5 overflow = 7 max
    # connections. asyncpg connections are single-flight, so the pool
    # only needs to cover typical multi-user MCP burst — not every
    # potential in-flight tool call. Tune up with DATABASE_POOL_SIZE /
    # DATABASE_MAX_OVERFLOW for high-traffic prod fleets.
    "database_pool_size": 2,
    "database_max_overflow": 5,
    # Webhook delivery authentication (ADR-010): when set, registrations
    # tell NC to add `Authorization: Bearer <secret>` to webhook deliveries
    # and the receiver rejects unauthenticated requests.
    "webhook_secret": None,
    # Internal URL override for webhook registration; wins over
    # NEXTCLOUD_MCP_SERVER_URL when set (e.g. split internal/external URLs).
    "webhook_internal_url": None,
    # Vector sync
    "vector_sync_scan_interval": 300,
    "vector_sync_processor_workers": 3,
    "vector_sync_queue_max_size": 10000,
    "vector_sync_metrics_refresh_interval": 20,
    "vector_sync_user_poll_interval": 60,
    "health_ready_refresh_interval": 15,
    # Orphan-sweep at Pod startup (card #101). When True, delete any
    # placeholders carrying a different / absent ``instance_id`` before
    # the scanner's first cycle, so a Pod restart mid-batch doesn't
    # leave work stuck behind the 5x-scan-interval staleness gate.
    # Escape hatch only — leave on by default.
    "vector_sync_orphan_sweep_enabled": True,
    # System tag that marks files for vector indexing. The scanner indexes
    # files carrying this tag; verify-on-read gates results on current
    # membership of this tag (ADR-019).
    "vector_sync_pdf_tag": "vector-index",
    # Verify-on-read concurrency cap (ADR-019)
    "verification_concurrency": 20,
    # Qdrant
    "qdrant_url": None,
    "qdrant_location": None,
    "qdrant_api_key": None,
    "qdrant_collection": "nextcloud_content",
    # Ollama
    "ollama_base_url": None,
    "ollama_embedding_model": "nomic-embed-text",
    "ollama_generation_model": None,
    "ollama_verify_ssl": True,
    # OpenAI
    "openai_api_key": None,
    "openai_base_url": None,
    "openai_embedding_model": "text-embedding-3-small",
    "openai_generation_model": None,
    # Bedrock (AWS)
    "aws_region": None,
    "aws_access_key_id": None,
    "aws_secret_access_key": None,
    "bedrock_embedding_model": None,
    "bedrock_generation_model": None,
    # Mistral
    "mistral_api_key": None,
    "mistral_embedding_model": "mistral-embed",
    "mistral_base_url": None,
    # Simple (fallback) embedding dimension
    "simple_embedding_dimension": 384,
    # Document chunking
    "document_chunk_size": 2048,
    "document_chunk_overlap": 200,
    # Page-aware chunking for paginated docs (PDFs): split on page boundaries
    # first so no chunk spans a page (exact page_number, clean snippets, and
    # predictable ~1 chunk/page when chunk_size >= the largest page).
    "document_chunk_page_aware": True,
    # PDF parse isolation (OOM guard)
    "document_pdf_graphics_limit": 1000,
    "document_parse_timeout_seconds": 120.0,
    "document_parse_mem_limit_mb": 1536,
    # Pre-parse size cap (MB): PDFs larger than this fail fast with reason
    # "oversize" instead of burning the OCR timeout to 0 chars on a pathological
    # file. 0 disables the guard.
    "document_max_pdf_size_mb": 50.0,
    # Tier-0 classifier (records classification metrics on the tiered path)
    "document_classify_enabled": True,
    # Tiered PDF pipeline: pypdfium2 is the default/only hot-path extractor;
    # "pymupdf" is a deprecated rollback escape hatch. OCR (tier-3) is the only
    # escalation target and is off by default (no provider wired yet).
    "document_tier1_engine": "pypdfium2",
    "document_ocr_enabled": False,
    # OCR backend: "auto" picks gateway (if EMBEDDING_GATEWAY_URL) else mistral
    # (if MISTRAL_API_KEY); "gateway"/"mistral" force one; "none" disables. The
    # gateway routes on the model's "<provider>/" prefix, so it serves Mistral,
    # surya (in-cluster GPU over the tailnet), etc. — one configurable OCR tier.
    "document_ocr_provider": "auto",
    # Provider-namespaced OCR model id (gateway routes on the prefix; the direct
    # mistral backend strips it). e.g. "mistral/mistral-ocr-latest" (Mistral) or
    # "surya/surya-ocr-2" (surya via the gateway) — never hard-coded, only this
    # default.
    "document_ocr_model": "mistral/mistral-ocr-latest",
    # OCR escalation triggers (tier-0). A page is OCR-worthy when its text is
    # near-empty (< min_page_chars) OR low-quality (< min_text_quality) OR (when
    # detect_scanned) mostly a raster image; a doc escalates when the OCR-worthy
    # page fraction reaches page_fraction. Calibrate min_text_quality from the
    # astrolabe_document_text_quality histogram per tenant.
    "document_ocr_min_text_quality": 0.5,
    "document_ocr_page_fraction": 0.5,
    "document_ocr_min_page_chars": 16,
    "document_ocr_detect_scanned": True,
    # Tier-0 glyph-corruption trigger. When the fast (pypdfium2) extraction's
    # doc-level C0-control-char ratio exceeds this, the text layer is treated as
    # glyph-corrupt (a broken /ToUnicode mapping leaking raw glyph codes) and the
    # doc escalates fast->structured (pymupdf re-extracts it correctly -- no OCR).
    # 0 disables. Clean docs sit ~0; affected PDFs measured ~1-11% in testing.
    "document_glyph_corruption_ratio": 0.02,
    # OCR backend request timeout (seconds). Slow scanned newspapers can take
    # 20-60s; raise/lower per tenant. Configurable so a tenant isn't stuck with
    # the 180s default when its gateway has its own shorter ceiling.
    "document_ocr_timeout_seconds": 180.0,
    # OCR execution mode (Deck #332). "sync" (default) transcribes inline via the
    # backend's synchronous path. "batch" routes to the gateway's async Batch OCR
    # job (~50% cheaper, minutes-hours latency) for large-corpus backfill — opt-in
    # and gateway-routed. It requires EMBEDDING_GATEWAY_URL (rejected at startup
    # otherwise, see __post_init__) and the per-tier procrastinate path; the
    # inline/memory pool can't defer a poll, so batch raises there rather than
    # silently downgrading to sync.
    "document_ocr_mode": "sync",
    # Seconds between batch-job polls (the procrastinate re-enqueue delay). Each
    # poll re-runs the tier; keep it well above a few seconds.
    "document_ocr_batch_poll_seconds": 120,
    # Hard deadline (seconds from submit) after which a still-pending batch job is
    # abandoned and the document marked parse-failed (timeout). Matches the
    # gateway's 24h Batch timeout default.
    "document_ocr_batch_max_wait_seconds": 86400,
    # Observability
    "metrics_enabled": True,
    "metrics_port": 9090,
    "otel_exporter_otlp_endpoint": None,
    "otel_exporter_verify_ssl": False,
    "otel_service_name": "nextcloud-mcp-server",
    "otel_traces_sampler": "always_on",
    "otel_traces_sampler_arg": 1.0,
    "log_format": "text",
    "log_level": "INFO",
    "log_include_trace_context": True,
    # Document processing
    "enable_document_processing": False,
    "document_processor": "unstructured",
    "enable_unstructured": False,
    "unstructured_api_url": "http://unstructured:8000",
    "unstructured_timeout": 120,
    "unstructured_strategy": "auto",
    "unstructured_languages": "eng,deu",
    "progress_interval": 10,
    "enable_tesseract": False,
    "tesseract_cmd": None,
    "tesseract_lang": "eng",
    "enable_pymupdf": True,
    "pymupdf_extract_images": True,
    "pymupdf_image_dir": None,
    "enable_custom_processor": False,
    "custom_processor_url": None,
    "custom_processor_types": "application/pdf",
    "custom_processor_name": "custom",
    "custom_processor_api_key": None,
    "custom_processor_timeout": 60,
    # Tag-based file exclusion (issue #710): comma-separated list of
    # Nextcloud system tag names. Files/folders carrying any of these tags
    # are hidden from WebDAV MCP tools. Empty = feature off.
    "excluded_tags": "",
    # MCP decomposition hook points (design §10). Every default reproduces
    # the current monolithic behavior; self-hosters who set none are
    # unaffected. See docs/architecture/mcp-decomposition.md (sibling repo).
    "embedding_provider": "autodetect",  # autodetect | gateway
    # Search mode (ADR-030). ``hybrid`` (default) generates + queries dense
    # embeddings AND BM25 sparse vectors. ``keyword`` skips dense entirely:
    # BM25 sparse only, so fully airgapped deployments need no embedding
    # endpoint. ``keyword`` still requires vector sync enabled (it uses Qdrant).
    "search_mode": "hybrid",  # hybrid (dense+sparse) | keyword (BM25 sparse only)
    # Ingest queue backend (Deck #183). None → ``memory`` (the in-process anyio
    # queue): procrastinate is strictly opt-in, even on a Postgres DATABASE_URL.
    # Set ``postgres`` explicitly to split ingest into a procrastinate worker;
    # that requires a PostgreSQL DATABASE_URL.
    "ingest_queue": None,  # memory | postgres
    # Process role for the per-tenant two-pod model (Deck #183). ``api`` runs the
    # MCP/query server + scanner (defers jobs); the ``worker`` role is the
    # `nextcloud-mcp-server worker` process that drains the queue. ``all`` keeps
    # the monolithic behaviour (API + in-process SQLite pool).
    "mcp_role": "all",  # api | worker | all
    # Reclaim an ingest job orphaned in ``doing`` by a crashed worker once its
    # worker heartbeat is this many seconds stale (Deck #183). Default is well
    # above the longest expected document; raise it for slow embedding backends.
    "ingest_stalled_job_seconds": 300,
    # Delete succeeded ingest jobs (keeps the queue table lean + the KEDA
    # queue-depth metric clean). Set false to retain succeeded rows for audit
    # (note: indexing success is also recorded in logs/metrics regardless).
    "ingest_delete_succeeded_jobs": True,
    # Whether the procrastinate worker uses LISTEN/NOTIFY for job pickup (Deck
    # #424). True (default) = near-instant wakeup via a long-lived LISTEN
    # connection. Set false to run POLL-ONLY when DATABASE_URL routes through a
    # transaction-mode connection pooler (PgBouncer transaction mode), which is
    # incompatible with LISTEN/NOTIFY — the LISTEN registration is dropped when
    # the backend returns to the pool. Poll-only trades a few seconds of pickup
    # latency (fetch_job_polling_interval) for pooler safety; job queries still
    # multiplex through the pooler. Snapshotted at worker startup; needs a
    # restart to change.
    "ingest_listen_notify": True,
    # Per-tier escalation on the procrastinate (postgres) ingest path (Deck
    # #323). When true, a document that a tier cannot parse well is requeued onto
    # the next tier's queue (fast -> structured -> ocr) via a native procrastinate
    # queue-hop. When false the ``fast`` tier is terminal -- reproduces the
    # pre-#323 behaviour where the cheap tier's output is indexed as-is. No effect
    # on the in-process ``memory`` backend, which keeps the inline escalation.
    # HOT: re-read per job (process_document_task), so it takes effect on the next
    # job -- unlike INGEST_TRANSIENT_MAX_ATTEMPTS, which is snapshotted at worker
    # startup and needs a restart.
    "ingest_escalation_enabled": True,
    # Global cap on SAME-tier retries for transient infra errors (doc fetch /
    # embed / Qdrant blips) on the procrastinate path. Parse-quality failures
    # escalate (one parse attempt per tier) and do NOT consume this budget; only
    # whitelisted transient exceptions retry in place. Shared across tiers because
    # a queue-hop cannot reset a per-tier counter (see TieredEscalationStrategy).
    # Snapshotted at worker startup (blueprint build); restart to change it.
    "ingest_transient_max_attempts": 5,
    # Delay (seconds) before a reclaimed stalled job is re-run. A stall is often
    # systemic (Qdrant/embedding outage), so reclaiming every crashed job at
    # now() would thundering-herd a recovering dependency every reclaim tick
    # (*/5min), bypassing TieredEscalationStrategy's per-job backoff. A small
    # fixed delay staggers the retry. 0 = immediate (legacy behaviour).
    "ingest_reclaim_retry_delay_seconds": 30,
    "collection_metadata_source": "qdrant",  # qdrant | api
    # CP base URL for COLLECTION_METADATA_SOURCE=api (e.g. http://control-plane).
    # Required only when the source is api.
    "collection_metadata_api_url": None,
    "embedding_gateway_url": None,  # required when embedding_provider=gateway
    # Provider-namespaced model the gateway serves, "<provider>/<model>"
    # (the gateway routes on the "/"-prefix; mistral/mistral-embed → Mistral
    # for the MVP). Only consulted when embedding_provider=gateway.
    "embedding_gateway_model": "mistral/mistral-embed",
    # Gateway auth: the MCP server is an OIDC *client* in the gateway's own
    # M2M realm (parallel to, and distinct from, the tenant realm it already
    # serves). It obtains a client-credentials token and the gateway maps the
    # client-id → the tenant's underlying provider API key. All four unset =
    # call the gateway unauthenticated (matches today's not-yet-authed gateway).
    "embedding_gateway_token_url": None,  # M2M token endpoint
    "embedding_gateway_client_id": None,
    "embedding_gateway_client_secret": None,
    "embedding_gateway_scope": None,  # e.g. astrolabe-embedding-gateway/embed
    "tenant_id": None,  # per-tenant identity (UUID form); see vector/payload_keys
    # Query-side ACL pre-filter (design §11). OFF by default: a Qdrant
    # `match any` on `acl_hash` excludes points missing the key, so enabling
    # this before a real ACL backfill would silently drop legacy results.
    # verify-on-read remains the correctness backstop regardless.
    "acl_prefilter_enabled": False,
    # Usage metering (Deck #67, control-plane usage-metering.md). OFF by
    # default so OSS self-hosters don't accrue a metering table or write
    # overhead; Astrolabe Cloud provisioning sets it true. When on, billable
    # ops record rows into the app-DB usage_events table (best-effort).
    "usage_metering_enabled": False,
}


def _resolve_settings_files() -> list[str]:
    """Find optional external settings files.

    Priority:
      1. NEXTCLOUD_MCP_SETTINGS_FILE env var (absolute or relative path).
         If set but the file does not exist, raise FileNotFoundError —
         silently falling back to defaults on a typo would be a footgun.
         .secrets.toml is looked for alongside the explicit file.
      2. Otherwise ./settings.toml in cwd (for docker / dev workflows),
         with .secrets.toml also looked for in cwd.

    Returns an empty list if nothing is configured — that's fine, defaults
    and env vars still apply.
    """
    files: list[str] = []
    # The ONLY legitimate os.environ read in the app: this runs BEFORE the
    # dynaconf instance is built (it computes the settings_files list passed to
    # Dynaconf), so it cannot go through dynaconf — you can't read the location
    # of the settings file from the settings file. This + the MCP_DEPLOYMENT_MODE
    # env_switcher are dynaconf's own bootstrap. All OTHER config is dynaconf-driven.
    explicit = os.environ.get("NEXTCLOUD_MCP_SETTINGS_FILE")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(
                f"NEXTCLOUD_MCP_SETTINGS_FILE points to a file that does "
                f"not exist: {explicit}"
            )
        files.append(str(p))
        secrets = p.parent / ".secrets.toml"
    else:
        cwd_settings = Path.cwd() / "settings.toml"
        if cwd_settings.exists():
            files.append(str(cwd_settings))
        secrets = Path.cwd() / ".secrets.toml"
    if secrets.exists():
        files.append(str(secrets))
    return files


# Dynaconf instance — env vars always win (12-factor). Settings files are
# optional; when absent the defaults above provide the full key schema so
# env vars still override correctly. See ADR-024/025 for architecture.
_dynaconf = Dynaconf(
    settings_files=_resolve_settings_files(),
    environments=True,
    envvar_prefix=False,
    env_switcher="MCP_DEPLOYMENT_MODE",
    ignore_unknown_envvars=True,
    load_dotenv=False,
    **_DEFAULTS,
    validators=[
        # Port ranges
        Validator("METRICS_PORT", gte=1, lte=65535),
        # Positive integers
        Validator("INGEST_STALLED_JOB_SECONDS", gte=1),
        Validator("INGEST_TRANSIENT_MAX_ATTEMPTS", gte=1),
        Validator("INGEST_RECLAIM_RETRY_DELAY_SECONDS", gte=0),
        Validator("VECTOR_SYNC_SCAN_INTERVAL", gte=1),
        Validator("VECTOR_SYNC_PROCESSOR_WORKERS", gte=1),
        Validator("VECTOR_SYNC_QUEUE_MAX_SIZE", gte=1),
        Validator("VECTOR_SYNC_METRICS_REFRESH_INTERVAL", gte=1),
        Validator("VECTOR_SYNC_USER_POLL_INTERVAL", gte=1),
        Validator("HEALTH_READY_REFRESH_INTERVAL", gte=1),
        Validator("PORT", gte=1, lte=65535),
        Validator("VERIFICATION_CONCURRENCY", gte=1),
        Validator("DOCUMENT_CHUNK_SIZE", gte=1),
        Validator("DOCUMENT_PARSE_TIMEOUT_SECONDS", gte=1),
        Validator("DOCUMENT_OCR_TIMEOUT_SECONDS", gte=1),
        # DOCUMENT_OCR_MODE is normalised + membership-checked in
        # Settings.__post_init__ via _enum_fields (case-insensitive, like
        # DOCUMENT_OCR_PROVIDER) — no strict dynaconf Validator here, so
        # "Batch"/"SYNC" normalise instead of erroring.
        # Poll cadence well above a few seconds (each poll re-runs the tier);
        # deadline at least one poll interval.
        Validator("DOCUMENT_OCR_BATCH_POLL_SECONDS", gte=5),
        Validator("DOCUMENT_OCR_BATCH_MAX_WAIT_SECONDS", gte=60),
        Validator("DOCUMENT_PARSE_MEM_LIMIT_MB", gte=128),
        # 0 disables the pre-parse PDF size cap; otherwise it must be positive.
        Validator("DOCUMENT_MAX_PDF_SIZE_MB", gte=0),
        # >=1: pymupdf4llm treats graphics_limit=0 as "no cap", which would
        # re-expose the OOM this guards against.
        Validator("DOCUMENT_PDF_GRAPHICS_LIMIT", gte=1),
        # OCR escalation thresholds: quality + page-fraction are [0, 1].
        Validator("DOCUMENT_OCR_MIN_TEXT_QUALITY", gte=0, lte=1),
        Validator("DOCUMENT_OCR_PAGE_FRACTION", gte=0, lte=1),
        Validator("DOCUMENT_OCR_MIN_PAGE_CHARS", gte=0),
        Validator("DOCUMENT_GLYPH_CORRUPTION_RATIO", gte=0, lte=1),
        # Non-negative
        Validator("DOCUMENT_CHUNK_OVERLAP", gte=0),
        # Non-empty strings
        Validator("VECTOR_SYNC_PDF_TAG", len_min=1),
        # WEBHOOK_SECRET is optional (None disables webhooks — GHSA-8vh3-g2qg-2h2c),
        # but when set it must be long enough to resist guessing. Surfaces a
        # weak/placeholder secret at startup rather than in a later audit.
        Validator(
            "WEBHOOK_SECRET",
            condition=lambda v: v is None or len(v) >= 16,
            messages={
                "condition": "WEBHOOK_SECRET must be at least 16 characters when set"
            },
        ),
        # Enum constraints (document_* enums are validated + normalized in
        # __post_init__ via _enum_fields instead, for case-insensitive input).
        Validator("LOG_FORMAT", is_in=["text", "json"]),
        Validator(
            "LOG_LEVEL",
            is_in=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        ),
        Validator(
            "OTEL_TRACES_SAMPLER",
            is_in=[
                "always_on",
                "always_off",
                "traceidratio",
                "parentbased_always_on",
                "parentbased_always_off",
                "parentbased_traceidratio",
            ],
        ),
        # Float ranges
        Validator("OTEL_TRACES_SAMPLER_ARG", gte=0.0, lte=1.0),
    ],
)


def _reload_config():
    """Reload dynaconf settings from files and environment.

    Call this in tests after modifying os.environ to refresh the cache.
    Re-validates all validators since reload() only checks unchecked ones.
    """
    _dynaconf.reload()
    _dynaconf.validators.validate_all()


_ephemeral_db_path: str | None = None


def get_token_db_path() -> str:
    """Resolve the token SQLite database path.

    Priority:
    1. TOKEN_STORAGE_DB if explicitly set — docker-compose pins
       /app/data/tokens.db this way. Read via dynaconf, which picks up
       the env var because TOKEN_STORAGE_DB is declared in _DEFAULTS.
    2. Otherwise a per-process tempfile under tempfile.gettempdir(),
       allocated lazily and deleted at interpreter exit via atexit.
       Ephemeral: tokens are wiped on restart, matching the Qdrant
       ":memory:" default pattern used elsewhere in this project.
    """
    explicit = _dynaconf.get("TOKEN_STORAGE_DB")
    if explicit:
        return str(explicit)
    global _ephemeral_db_path
    if _ephemeral_db_path is None:
        fd, path = tempfile.mkstemp(
            prefix=f"nextcloud-mcp-tokens-{os.getpid()}-", suffix=".db"
        )
        os.close(fd)
        _ephemeral_db_path = path

        def _cleanup(p: str = path) -> None:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

        atexit.register(_cleanup)
    return _ephemeral_db_path


def is_ephemeral_token_db(path: str) -> bool:
    """Return True if the given path is the process-local ephemeral tempfile.

    Precondition: `get_token_db_path()` must have been called at least once
    in this process to allocate the tempfile. If called before allocation,
    this returns False for any input (including the eventual tempfile path),
    because there is nothing to compare against yet. In practice every call
    site in this repo resolves the path via `get_token_db_path()` first.
    """
    return path == _ephemeral_db_path


def get_database_url() -> str:
    """Resolve the SQLAlchemy database URL for token storage.

    Priority:
    1. ``DATABASE_URL`` if set — any SQLAlchemy URL is accepted; the primary
       supported backends are ``postgresql+asyncpg://...`` for HA k8s
       deployments and ``sqlite+aiosqlite:///...`` for development.
    2. Otherwise build ``sqlite+aiosqlite:///{get_token_db_path()}`` so the
       legacy ``TOKEN_STORAGE_DB`` env var and the ephemeral-tempfile
       fallback both keep working unchanged.
    """
    explicit = _dynaconf.get("DATABASE_URL")
    if explicit:
        return str(explicit)
    return f"sqlite+aiosqlite:///{get_token_db_path()}"


def is_sqlite_url(url: str) -> bool:
    """Return True for SQLite SQLAlchemy URLs (used to gate sqlite-only logic
    like file-permission hardening and ``sqlite_master`` legacy lookups).

    Recognizes both file-backed (``sqlite+aiosqlite:///path/to/db``) and
    in-memory (``sqlite+aiosqlite:///:memory:``) URLs. The caller is
    responsible for handling ``:memory:`` as a magic value where a real
    filesystem path is expected.
    """
    return url.lower().startswith("sqlite")


def mask_db_password(url: str) -> str:
    """Return a logger-safe rendering of a SQLAlchemy URL.

    DATABASE_URL routinely carries a password (e.g.
    ``postgresql+asyncpg://mcp:secret@db/mcp``); logging it raw leaks the
    secret to stdout/stderr and any aggregator. SQLAlchemy's
    :func:`make_url` + ``render_as_string(hide_password=True)`` substitutes
    a fixed ``***`` placeholder while keeping the rest of the URL intact
    so operators can still see which host / driver they're hitting.
    """
    try:
        from sqlalchemy.engine.url import make_url  # noqa: PLC0415

        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        # If parsing fails (e.g. an explicit ssl-disable test URL with an
        # exotic shape), fall back to a regex that scrubs any
        # ``://user:password@`` pattern. Never raise from a logging path.
        import re  # noqa: PLC0415

        return re.sub(r"(://[^:/]+):[^@]*@", r"\1:***@", url)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "http",
        },
    },
    "formatters": {
        "http": {
            "format": "%(levelname)s [%(asctime)s] %(name)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "loggers": {
        "": {
            "handlers": ["default"],
            "level": "INFO",
        },
        "httpx": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,  # Prevent propagation to root logger
        },
        "httpcore": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,  # Prevent propagation to root logger
        },
        "uvicorn": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.error": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)


# Document Processing Configuration


def get_document_processor_config() -> dict[str, Any]:
    """Get document processor configuration from dynaconf.

    Returns:
        Dict with processor configs:
        {
            "enabled": bool,
            "default_processor": str,
            "processors": {
                "unstructured": {...},
                "tesseract": {...},
                "custom": {...},
            }
        }
    """
    config: dict[str, Any] = {
        "enabled": _dynaconf.get("ENABLE_DOCUMENT_PROCESSING"),
        "default_processor": _dynaconf.get("DOCUMENT_PROCESSOR"),
        "processors": {},
    }

    # Unstructured configuration
    if _dynaconf.get("ENABLE_UNSTRUCTURED"):
        languages_str = _dynaconf.get("UNSTRUCTURED_LANGUAGES")
        config["processors"]["unstructured"] = {
            "api_url": _dynaconf.get("UNSTRUCTURED_API_URL"),
            "timeout": _dynaconf.get("UNSTRUCTURED_TIMEOUT"),
            "strategy": _dynaconf.get("UNSTRUCTURED_STRATEGY"),
            "languages": [
                lang.strip() for lang in languages_str.split(",") if lang.strip()
            ],
            "progress_interval": _dynaconf.get("PROGRESS_INTERVAL"),
        }

    # Tesseract configuration
    if _dynaconf.get("ENABLE_TESSERACT"):
        config["processors"]["tesseract"] = {
            "tesseract_cmd": _dynaconf.get("TESSERACT_CMD"),  # None = auto-detect
            "lang": _dynaconf.get("TESSERACT_LANG"),
        }

    # PyMuPDF configuration (local PDF processing)
    if _dynaconf.get("ENABLE_PYMUPDF"):  # Enabled by default
        config["processors"]["pymupdf"] = {
            "extract_images": _dynaconf.get("PYMUPDF_EXTRACT_IMAGES"),
            "image_dir": _dynaconf.get(
                "PYMUPDF_IMAGE_DIR"
            ),  # None = use temp directory
        }

    # Custom processor (via HTTP API)
    if _dynaconf.get("ENABLE_CUSTOM_PROCESSOR"):
        custom_url = _dynaconf.get("CUSTOM_PROCESSOR_URL")
        if custom_url:
            supported_types_str = _dynaconf.get("CUSTOM_PROCESSOR_TYPES")
            supported_types = {
                t.strip() for t in supported_types_str.split(",") if t.strip()
            }

            config["processors"]["custom"] = {
                "name": _dynaconf.get("CUSTOM_PROCESSOR_NAME"),
                "api_url": custom_url,
                "api_key": _dynaconf.get("CUSTOM_PROCESSOR_API_KEY"),
                "timeout": _dynaconf.get("CUSTOM_PROCESSOR_TIMEOUT"),
                "supported_types": supported_types,
            }

    return config


@dataclass
class Settings:
    """Application settings from environment variables."""

    # Deployment mode (ADR-021: explicit mode selection; updated by ADR-022)
    # Optional: If not set, mode is auto-detected from other settings
    # Valid values: single_user_basic, multi_user_basic, login_flow
    # (ADR-022: `oauth_single_audience` was renamed to `login_flow`.)
    deployment_mode: str | None = None

    # OAuth/OIDC settings
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_issuer: str | None = None
    oidc_resource_server_id: str | None = None
    oidc_token_type: str = "Bearer"  # NEXTCLOUD_OIDC_TOKEN_TYPE
    oidc_scopes: str = ""  # NEXTCLOUD_OIDC_SCOPES (space-separated)
    port: int = 8000  # Server port (PORT); used to build fallback URLs

    # Nextcloud settings
    nextcloud_host: str | None = None
    nextcloud_username: str | None = None
    nextcloud_password: str | None = None
    nextcloud_app_password: str | None = None  # Preferred over nextcloud_password

    # Browser-reachable public URL for OAuth/Login-Flow-v2 redirects when
    # NEXTCLOUD_HOST is an internal Docker hostname. Falls back to
    # nextcloud_host when unset.
    nextcloud_public_issuer_url: str | None = None

    # Browser cookie Secure flag. None = auto-detect from nextcloud_host
    # scheme (https → True, else False). Set COOKIE_SECURE=true/false to
    # override.
    cookie_secure: bool | None = None

    # Nextcloud SSL/TLS settings
    nextcloud_verify_ssl: bool = True
    nextcloud_ca_bundle: str | None = None

    # Reuse pooled keep-alive connections for the Nextcloud httpx client.
    # Default True preserves low-latency interactive traffic. Set
    # NEXTCLOUD_HTTP_KEEPALIVE=false to force a fresh connection per request
    # (max_keepalive_connections=0) — mirrors the DATABASE_POOL_SIZE→NullPool
    # precedent and prevents a truncated/desynced response from poisoning a
    # pooled connection on flaky CDN/WAN paths (see #965).
    nextcloud_http_keepalive: bool = True

    # Postgres backend TLS settings (ADR-026). Default verify_ssl is None,
    # not True: when DATABASE_URL is unset there's nothing to verify, and
    # when it is set we don't want to break cluster-internal Postgres that
    # commonly runs without TLS. Operators opt in to verify-full with True
    # or supply a private-CA bundle.
    database_verify_ssl: bool | None = None
    database_ca_bundle: str | None = None
    # Postgres connection pool sizing — DEPRECATED, retained for
    # backward compatibility. The asyncpg engine switched to NullPool
    # in #799 (cross-event-loop crashes under anyio TaskGroups made
    # the original QueuePool + pool_pre_ping setup unsafe). These
    # fields no longer affect the Postgres engine; the validators
    # below still reject invalid values so misconfigured deploys
    # fail loudly rather than silently. See ADR-026 § Connection
    # pool and docs/configuration.md.
    database_pool_size: int = 2
    database_max_overflow: int = 5

    # ADR-005: Token Audience Validation (required for OAuth mode)
    nextcloud_mcp_server_url: str | None = None  # MCP server URL (used as audience)
    nextcloud_resource_uri: str | None = None  # Nextcloud resource identifier

    # Token verification endpoints
    jwks_uri: str | None = None
    introspection_uri: str | None = None
    userinfo_uri: str | None = None

    # Progressive Consent settings (always enabled - no flag needed)
    enable_offline_access: bool = False

    # Multi-user BasicAuth pass-through mode (ADR-019 interim solution).
    # Internal — not user-settable; the ENABLE_MULTI_USER_BASIC_AUTH env-var
    # alias was removed in the ADR-022 follow-up. Auto-set by
    # Settings.__post_init__ when MCP_DEPLOYMENT_MODE=multi_user_basic. When True,
    # the MCP server extracts BasicAuth credentials from request headers and
    # passes them through to Nextcloud APIs (no storage, stateless). Kept
    # as a field for backward compat with the runtime call sites that read it.
    enable_multi_user_basic_auth: bool = False

    # Login Flow v2 derived flag (ADR-022). Internal — not user-settable.
    # Auto-set by Settings.__post_init__ when the resolved deployment mode is
    # LOGIN_FLOW. Kept as a field for backward compat with the runtime call
    # sites that read it (app.py, context.py, scope_authorization.py).
    enable_login_flow: bool = False

    # Token and webhook storage settings
    # TOKEN_ENCRYPTION_KEY: Optional - Only required for OAuth token storage operations.
    #                       Webhook tracking works without encryption key.
    #                       If set, must be a valid base64-encoded Fernet key (32 bytes).
    # TOKEN_STORAGE_DB: Path to SQLite database for persistent storage.
    #                   Used for webhook tracking (all modes) and OAuth token storage.
    #                   Defaults to /tmp/tokens.db
    token_encryption_key: str | None = None
    token_storage_db: str | None = None

    # Webhook delivery authentication (ADR-010). REQUIRED for webhooks
    # (GHSA-8vh3-g2qg-2h2c). When set, the registrar passes
    # Authorization: Bearer <secret> as the webhook authData and the receiver
    # validates the same header on each delivery. When unset, the
    # /webhooks/nextcloud route is not mounted, the receiver refuses any request
    # that reaches it (503), and registration refuses to create webhooks — the
    # receiver trusts user.uid from the payload, so unauthenticated access would
    # let any caller delete/re-index other users' embeddings. Vector sync still
    # works via the polling scanner when this is unset.
    webhook_secret: str | None = None
    # Internal URL override for webhook registration. Highest-priority
    # source for the URL we register with NC (above
    # nextcloud_mcp_server_url and the docker-detection fallback).
    webhook_internal_url: str | None = None

    # Vector sync settings (ADR-007)
    vector_sync_enabled: bool = False
    vector_sync_scan_interval: int = 300  # seconds (5 minutes)
    vector_sync_processor_workers: int = 3
    vector_sync_queue_max_size: int = 10000
    # Cadence for the periodic gauge publisher (vector/metrics_publisher.py):
    # outstanding-work + indexed documents/chunks. Decoupled from the consumer
    # so the gauges are correct on every deployment mode and queue backend.
    vector_sync_metrics_refresh_interval: int = 20  # seconds
    vector_sync_user_poll_interval: int = 60  # seconds - OAuth mode user discovery
    vector_sync_orphan_sweep_enabled: bool = True  # card #101
    # Cadence for the background readiness dependency-health refresh loop
    # (app.py): keeps the Nextcloud/Qdrant snapshot warm off the probe path so
    # /health/ready never does external I/O (Deck #302).
    health_ready_refresh_interval: int = 15  # seconds
    # System tag marking files for vector indexing. The scanner indexes files
    # carrying this tag and verify-on-read gates results on current membership
    # (ADR-019), so an untagged file drops out of search immediately.
    vector_sync_pdf_tag: str = "vector-index"

    # Verify-on-read concurrency (ADR-019). Cap on parallel Nextcloud
    # round-trips during search-result verification fan-out. Lower this if the
    # Nextcloud backend struggles with the parallel load; raise it on a
    # healthy connection to speed up large result pages.
    verification_concurrency: int = 20

    # Qdrant settings (mutually exclusive modes)
    qdrant_url: str | None = None  # Network mode: http://qdrant:6333
    qdrant_location: str | None = None  # Local mode: :memory: or /path/to/data
    qdrant_api_key: str | None = None
    qdrant_collection: str = "nextcloud_content"

    # Ollama settings (embeddings + optional generation)
    ollama_base_url: str | None = None
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_generation_model: str | None = None
    ollama_verify_ssl: bool = True

    # OpenAI settings (embeddings + optional generation)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    openai_generation_model: str | None = None

    # Bedrock (AWS) settings — boto3 also reads these from its credential chain
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bedrock_embedding_model: str | None = None
    bedrock_generation_model: str | None = None

    # Mistral settings (embeddings only)
    mistral_api_key: str | None = None
    mistral_embedding_model: str = "mistral-embed"
    mistral_base_url: str | None = None

    # Simple (fallback) provider — dimension when no real provider configured
    simple_embedding_dimension: int = 384

    # Document chunking settings (for vector embeddings)
    document_chunk_size: int = 2048  # Characters per chunk
    document_chunk_overlap: int = 200  # Overlapping characters between chunks
    # Page-aware chunking for paginated docs (PDFs). When True (default), PDF
    # text is split on page boundaries first (one chunk per page; oversized
    # pages are character-split within the page), giving exact page numbers,
    # snippets that never lead with a neighbouring page, and a predictable
    # ~1 chunk/page when document_chunk_size >= the largest page. When False,
    # the legacy char-based path runs with post-hoc assign_page_numbers.
    document_chunk_page_aware: bool = True

    # PDF parse isolation (OOM guard). The parse runs in a subprocess so one
    # pathological file fails that doc, not the pod.
    # to_markdown graphics cap: pages with more vector drawings than this skip
    # the O(n^2) find_tables analysis. Must be >=1 -- pymupdf4llm treats 0 as
    # "no cap", which re-exposes the OOM. Default 1000: form/table PDFs have
    # ~1.5k grid-line drawings per page, which at the old 5000 cap slipped
    # through uncapped and timed out after ~17s/page (for zero recovered
    # tables); at 1000 they parse in ~3s with identical text. Pages with genuine
    # simple tables (<1000 drawings) still get table detection.
    document_pdf_graphics_limit: int = 1000
    # wall-clock cap per parse; the worker subprocess is killed on timeout.
    # float so a fractional DOCUMENT_PARSE_TIMEOUT_SECONDS is honoured, matching
    # anyio.move_on_after's float seconds.
    document_parse_timeout_seconds: float = 120.0
    # Pre-parse PDF size cap (MB). A PDF larger than this fails fast with
    # parse_failed_reason="oversize" (placeholder marked "failed") rather than
    # being handed to the fast/OCR tiers, where a pathological large file burns
    # the OCR timeout for 0 chars. 0 disables the guard.
    document_max_pdf_size_mb: float = 50.0
    # RLIMIT_AS in the parse subprocess (below the pod limit). Applied once per
    # worker for its lifetime, so changing it needs a pod restart.
    document_parse_mem_limit_mb: int = 1536
    # Tier-0 classifier. Records classification metrics (recommended_tier,
    # text-quality) on the tiered path, derived from the tier-1 extraction.
    document_classify_enabled: bool = True
    # PDF extraction engine for the ``fast`` tier. "pypdfium2" (default,
    # permissive license, no find_tables) is the hot path; "pymupdf" is a
    # deprecated rollback to pymupdf4llm (AGPL, graphics-limited) for one corpus.
    document_tier1_engine: str = "pypdfium2"
    # Route scanned/no-text-layer PDFs to the OCR tier. Off by default; when off,
    # the fast tier is terminal.
    document_ocr_enabled: bool = False
    # OCR backend selection: "auto" | "gateway" | "mistral" | "none". The gateway
    # routes on the model's "<provider>/" prefix, so it serves Mistral, surya, etc.
    document_ocr_provider: str = "auto"
    # Provider-namespaced OCR model id (e.g. "mistral/mistral-ocr-latest" or
    # "surya/surya-ocr-2"). The gateway routes on the "<provider>/" prefix; the
    # direct mistral backend strips it.
    document_ocr_model: str = "mistral/mistral-ocr-latest"
    # OCR backend HTTP request timeout (seconds). float for parity with the
    # parse timeout / httpx.Timeout; per-tenant tunable so a gateway with a
    # shorter ceiling isn't masked by the 180s default.
    document_ocr_timeout_seconds: float = 180.0
    # OCR execution mode: "sync" | "batch" (Deck #332). batch is opt-in and routes
    # through the embedding gateway's async Batch OCR job (large-corpus backfill).
    # It requires a gateway: with no EMBEDDING_GATEWAY_URL, __post_init__ rejects
    # mode=batch at startup rather than silently downgrading to sync.
    document_ocr_mode: str = "sync"
    # Batch-job poll cadence (procrastinate re-enqueue delay) and hard deadline.
    document_ocr_batch_poll_seconds: int = 120
    document_ocr_batch_max_wait_seconds: int = 86400
    # OCR escalation triggers (tier-0), per-tenant tunable. A page is OCR-worthy
    # if near-empty (< min_page_chars) OR low text-quality (< min_text_quality)
    # OR (when detect_scanned, image-analysis only runs when OCR is enabled)
    # mostly a raster image; the doc escalates at >= page_fraction such pages.
    document_ocr_min_text_quality: float = 0.5
    document_ocr_page_fraction: float = 0.5
    document_ocr_min_page_chars: int = 16
    document_ocr_detect_scanned: bool = True
    # Tier-0 glyph-corruption trigger: doc-level C0-control-char ratio above which
    # the fast (pypdfium2) text layer is treated as glyph-corrupt and escalated
    # fast->structured (pymupdf). 0 disables. See classifier._control_char_ratio.
    document_glyph_corruption_ratio: float = 0.02

    # Observability settings
    metrics_enabled: bool = True
    metrics_port: int = 9090
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_verify_ssl: bool = False
    otel_service_name: str = "nextcloud-mcp-server"
    otel_traces_sampler: str = "always_on"
    otel_traces_sampler_arg: float = 1.0
    log_format: str = "text"  # "json" or "text"
    log_level: str = "INFO"
    log_include_trace_context: bool = True

    # Tag-based file exclusion (issue #710): comma-separated list of
    # Nextcloud system tag names. Files/folders carrying any of these tags
    # are hidden from WebDAV MCP tools.
    excluded_tags: str = ""

    # MCP decomposition hook points (design §10, opt-in). All defaults
    # reproduce the current monolith; validated in __post_init__.
    embedding_provider: str = "autodetect"  # autodetect | gateway
    search_mode: str = "hybrid"  # hybrid | keyword (BM25 sparse only) — ADR-030
    # Ingest queue backend (Deck #183). None → resolved in __post_init__ to
    # ``postgres`` when DATABASE_URL is Postgres, else ``memory``.
    ingest_queue: str | None = None  # memory | postgres
    mcp_role: str = "all"  # api | worker | all (Deck #183 two-pod model)
    ingest_stalled_job_seconds: int = 300  # crashed-worker reclaim threshold
    ingest_delete_succeeded_jobs: bool = True  # drop succeeded ingest jobs
    ingest_listen_notify: bool = True  # False = poll-only (txn-mode pooler, Deck #424)
    ingest_escalation_enabled: bool = True  # per-tier queue-hop (Deck #323)
    ingest_transient_max_attempts: int = 5  # same-tier transient-retry cap
    ingest_reclaim_retry_delay_seconds: int = 30  # stagger reclaimed-job retries
    collection_metadata_source: str = "qdrant"  # qdrant | api
    collection_metadata_api_url: str | None = None  # CP URL when source=api
    embedding_gateway_url: str | None = None  # required when provider=gateway
    embedding_gateway_model: str = (
        "mistral/mistral-embed"  # provider-namespaced id the gateway routes on
    )
    # Gateway M2M OIDC client creds (separate realm; see _DEFAULTS comment).
    embedding_gateway_token_url: str | None = None
    embedding_gateway_client_id: str | None = None
    embedding_gateway_client_secret: str | None = None
    embedding_gateway_scope: str | None = None
    tenant_id: str | None = None  # per-tenant identity (UUID form)
    acl_prefilter_enabled: bool = False  # query-side ACL pre-filter (§11); OFF
    # Usage metering (Deck #67); OFF by default. When true, billable ops
    # record best-effort rows into the app-DB usage_events table for the
    # control plane to pull. See nextcloud_mcp_server/usage/store.py.
    usage_metering_enabled: bool = False

    def __post_init__(self):
        """Validate configuration and set defaults."""

        # Validate SSL/TLS configuration
        if not self.nextcloud_verify_ssl:
            logger.warning(
                "NEXTCLOUD_VERIFY_SSL is disabled. "
                "TLS certificate verification is turned off for all Nextcloud connections. "
                "This is insecure and should only be used for development/testing."
            )
        if self.nextcloud_ca_bundle:
            if not os.path.isfile(self.nextcloud_ca_bundle):
                raise ValueError(
                    f"NEXTCLOUD_CA_BUNDLE path does not exist: {self.nextcloud_ca_bundle}"
                )
            logger.info("Using custom CA bundle: %s", self.nextcloud_ca_bundle)

        # Surface the keep-alive opt-out at startup so an operator who set it to
        # work around #965 gets confirmation it took effect.
        if not self.nextcloud_http_keepalive:
            logger.info(
                "NEXTCLOUD_HTTP_KEEPALIVE is disabled. The Nextcloud HTTP client "
                "will open a fresh connection per request (no pooled keep-alive "
                "reuse) — a TLS handshake per call in exchange for immunity to "
                "poisoned/desynced pooled connections (#965)."
            )

        # Validate Postgres backend TLS configuration (ADR-026)
        if self.database_verify_ssl is False:
            logger.warning(
                "DATABASE_VERIFY_SSL is disabled. "
                "TLS certificate verification is turned off for the Postgres "
                "backend. Only acceptable for homelab / self-signed setups; "
                "prefer DATABASE_CA_BUNDLE for production."
            )
        if self.database_ca_bundle:
            if not os.path.isfile(self.database_ca_bundle):
                raise ValueError(
                    f"DATABASE_CA_BUNDLE path does not exist: {self.database_ca_bundle}"
                )
            logger.info(
                "Using custom CA bundle for Postgres backend: %s",
                self.database_ca_bundle,
            )

        # Pool sizing must be sensible — guard against operators accidentally
        # setting 0 / negative via env (would deadlock at first request).
        if self.database_pool_size < 1:
            raise ValueError(
                f"DATABASE_POOL_SIZE must be >= 1; got {self.database_pool_size}"
            )
        if self.database_max_overflow < 0:
            raise ValueError(
                f"DATABASE_MAX_OVERFLOW must be >= 0; got {self.database_max_overflow}"
            )

        # Ensure mutual exclusivity
        if self.qdrant_url and self.qdrant_location:
            raise ValueError(
                "Cannot set both QDRANT_URL and QDRANT_LOCATION. "
                "Use QDRANT_URL for network mode or QDRANT_LOCATION for local mode."
            )

        # Default to :memory: if neither set
        if not self.qdrant_url and not self.qdrant_location:
            self.qdrant_location = ":memory:"
            logger.debug("Using default Qdrant mode: in-memory (:memory:)")

        # Warn if API key set in local mode
        if self.qdrant_location and self.qdrant_api_key:
            logger.warning(
                "QDRANT_API_KEY is set but QDRANT_LOCATION is used (local mode). "
                "API key is only relevant for network mode and will be ignored."
            )

        # Validate chunking configuration
        if self.document_chunk_overlap >= self.document_chunk_size:
            raise ValueError(
                f"DOCUMENT_CHUNK_OVERLAP ({self.document_chunk_overlap}) must be less than "
                f"DOCUMENT_CHUNK_SIZE ({self.document_chunk_size}). "
                f"Overlap should be 10-20% of chunk size for optimal results."
            )

        if self.document_chunk_size < 512:
            logger.warning(
                "DOCUMENT_CHUNK_SIZE is set to %s characters, which is quite small. Smaller chunks may lose context. Consider using at least 1024 characters.",
                self.document_chunk_size,
            )

        # --- MCP decomposition hook points (design §10) ---
        # Normalize + validate the opt-in enum settings. Defaults reproduce
        # the monolith, so deployments that set none of these pass through.
        _enum_fields = {
            "embedding_provider": {"autodetect", "gateway"},
            "search_mode": {"hybrid", "keyword"},
            "mcp_role": {"api", "worker", "all"},
            "collection_metadata_source": {"qdrant", "api"},
            "document_tier1_engine": {"pypdfium2", "pymupdf"},
            "document_ocr_provider": {"auto", "gateway", "mistral", "none"},
            "document_ocr_mode": {"sync", "batch"},
        }
        for _field, _allowed in _enum_fields.items():
            _val = (getattr(self, _field) or "").strip().lower()
            setattr(self, _field, _val)
            if _val not in _allowed:
                raise ValueError(
                    f"{_field.upper()} must be one of {sorted(_allowed)}; got {_val!r}"
                )

        # Ingest queue backend (Deck #183). Procrastinate is opt-in: unset →
        # ``memory`` (the in-process anyio queue) regardless of DB backend, so a
        # Postgres DATABASE_URL alone never silently spins up a procrastinate
        # worker. ``postgres`` must be set explicitly, and an explicit
        # ``postgres`` against a SQLite DATABASE_URL is a misconfiguration —
        # fail loudly below.
        _queue = (self.ingest_queue or "").strip().lower()
        if not _queue:
            _queue = "memory"
        if _queue not in {"memory", "postgres"}:
            raise ValueError(
                f"INGEST_QUEUE must be one of ['memory', 'postgres']; got {_queue!r}"
            )
        self.ingest_queue = _queue
        if self.ingest_queue == "postgres" and is_sqlite_url(get_database_url()):
            raise ValueError(
                "INGEST_QUEUE=postgres requires a PostgreSQL DATABASE_URL "
                "(procrastinate is Postgres-only); use INGEST_QUEUE=memory for "
                "SQLite/dev"
            )

        if self.embedding_provider == "gateway" and not self.embedding_gateway_url:
            raise ValueError(
                "EMBEDDING_GATEWAY_URL is required when EMBEDDING_PROVIDER=gateway"
            )
        # Batch OCR routes through the embedding gateway's async Batch API (the only
        # batch path from the pod), so it requires a gateway. Fail fast rather than
        # silently downgrading to synchronous OCR at ingest time. provider=none means
        # OCR is off entirely, so the mode is moot there.
        if (
            self.document_ocr_mode == "batch"
            and self.document_ocr_provider != "none"
            and not self.embedding_gateway_url
        ):
            raise ValueError(
                "DOCUMENT_OCR_MODE=batch requires EMBEDDING_GATEWAY_URL (batch OCR "
                "routes through the embedding gateway); use DOCUMENT_OCR_MODE=sync "
                "for the direct backend without a gateway"
            )
        if (
            self.collection_metadata_source == "api"
            and not self.collection_metadata_api_url
        ):
            raise ValueError(
                "COLLECTION_METADATA_API_URL is required when "
                "COLLECTION_METADATA_SOURCE=api"
            )

        # Gateway M2M OIDC creds are all-or-nothing: a partial set (e.g. a
        # client_id with no token endpoint) is a misconfiguration that would
        # silently fall back to unauthenticated calls. scope is optional.
        _gw_creds = (
            self.embedding_gateway_token_url,
            self.embedding_gateway_client_id,
            self.embedding_gateway_client_secret,
        )
        if any(_gw_creds) and not all(_gw_creds):
            raise ValueError(
                "EMBEDDING_GATEWAY_TOKEN_URL, EMBEDDING_GATEWAY_CLIENT_ID, and "
                "EMBEDDING_GATEWAY_CLIENT_SECRET must be set together (M2M OIDC "
                "client-credentials) or all left unset (unauthenticated gateway)"
            )

        # --- ADR-022 follow-up: deployment mode is the single source of truth ---
        # The ENABLE_MULTI_USER_BASIC_AUTH and ENABLE_LOGIN_FLOW env vars were
        # removed in favour of MCP_DEPLOYMENT_MODE. We do TWO things here:
        #
        # 1. Loud-fail if a user still has either legacy env var set to a
        #    truthy value (silent removal would have flipped them into the
        #    wrong runtime mode). Only fires for truthy strings, so an
        #    explicit `ENABLE_LOGIN_FLOW=false` in a leftover .env passes
        #    through harmlessly.
        # 2. Derive `enable_login_flow` and `enable_multi_user_basic_auth`
        #    from the resolved deployment mode here, in __post_init__, so
        #    every Settings instance carries correct flags. (`get_settings()`
        #    builds a fresh Settings on each call — without this, the
        #    mutation that used to live in detect_auth_mode would only stick
        #    on the startup Settings instance, leaving per-request handlers
        #    with default False values.)
        _truthy = {"1", "true", "yes", "on"}
        for _legacy, _replacement in (
            ("ENABLE_MULTI_USER_BASIC_AUTH", "multi_user_basic"),
            ("ENABLE_LOGIN_FLOW", "login_flow"),
        ):
            # Read raw os.environ here, NOT cfg(): these legacy keys are
            # intentionally NOT in the dynaconf schema (they're derived from
            # MCP_DEPLOYMENT_MODE, ADR-022), so cfg() would always ignore them.
            # This is a deprecation *detector* for raw env usage, not config
            # reading — a deliberate exception to the dynaconf-drives-all rule.
            if os.environ.get(_legacy, "").strip().lower() in _truthy:
                raise ValueError(
                    f"{_legacy} is no longer read from the environment. "
                    f"Set MCP_DEPLOYMENT_MODE={_replacement} instead "
                    "(ADR-022). The deployment mode is the single source "
                    "of truth for selecting an auth flow."
                )

        # NOTE: this block mirrors the resolution logic in
        # `config_validators.detect_auth_mode` (which works on strings via a
        # `mode_map`). Both call sites resolve the deployment mode
        # independently — the canonical AuthMode enum in detect_auth_mode,
        # and the boolean derived flags here. **Keep them in sync when
        # adding a new mode**: a new entry must be added in both places, in
        # addition to `mode_map` (`config_validators.py`) and any
        # MODE_REQUIREMENTS entry.
        resolved_mode = (self.deployment_mode or "").strip().lower()
        if not resolved_mode:
            if self.nextcloud_username and self.nextcloud_password:
                resolved_mode = "single_user_basic"
            else:
                # Default multi-user mode is Login Flow v2 (browser-based
                # app-password acquisition); the un-augmented OAuth bearer
                # pass-through it replaced needed unmerged Nextcloud
                # user_oidc patches and is no longer supported.
                resolved_mode = "login_flow"

        self.enable_multi_user_basic_auth = resolved_mode == "multi_user_basic"
        self.enable_login_flow = resolved_mode == "login_flow"

    def _detect_base_provider(self) -> tuple[str, str]:
        """
        Resolve the ``(family, model)`` for the underlying embedding provider.

        Single source of truth for the provider-detection priority chain shared
        by ``get_embedding_model_name`` and ``get_embedding_provider_family``:
        1. Bedrock - if AWS_REGION or BEDROCK_EMBEDDING_MODEL is set
        2. OpenAI - if OPENAI_API_KEY is set
        3. Mistral - if MISTRAL_API_KEY is set
        4. Ollama - if OLLAMA_BASE_URL is set
        5. Simple - fallback

        Does NOT handle the gateway short-circuit — callers layer that on top
        as needed (see the asymmetry note on ``get_embedding_model_name``).
        """
        if (
            self.aws_region
            or self.bedrock_embedding_model
            or self.bedrock_generation_model
        ):
            return "bedrock", self.bedrock_embedding_model or "bedrock-default"

        if self.openai_api_key:
            return "openai", self.openai_embedding_model

        if self.mistral_api_key:
            return "mistral", self.mistral_embedding_model

        if self.ollama_base_url:
            return "ollama", self.ollama_embedding_model

        return "simple", f"simple-{self.simple_embedding_dimension}"

    def get_embedding_model_name(self) -> str:
        """
        Get the active embedding model name based on provider priority.

        Priority order (same as ProviderRegistry): bedrock → openai → mistral →
        ollama → simple (returns "simple-{dimension}").

        Returns:
            Active embedding model name
        """
        # NOTE: there is intentionally no "gateway" branch here. When
        # EMBEDDING_PROVIDER=gateway this falls through to the underlying
        # provider's model (used for the Qdrant collection name), whereas
        # get_embedding_provider_family() short-circuits to the gateway-routed
        # family. Keep that asymmetry in mind before joining metrics/labels
        # derived from these two methods.
        return self._detect_base_provider()[1]

    def get_embedding_provider_family(self) -> str:
        """
        Get the active dense-embedding provider family (a low-cardinality label).

        This is the single source of truth for the ``provider`` metric label and
        the ``embedding.provider`` span attribute. It returns the provider
        *family* (e.g. "bedrock"), never the model name, to keep metric
        cardinality bounded.

        Gateway short-circuits to the gateway-routed family (from the model
        prefix, e.g. "mistral/mistral-embed" -> "mistral"); otherwise the family
        comes from the shared ``_detect_base_provider`` priority chain.

        Returns:
            Provider family: gateway-routed family | bedrock | openai | mistral
            | ollama | simple
        """
        if self.embedding_provider == "gateway":
            model = self.embedding_gateway_model or ""
            return model.split("/", 1)[0] if "/" in model else "gateway"

        return self._detect_base_provider()[0]

    def get_collection_name(self) -> str:
        """
        Get Qdrant collection name.

        Auto-generates from deployment ID + model name unless explicitly set.
        Deployment ID uses OTEL_SERVICE_NAME if configured, otherwise hostname.

        This enables:
        - Safe embedding model switching (new model → new collection)
        - Multi-server deployments (unique deployment IDs)
        - Clear collection naming (shows deployment and model)

        Format: {deployment-id}-{model-name}

        Examples:
            - "my-deployment-nomic-embed-text" (Ollama)
            - "my-deployment-text-embedding-3-small" (OpenAI)
            - "mcp-container-openai-text-embedding-3-small" (hostname fallback)

        Returns:
            Collection name string
        """

        # Use explicit override if user configured non-default value
        if self.qdrant_collection != "nextcloud_content":
            return self.qdrant_collection

        # Determine deployment ID (OTEL service name or hostname fallback)
        if self.otel_service_name != "nextcloud-mcp-server":  # Non-default
            deployment_id = self.otel_service_name
        else:
            # Fallback to hostname for simple Docker deployments without OTEL config
            deployment_id = socket.gethostname()

        # Sanitize deployment ID
        deployment_id = deployment_id.lower().replace(" ", "-").replace("_", "-")

        # Keyword mode (ADR-030) indexes BM25 sparse vectors only and may run
        # with no embedding provider configured, so the embedding model name is
        # a meaningless ``simple-{dim}`` fallback. Use a fixed mode marker
        # instead: this guarantees keyword and hybrid indexes never share a
        # collection (their point schemas are not interchangeable — see ADR-030)
        # and removes keyword mode's dependency on the phantom model name.
        if self.search_mode == "keyword":
            return f"{deployment_id}-bm25-keyword"

        model_name = self.get_embedding_model_name().replace("/", "-").replace(":", "-")

        return f"{deployment_id}-{model_name}"

    # ADR-021: Property aliases for new naming convention
    # These provide the new names while maintaining backward compatibility with old field names

    @property
    def enable_semantic_search(self) -> bool:
        """Semantic search enabled (ADR-021 alias for vector_sync_enabled)."""
        return self.vector_sync_enabled

    @property
    def dense_enabled(self) -> bool:
        """Dense embeddings generated + queried.

        False in ``keyword`` mode (ADR-030), where ingestion and search use BM25
        sparse vectors only — no external embedding endpoint is contacted. Single
        source of truth for the processor, the search algorithm, and the Qdrant
        client so the three subsystems stay consistent.
        """
        return self.search_mode != "keyword"

    @property
    def enable_background_operations(self) -> bool:
        """Background operations enabled (ADR-021 alias for enable_offline_access)."""
        return self.enable_offline_access


def _get_semantic_search_enabled() -> bool:
    """Get semantic search enabled status, supporting both old and new variable names.

    Supports:
    - ENABLE_SEMANTIC_SEARCH (new, preferred)
    - VECTOR_SYNC_ENABLED (old, deprecated)

    Returns:
        True if semantic search should be enabled
    """
    new_value = _dynaconf.get("ENABLE_SEMANTIC_SEARCH", False)
    old_value = _dynaconf.get("VECTOR_SYNC_ENABLED", False)

    if new_value and old_value:
        logger.warning(
            "Both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED are set. "
            "Using ENABLE_SEMANTIC_SEARCH. "
            "VECTOR_SYNC_ENABLED is deprecated and will be removed in v1.0.0."
        )
    elif old_value and not new_value:
        logger.warning(
            "VECTOR_SYNC_ENABLED is deprecated. "
            "Please use ENABLE_SEMANTIC_SEARCH instead. "
            "Support for VECTOR_SYNC_ENABLED will be removed in v1.0.0."
        )

    return new_value or old_value


def _is_multi_user_mode() -> bool:
    """Detect if this is a multi-user deployment mode.

    Runs early in config setup (before Settings is fully built) for
    mode-conditional defaults. Must match the canonical detection in
    `config_validators.detect_auth_mode`, but works directly against the
    raw dynaconf store since Settings doesn't exist yet.

    Multi-user modes are:
    - Multi-user BasicAuth (MCP_DEPLOYMENT_MODE=multi_user_basic)
    - Login Flow v2 / default OAuth (MCP_DEPLOYMENT_MODE=login_flow, or no
      username/password and no explicit mode)

    Single-user mode is:
    - Single-user BasicAuth (username and password both set)

    Returns:
        True if multi-user mode detected
    """
    # Explicit deployment mode wins. The ENABLE_MULTI_USER_BASIC_AUTH env-var
    # alias was removed in the ADR-022 follow-up; selection is now via
    # MCP_DEPLOYMENT_MODE.
    explicit_mode = str(_dynaconf.get("MCP_DEPLOYMENT_MODE", "") or "").lower().strip()
    if explicit_mode in {"multi_user_basic", "login_flow"}:
        return True
    if explicit_mode == "single_user_basic":
        return False

    # If both username and password are set, it's single-user BasicAuth
    has_username = bool(_dynaconf.get("NEXTCLOUD_USERNAME"))
    has_password = bool(_dynaconf.get("NEXTCLOUD_PASSWORD"))
    if has_username and has_password:
        return False

    # Otherwise, assume multi-user (default when no credentials provided)
    return True


# Per-process guard for the three advisory log messages emitted by
# `_get_background_operations_enabled()`. The function runs on every
# `get_settings()` call (per ADR-024 / dynaconf design `get_settings()` is
# intentionally non-cached), so unguarded `logger.info`/`logger.warning`
# calls spam every MCP tool invocation. Mirrors the precedent at
# `nextcloud_mcp_server/vector/webhook_receiver.py:_warn_missing_secret_once`.
_bg_ops_advisories_logged: bool = False


def _log_bg_ops_advisories_once(
    explicit: bool, legacy: bool, auto_enabled: bool
) -> None:
    """Emit ENABLE_BACKGROUND_OPERATIONS advisory logs at most once per process."""
    global _bg_ops_advisories_logged
    if _bg_ops_advisories_logged:
        return
    _bg_ops_advisories_logged = True

    if explicit and legacy:
        logger.warning(
            "Both ENABLE_BACKGROUND_OPERATIONS and ENABLE_OFFLINE_ACCESS are set. "
            "Using ENABLE_BACKGROUND_OPERATIONS. "
            "ENABLE_OFFLINE_ACCESS is deprecated and will be removed in v1.0.0."
        )
    elif legacy and not explicit:
        logger.warning(
            "ENABLE_OFFLINE_ACCESS is deprecated. "
            "Please use ENABLE_BACKGROUND_OPERATIONS instead. "
            "Support for ENABLE_OFFLINE_ACCESS will be removed in v1.0.0."
        )
    if auto_enabled and not (explicit or legacy):
        logger.info(
            "Automatically enabled background operations for semantic search in multi-user mode. "
            "Set ENABLE_BACKGROUND_OPERATIONS=false to disable (this will also disable semantic search)."
        )


def _get_background_operations_enabled() -> bool:
    """Get background operations enabled status with auto-enablement for semantic search.

    Supports:
    - ENABLE_BACKGROUND_OPERATIONS (new, preferred)
    - ENABLE_OFFLINE_ACCESS (old, deprecated)
    - Auto-enabled if ENABLE_SEMANTIC_SEARCH=true in multi-user modes

    Returns:
        True if background operations should be enabled
    """
    explicit = _dynaconf.get("ENABLE_BACKGROUND_OPERATIONS", False)
    legacy = _dynaconf.get("ENABLE_OFFLINE_ACCESS", False)
    semantic_search_enabled = _get_semantic_search_enabled()
    is_multi_user = _is_multi_user_mode()
    auto_enabled = semantic_search_enabled and is_multi_user

    _log_bg_ops_advisories_once(explicit, legacy, auto_enabled)

    return explicit or legacy or auto_enabled


def _dget(key):
    """Get a value from dynaconf if configured, otherwise return _UNSET.

    Distinguishes "explicitly set to None" (via @none in TOML or env var)
    from "not configured at all". When _UNSET is returned, callers should
    let the Settings dataclass default apply.
    """
    return _dynaconf[key] if key in _dynaconf else _UNSET


def cfg(key: str, default=None):
    """Dynaconf-driven config accessor for keys NOT modelled on ``Settings``.

    The single supported way for application code to read configuration that
    isn't a first-class ``Settings`` field — reads from settings.toml, env vars
    and runtime overrides via dynaconf. Application code MUST use this (or
    ``get_settings().<field>``) instead of ``os.getenv`` / ``os.environ`` so all
    config is dynaconf-driven (settings.toml [default]/[<mode>] + env + overrides).

    Mirrors ``os.getenv(key, default)``: returns ``default`` when the value is
    unset OR ``None``, so a key modelled in ``_DEFAULTS`` with a ``None`` default
    does NOT shadow a meaningful call-site fallback (e.g.
    ``cfg("MCP_SERVER_CLIENT_SECRET", oauth_config.get("client_secret"))``).
    """
    value = _dynaconf.get(key)
    return value if value is not None else default


def set_override(key: str, value) -> None:
    """Set a runtime config override in dynaconf (the documented ``.set`` path).

    Used by the CLI to feed ``--flags`` into dynaconf instead of mutating
    ``os.environ``. Subsequent ``cfg()`` / ``get_settings()`` reads see it.
    ``tomlfy=True`` parses the value so typed scalars ("9090" -> int, "true" ->
    bool) land with the right type, matching settings.toml semantics.
    """
    _dynaconf.set(key, value, tomlfy=True)


def get_settings() -> Settings:
    """Get application settings from dynaconf configuration.

    Settings are loaded from (last wins):
    1. settings.toml [default] section
    2. settings.toml [<mode>] section (via MCP_DEPLOYMENT_MODE)
    3. .secrets.toml (if present)
    4. settings.local.toml (if present)
    5. Environment variables (highest priority)

    Values not found in any source are omitted, letting Settings dataclass
    defaults apply. This ensures the server starts correctly even without
    settings.toml (e.g., env-var-only deployments).

    Returns:
        Settings object with configuration values
    """
    # Get consolidated values with smart dependency resolution
    enable_semantic_search = _get_semantic_search_enabled()
    enable_background_operations = _get_background_operations_enabled()

    # Mapping from Settings field name to dynaconf key
    _field_map = {
        # Deployment mode (ADR-021)
        "deployment_mode": "MCP_DEPLOYMENT_MODE",
        # OAuth/OIDC settings
        "oidc_discovery_url": "OIDC_DISCOVERY_URL",
        "oidc_client_id": "NEXTCLOUD_OIDC_CLIENT_ID",
        "oidc_client_secret": "NEXTCLOUD_OIDC_CLIENT_SECRET",
        "oidc_issuer": "OIDC_ISSUER",
        "oidc_resource_server_id": "OIDC_RESOURCE_SERVER_ID",
        "oidc_token_type": "NEXTCLOUD_OIDC_TOKEN_TYPE",
        "oidc_scopes": "NEXTCLOUD_OIDC_SCOPES",
        "port": "PORT",
        # Nextcloud settings
        "nextcloud_host": "NEXTCLOUD_HOST",
        "nextcloud_username": "NEXTCLOUD_USERNAME",
        "nextcloud_password": "NEXTCLOUD_PASSWORD",
        "nextcloud_app_password": "NEXTCLOUD_APP_PASSWORD",
        "nextcloud_public_issuer_url": "NEXTCLOUD_PUBLIC_ISSUER_URL",
        "cookie_secure": "COOKIE_SECURE",
        # Nextcloud SSL/TLS settings
        "nextcloud_verify_ssl": "NEXTCLOUD_VERIFY_SSL",
        "nextcloud_ca_bundle": "NEXTCLOUD_CA_BUNDLE",
        "nextcloud_http_keepalive": "NEXTCLOUD_HTTP_KEEPALIVE",
        # Postgres backend TLS (ADR-026)
        "database_verify_ssl": "DATABASE_VERIFY_SSL",
        "database_ca_bundle": "DATABASE_CA_BUNDLE",
        # Postgres backend pool sizing (ADR-026)
        "database_pool_size": "DATABASE_POOL_SIZE",
        "database_max_overflow": "DATABASE_MAX_OVERFLOW",
        # ADR-005: Token Audience Validation
        "nextcloud_mcp_server_url": "NEXTCLOUD_MCP_SERVER_URL",
        "nextcloud_resource_uri": "NEXTCLOUD_RESOURCE_URI",
        # Token verification endpoints
        "jwks_uri": "JWKS_URI",
        "introspection_uri": "INTROSPECTION_URI",
        "userinfo_uri": "USERINFO_URI",
        # NOTE: `enable_multi_user_basic_auth` and `enable_login_flow` no
        # longer have env-var aliases — both are derived from the resolved
        # MCP_DEPLOYMENT_MODE in detect_auth_mode() so users only configure
        # the mode (ADR-022 follow-up).
        # Token and webhook storage settings
        "token_encryption_key": "TOKEN_ENCRYPTION_KEY",
        "token_storage_db": "TOKEN_STORAGE_DB",
        # Webhook auth (ADR-010)
        "webhook_secret": "WEBHOOK_SECRET",
        "webhook_internal_url": "WEBHOOK_INTERNAL_URL",
        # Vector sync settings (ADR-007)
        "vector_sync_scan_interval": "VECTOR_SYNC_SCAN_INTERVAL",
        "vector_sync_processor_workers": "VECTOR_SYNC_PROCESSOR_WORKERS",
        "vector_sync_queue_max_size": "VECTOR_SYNC_QUEUE_MAX_SIZE",
        "vector_sync_metrics_refresh_interval": "VECTOR_SYNC_METRICS_REFRESH_INTERVAL",
        "vector_sync_user_poll_interval": "VECTOR_SYNC_USER_POLL_INTERVAL",
        "vector_sync_orphan_sweep_enabled": "VECTOR_SYNC_ORPHAN_SWEEP_ENABLED",
        "health_ready_refresh_interval": "HEALTH_READY_REFRESH_INTERVAL",
        "vector_sync_pdf_tag": "VECTOR_SYNC_PDF_TAG",
        # Verify-on-read (ADR-019)
        "verification_concurrency": "VERIFICATION_CONCURRENCY",
        # Qdrant settings
        "qdrant_url": "QDRANT_URL",
        "qdrant_location": "QDRANT_LOCATION",
        "qdrant_api_key": "QDRANT_API_KEY",
        "qdrant_collection": "QDRANT_COLLECTION",
        # Ollama settings
        "ollama_base_url": "OLLAMA_BASE_URL",
        "ollama_embedding_model": "OLLAMA_EMBEDDING_MODEL",
        "ollama_generation_model": "OLLAMA_GENERATION_MODEL",
        "ollama_verify_ssl": "OLLAMA_VERIFY_SSL",
        # OpenAI settings
        "openai_api_key": "OPENAI_API_KEY",
        "openai_base_url": "OPENAI_BASE_URL",
        "openai_embedding_model": "OPENAI_EMBEDDING_MODEL",
        "openai_generation_model": "OPENAI_GENERATION_MODEL",
        # Bedrock (AWS) settings
        "aws_region": "AWS_REGION",
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "bedrock_embedding_model": "BEDROCK_EMBEDDING_MODEL",
        "bedrock_generation_model": "BEDROCK_GENERATION_MODEL",
        # Mistral settings
        "mistral_api_key": "MISTRAL_API_KEY",
        "mistral_embedding_model": "MISTRAL_EMBEDDING_MODEL",
        "mistral_base_url": "MISTRAL_BASE_URL",
        # Simple provider
        "simple_embedding_dimension": "SIMPLE_EMBEDDING_DIMENSION",
        # Document chunking settings
        "document_chunk_size": "DOCUMENT_CHUNK_SIZE",
        "document_chunk_overlap": "DOCUMENT_CHUNK_OVERLAP",
        "document_chunk_page_aware": "DOCUMENT_CHUNK_PAGE_AWARE",
        "document_pdf_graphics_limit": "DOCUMENT_PDF_GRAPHICS_LIMIT",
        "document_parse_timeout_seconds": "DOCUMENT_PARSE_TIMEOUT_SECONDS",
        "document_max_pdf_size_mb": "DOCUMENT_MAX_PDF_SIZE_MB",
        "document_parse_mem_limit_mb": "DOCUMENT_PARSE_MEM_LIMIT_MB",
        "document_classify_enabled": "DOCUMENT_CLASSIFY_ENABLED",
        "document_tier1_engine": "DOCUMENT_TIER1_ENGINE",
        "document_ocr_enabled": "DOCUMENT_OCR_ENABLED",
        "document_ocr_provider": "DOCUMENT_OCR_PROVIDER",
        "document_ocr_model": "DOCUMENT_OCR_MODEL",
        "document_ocr_timeout_seconds": "DOCUMENT_OCR_TIMEOUT_SECONDS",
        "document_ocr_mode": "DOCUMENT_OCR_MODE",
        "document_ocr_batch_poll_seconds": "DOCUMENT_OCR_BATCH_POLL_SECONDS",
        "document_ocr_batch_max_wait_seconds": "DOCUMENT_OCR_BATCH_MAX_WAIT_SECONDS",
        "document_ocr_min_text_quality": "DOCUMENT_OCR_MIN_TEXT_QUALITY",
        "document_ocr_page_fraction": "DOCUMENT_OCR_PAGE_FRACTION",
        "document_ocr_min_page_chars": "DOCUMENT_OCR_MIN_PAGE_CHARS",
        "document_ocr_detect_scanned": "DOCUMENT_OCR_DETECT_SCANNED",
        "document_glyph_corruption_ratio": "DOCUMENT_GLYPH_CORRUPTION_RATIO",
        # Observability settings
        "metrics_enabled": "METRICS_ENABLED",
        "metrics_port": "METRICS_PORT",
        "otel_exporter_otlp_endpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
        "otel_exporter_verify_ssl": "OTEL_EXPORTER_VERIFY_SSL",
        "otel_service_name": "OTEL_SERVICE_NAME",
        "otel_traces_sampler": "OTEL_TRACES_SAMPLER",
        "otel_traces_sampler_arg": "OTEL_TRACES_SAMPLER_ARG",
        "log_format": "LOG_FORMAT",
        "log_level": "LOG_LEVEL",
        "log_include_trace_context": "LOG_INCLUDE_TRACE_CONTEXT",
        "excluded_tags": "EXCLUDED_TAGS",
        # MCP decomposition hook points (design §10)
        "embedding_provider": "EMBEDDING_PROVIDER",
        "search_mode": "SEARCH_MODE",
        "ingest_queue": "INGEST_QUEUE",
        "mcp_role": "MCP_ROLE",
        "ingest_stalled_job_seconds": "INGEST_STALLED_JOB_SECONDS",
        "ingest_delete_succeeded_jobs": "INGEST_DELETE_SUCCEEDED_JOBS",
        "ingest_listen_notify": "INGEST_LISTEN_NOTIFY",
        "ingest_escalation_enabled": "INGEST_ESCALATION_ENABLED",
        "ingest_transient_max_attempts": "INGEST_TRANSIENT_MAX_ATTEMPTS",
        "ingest_reclaim_retry_delay_seconds": "INGEST_RECLAIM_RETRY_DELAY_SECONDS",
        "collection_metadata_source": "COLLECTION_METADATA_SOURCE",
        "collection_metadata_api_url": "COLLECTION_METADATA_API_URL",
        "embedding_gateway_url": "EMBEDDING_GATEWAY_URL",
        "embedding_gateway_model": "EMBEDDING_GATEWAY_MODEL",
        "embedding_gateway_token_url": "EMBEDDING_GATEWAY_TOKEN_URL",
        "embedding_gateway_client_id": "EMBEDDING_GATEWAY_CLIENT_ID",
        "embedding_gateway_client_secret": "EMBEDDING_GATEWAY_CLIENT_SECRET",
        "embedding_gateway_scope": "EMBEDDING_GATEWAY_SCOPE",
        "tenant_id": "TENANT_ID",
        "acl_prefilter_enabled": "ACL_PREFILTER_ENABLED",
        "usage_metering_enabled": "USAGE_METERING_ENABLED",
    }

    # Only pass values that dynaconf actually has; omit unset keys so
    # the Settings dataclass defaults apply.
    kwargs = {
        field: val
        for field, key in _field_map.items()
        if (val := _dget(key)) is not _UNSET
    }

    # Smart dependency overrides (always set, regardless of dynaconf)
    kwargs["vector_sync_enabled"] = enable_semantic_search
    kwargs["enable_offline_access"] = enable_background_operations

    return Settings(**kwargs)


def get_nextcloud_ssl_verify() -> bool | ssl.SSLContext:
    """Return the SSL verification setting for Nextcloud connections.

    Returns:
        - False if NEXTCLOUD_VERIFY_SSL=false (disable verification)
        - ssl.SSLContext if NEXTCLOUD_CA_BUNDLE is set (custom CA)
        - True otherwise (default system CA verification)
    """
    settings = get_settings()
    if not settings.nextcloud_verify_ssl:
        return False
    if settings.nextcloud_ca_bundle:
        ctx = ssl.create_default_context(cafile=settings.nextcloud_ca_bundle)
        return ctx
    return True


def get_nextcloud_http_keepalive() -> bool:
    """Return whether the Nextcloud httpx client may reuse pooled connections.

    Returns:
        - False if NEXTCLOUD_HTTP_KEEPALIVE=false (fresh connection per
          request; mitigates the poisoned-keep-alive truncation in #965).
        - True otherwise (default pooled keep-alive behavior).
    """
    return get_settings().nextcloud_http_keepalive


def get_database_ssl() -> bool | ssl.SSLContext | None:
    """Return the asyncpg ``ssl`` arg for the Postgres backend (ADR-026).

    Returns:
        - ``None`` when both DATABASE_VERIFY_SSL and DATABASE_CA_BUNDLE are
          unset — caller skips passing ``ssl`` so asyncpg keeps its default
          (``prefer``). Preserves PR #798 behavior for cluster-local
          Postgres without TLS.
        - ``False`` if DATABASE_VERIFY_SSL=false (silence cert errors).
        - ``ssl.SSLContext`` if DATABASE_CA_BUNDLE is set (custom private
          CA, implies verify-full).
        - ``True`` if DATABASE_VERIFY_SSL=true and no bundle (verify-full
          against system trust store).

    DATABASE_VERIFY_SSL=false wins over DATABASE_CA_BUNDLE so an operator
    can quickly silence cert errors during incident response without
    having to delete the bundle path from their secret store. Matches the
    Nextcloud-pattern precedence for symmetry with
    :func:`get_nextcloud_ssl_verify`.
    """
    settings = get_settings()
    if settings.database_verify_ssl is False:
        # Operator-explicit opt-out (DATABASE_VERIFY_SSL=false) — semantics
        # are documented in the docstring above and ADR-026 TLS section.
        # Bare NOSONAR silences any cert-verification-required rule that
        # may fire on this branch (defensive; no such rule fires today).
        return False  # NOSONAR
    if settings.database_ca_bundle:
        # ``ssl.create_default_context()`` on Python 3.10+ already negotiates
        # the strongest available protocol (TLS 1.2+ with secure ciphers);
        # we pin Python 3.11+ in pyproject.toml. ``purpose=SERVER_AUTH`` is
        # the default but spelt out here so the intent is visible to
        # static analysers and human readers alike.
        return ssl.create_default_context(  # NOSONAR
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=settings.database_ca_bundle,
        )
    if settings.database_verify_ssl is True:
        return True
    return None


def _pg_ssl_params() -> dict[str, str]:
    """Map the DATABASE_VERIFY_SSL / DATABASE_CA_BUNDLE settings to libpq
    keyword params for psycopg3 (used by procrastinate, Deck #183).

    psycopg/libpq takes ``sslmode`` (and ``sslrootcert``) rather than an
    ``ssl.SSLContext`` like asyncpg, so we translate :func:`get_database_ssl`'s
    intent into the equivalent libpq settings:

    - ``None``  (both unset)        → ``{}`` (omit; libpq default ``prefer``,
      matching the asyncpg default for cluster-local Postgres without TLS).
    - ``False`` (DATABASE_VERIFY_SSL=false) → ``sslmode=require`` (encrypt but
      do not verify the certificate).
    - CA bundle set                → ``sslmode=verify-full`` + ``sslrootcert``.
    - ``True``  (verify, no bundle) → ``sslmode=verify-full`` (system trust).
    """
    ssl_setting = get_database_ssl()
    if ssl_setting is None:
        return {}
    if ssl_setting is False:
        return {"sslmode": "require"}
    settings = get_settings()
    if settings.database_ca_bundle:
        return {"sslmode": "verify-full", "sslrootcert": settings.database_ca_bundle}
    return {"sslmode": "verify-full"}


def get_procrastinate_conninfo(database_url: str | None = None) -> str:
    """Build a libpq conninfo string for procrastinate's psycopg3 connector.

    Derives the connection from ``DATABASE_URL`` (a SQLAlchemy URL such as
    ``postgresql+asyncpg://user:pass@host/db``): the SQLAlchemy driver suffix
    (``+asyncpg``/``+psycopg``) is stripped and the parts are rendered via
    :func:`psycopg.conninfo.make_conninfo`, which quotes values correctly (never
    f-string the password). TLS settings are appended from :func:`_pg_ssl_params`.

    This is driver-agnostic on purpose: procrastinate uses psycopg3 regardless of
    which SQLAlchemy driver the app's own engine uses, so it works whether
    ``DATABASE_URL`` carries ``+asyncpg`` or ``+psycopg``.

    TODO(Deck #183 follow-up, out-of-tree): unify the app's SQLAlchemy engine on
    psycopg3 too (``postgresql+psycopg://``) and drop asyncpg, so the deployment
    ships a single Postgres driver. This belongs in the rendered Helm chart
    (set ``DATABASE_URL`` to a ``+psycopg`` URL) rather than rewriting the driver
    in code — see charts repo, not this repo.

    Only the host/port/dbname/user/password components are forwarded, plus
    ``connect_timeout`` (a libpq keyword) honored from the URL query string or
    defaulted to 10s so a slow/unreachable DB can't hang worker/API startup
    indefinitely. Any *other* ``?key=value`` query parameters are **dropped**
    (TLS is set separately via :func:`_pg_ssl_params`, and SQLAlchemy-specific
    options don't map cleanly to libpq keywords); a warning lists them.

    Raises ``ValueError`` for a non-Postgres URL — procrastinate is Postgres-only.
    """
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415
    from sqlalchemy.engine.url import make_url  # noqa: PLC0415

    url = make_url(database_url or get_database_url())
    if not url.drivername.startswith("postgresql"):
        raise ValueError(
            "get_procrastinate_conninfo requires a PostgreSQL DATABASE_URL; "
            f"got driver {url.drivername!r}"
        )

    # ``connect_timeout`` is forwarded (libpq keyword); everything else in the
    # query string is dropped with a warning.
    dropped = sorted(k for k in url.query if k != "connect_timeout")
    if dropped:
        logger.warning(
            "Dropping DATABASE_URL query parameters not forwarded to the "
            "procrastinate connector: %s",
            ", ".join(dropped),
        )

    params: dict[str, str] = {}
    if url.host:
        params["host"] = url.host
    if url.port:
        params["port"] = str(url.port)
    if url.database:
        params["dbname"] = url.database
    if url.username:
        params["user"] = url.username
    if url.password:
        params["password"] = url.password
    # Honor an operator-supplied connect_timeout, else default to 10s. (make_url
    # query values are str or a tuple of strs when repeated; take the last.)
    # ``connect_timeout=0`` (disable) is preserved — only a missing/empty value
    # falls back to the default, and an explicit-but-empty value is flagged.
    _ct = url.query.get("connect_timeout")
    if isinstance(_ct, (list, tuple)):
        _ct = _ct[-1] if _ct else None
    if _ct == "":
        logger.warning("DATABASE_URL has an empty connect_timeout=; using default 10s")
    params["connect_timeout"] = _ct if _ct else "10"
    params.update(_pg_ssl_params())

    return make_conninfo(**params)
