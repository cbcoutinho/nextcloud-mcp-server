"""Tests for the MCP decomposition hook-point settings (design §10, Deck #183).

Every default must reproduce the monolith; the opt-in settings are validated
in ``Settings.__post_init__``.
"""

import pytest

import nextcloud_mcp_server.config as config_module
from nextcloud_mcp_server.canonical import canonical_json
from nextcloud_mcp_server.config import Settings


class TestDecompositionDefaults:
    """With nothing set, behavior matches today's monolith."""

    def test_defaults_are_monolith(self):
        s = Settings()
        assert s.embedding_provider == "autodetect"
        # SQLite/dev default → the in-process memory queue.
        assert s.ingest_queue == "memory"
        assert s.mcp_role == "all"
        assert s.collection_metadata_source == "qdrant"
        assert s.embedding_gateway_url is None
        assert s.tenant_id is None
        # LISTEN/NOTIFY stays on by default — poll-only is opt-in for
        # transaction-mode poolers (Deck #424).
        assert s.ingest_listen_notify is True

    def test_listen_notify_toggle(self):
        # The worker passes this straight to procrastinate's
        # run_worker_async(listen_notify=...); false = poll-only for a
        # transaction-mode pooler (PgBouncer).
        assert Settings(ingest_listen_notify=False).ingest_listen_notify is False
        assert Settings(ingest_listen_notify=True).ingest_listen_notify is True

    def test_enum_values_normalized(self):
        # Mixed case / surrounding whitespace is normalized before validation.
        s = Settings(
            collection_metadata_source=" QDRANT ",
            mcp_role=" API ",
            document_tier1_engine=" PyPDFium2 ",
            document_ocr_provider=" Gateway ",
        )
        assert s.collection_metadata_source == "qdrant"
        assert s.mcp_role == "api"
        assert s.document_tier1_engine == "pypdfium2"
        assert s.document_ocr_provider == "gateway"


class TestKeywordTag:
    """VECTOR_SYNC_KEYWORD_TAG selects files to index keyword-only (BM25 sparse,
    no dense vector), mirroring VECTOR_SYNC_TAG. Defaults to ``keyword-index``
    (symmetric with the ``vector-index`` default); set empty to disable."""

    def test_default_is_keyword_index(self):
        assert Settings().vector_sync_keyword_tag == "keyword-index"

    def test_explicit_value_round_trips(self):
        # Mirrors VECTOR_SYNC_TAG: a configured tag is passed through verbatim
        # (an explicit value overrides the default tag name).
        assert (
            Settings(vector_sync_keyword_tag="kw-only").vector_sync_keyword_tag
            == "kw-only"
        )

    def test_empty_disables(self):
        # Setting it empty turns the second tag off entirely.
        assert Settings(vector_sync_keyword_tag="").vector_sync_keyword_tag == ""


class TestEnumValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("embedding_provider", "openai"),
            ("mcp_role", "leader"),
            ("collection_metadata_source", "redis"),
            ("document_tier1_engine", "mupdf"),
            ("document_ocr_provider", "gatway"),
            ("docling_pipeline", "vllm"),
        ],
    )
    def test_invalid_enum_rejected(self, field, value):
        with pytest.raises(ValueError, match=field.upper()):
            Settings(**{field: value})

    def test_invalid_ingest_queue_rejected(self):
        with pytest.raises(ValueError, match="INGEST_QUEUE"):
            Settings(ingest_queue="kafka")

    def test_docling_ocr_provider_accepted(self):
        # docling is a valid (explicit-only) OCR provider, ADR-031.
        assert (
            Settings(document_ocr_provider="docling").document_ocr_provider == "docling"
        )

    def test_docling_pipeline_vlm_accepted(self):
        # vlm is the opt-in pipeline that drives docling-serve's VLM presets, ADR-032.
        assert Settings(docling_pipeline="vlm").docling_pipeline == "vlm"


class TestReadTimeoutCap:
    """Opt-in interactive read-parse cap (DOCUMENT_READ_TIMEOUT_SECONDS, ADR-032)."""

    def test_default_is_disabled(self):
        assert Settings().document_read_timeout_seconds is None

    def test_numeric_string_coerced_to_float(self):
        # dynaconf may hand the env value through as a string -> coerce for fail_after.
        s = Settings(document_read_timeout_seconds="60")
        assert s.document_read_timeout_seconds == pytest.approx(60.0)

    def test_empty_string_disables(self):
        # A bare DOCUMENT_READ_TIMEOUT_SECONDS= (compose passthrough) means "unset".
        assert (
            Settings(document_read_timeout_seconds="").document_read_timeout_seconds
            is None
        )
        assert (
            Settings(document_read_timeout_seconds="  ").document_read_timeout_seconds
            is None
        )

    def test_below_one_rejected(self):
        with pytest.raises(ValueError, match="DOCUMENT_READ_TIMEOUT_SECONDS"):
            Settings(document_read_timeout_seconds=0)

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="DOCUMENT_READ_TIMEOUT_SECONDS"):
            Settings(document_read_timeout_seconds="abc")


class TestIngestQueueResolution:
    def test_postgres_requires_postgres_url(self):
        # Explicit postgres against the default SQLite DATABASE_URL is a
        # misconfiguration (procrastinate is Postgres-only).
        with pytest.raises(ValueError, match="INGEST_QUEUE=postgres requires"):
            Settings(ingest_queue="postgres")

    def test_memory_default_even_on_postgres_url(self, monkeypatch):
        # Procrastinate is opt-in: a Postgres DATABASE_URL with INGEST_QUEUE
        # unset must NOT silently enable procrastinate. Default → memory.
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+psycopg://mcp:mcp@db/mcp",
        )
        assert Settings().ingest_queue == "memory"

    def test_explicit_postgres_on_postgres_url(self, monkeypatch):
        # Opting in explicitly against a Postgres URL is the supported path.
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+psycopg://mcp:mcp@db/mcp",
        )
        assert Settings(ingest_queue="postgres").ingest_queue == "postgres"

    def test_explicit_memory_on_postgres_url(self, monkeypatch):
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+psycopg://mcp:mcp@db/mcp",
        )
        assert Settings(ingest_queue="memory").ingest_queue == "memory"


class TestConditionalRequired:
    def test_gateway_requires_gateway_url(self):
        with pytest.raises(ValueError, match="EMBEDDING_GATEWAY_URL is required"):
            Settings(embedding_provider="gateway")

    def test_gateway_happy_path(self):
        s = Settings(
            embedding_provider="gateway",
            embedding_gateway_url="https://gateway:8083",
        )
        assert s.embedding_provider == "gateway"


class TestTenantId:
    def test_arbitrary_tenant_id_accepted(self):
        # The old NATS-subject charset restriction was dropped with NATS
        # (Deck #183); tenant_id is now just an opaque per-tenant identity.
        s = Settings(tenant_id="0a1b2c3d-0000-0000-0000-000000000000")
        assert s.tenant_id == "0a1b2c3d-0000-0000-0000-000000000000"


class TestProcrastinateConninfo:
    """Model A (ADR-026): DATABASE_URL is passed through verbatim — the only
    transform is stripping the SQLAlchemy ``+<driver>`` tag so libpq accepts
    the URL. No decomposition, no injected defaults, no env-var TLS."""

    def test_conninfo_strips_only_driver_tag(self, monkeypatch):
        # sslmode, connect_timeout, and the (percent-encoded) password all pass
        # through byte-for-byte; only ``+psycopg`` is removed.
        url = "postgresql+psycopg://mcp:p%40ss@db:5432/mcp?sslmode=require&connect_timeout=7"
        monkeypatch.setattr(config_module, "get_database_url", lambda: url)
        assert (
            config_module.get_procrastinate_conninfo()
            == "postgresql://mcp:p%40ss@db:5432/mcp?sslmode=require&connect_timeout=7"
        )

    def test_conninfo_parses_to_expected_libpq_params(self, monkeypatch):
        from psycopg.conninfo import conninfo_to_dict

        url = "postgresql+psycopg://mcp:p%40ss@db:5432/mcp?sslmode=require"
        monkeypatch.setattr(config_module, "get_database_url", lambda: url)
        parsed = conninfo_to_dict(config_module.get_procrastinate_conninfo())
        assert parsed["password"] == "p@ss"
        assert parsed["host"] == "db"
        assert parsed["dbname"] == "mcp"
        assert parsed["sslmode"] == "require"

    def test_conninfo_strips_driver_tag_case_insensitively(self, monkeypatch):
        # The scheme guard is case-insensitive; the driver-tag strip must match,
        # so an unconventional-cased scheme can't slip through unstripped.
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "Postgresql+psycopg://mcp:s@db/mcp?sslmode=require",
        )
        assert (
            config_module.get_procrastinate_conninfo()
            == "postgresql://mcp:s@db/mcp?sslmode=require"
        )

    def test_conninfo_no_injected_connect_timeout(self, monkeypatch):
        # The server injects nothing — a URL without connect_timeout stays that
        # way (the CP/gitops-generated DSN owns the default, not the server).
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+psycopg://mcp:s@db/mcp",
        )
        assert config_module.get_procrastinate_conninfo() == "postgresql://mcp:s@db/mcp"

    def test_conninfo_no_warning_on_query_params(self, monkeypatch, caplog):
        # The old decomposition dropped unknown query params with a warning;
        # passthrough must emit no such warning.
        import logging

        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+psycopg://mcp:s@db/mcp?sslmode=require&application_name=x",
        )
        with caplog.at_level(logging.WARNING):
            config_module.get_procrastinate_conninfo()
        assert "Dropping DATABASE_URL query parameters" not in caplog.text

    def test_conninfo_rejects_non_postgres(self, monkeypatch):
        monkeypatch.setattr(
            config_module, "get_database_url", lambda: "sqlite+aiosqlite:///x.db"
        )
        with pytest.raises(ValueError, match="requires a PostgreSQL DATABASE_URL"):
            config_module.get_procrastinate_conninfo()


class TestCanonicalJson:
    def test_sorted_keys_no_whitespace(self):
        assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_non_ascii_preserved(self):
        # ensure_ascii=False keeps the literal UTF-8 bytes.
        assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode("utf-8")

    def test_stable_across_calls(self):
        obj = {"tenant_id": "t", "doc_id": "d", "modified_at": "2026-01-01T00:00:00Z"}
        assert canonical_json(obj) == canonical_json(dict(reversed(list(obj.items()))))
