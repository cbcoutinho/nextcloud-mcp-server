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


class TestEnumValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("embedding_provider", "openai"),
            ("mcp_role", "leader"),
            ("collection_metadata_source", "redis"),
            ("document_tier1_engine", "mupdf"),
            ("document_ocr_provider", "gatway"),
        ],
    )
    def test_invalid_enum_rejected(self, field, value):
        with pytest.raises(ValueError, match=field.upper()):
            Settings(**{field: value})

    def test_invalid_ingest_queue_rejected(self):
        with pytest.raises(ValueError, match="INGEST_QUEUE"):
            Settings(ingest_queue="kafka")


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
            lambda: "postgresql+asyncpg://mcp:mcp@db/mcp",
        )
        assert Settings().ingest_queue == "memory"

    def test_explicit_postgres_on_postgres_url(self, monkeypatch):
        # Opting in explicitly against a Postgres URL is the supported path.
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+asyncpg://mcp:mcp@db/mcp",
        )
        assert Settings(ingest_queue="postgres").ingest_queue == "postgres"

    def test_explicit_memory_on_postgres_url(self, monkeypatch):
        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+asyncpg://mcp:mcp@db/mcp",
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
    @pytest.mark.parametrize(
        "url,expected_sslmode",
        [
            ("postgresql+asyncpg://mcp:p%40ss@db:5432/mcp", None),
        ],
    )
    def test_conninfo_round_trips_password(self, monkeypatch, url, expected_sslmode):
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setattr(config_module, "get_database_url", lambda: url)
        # No SSL settings → sslmode omitted (libpq default ``prefer``).
        monkeypatch.setattr(config_module, "get_database_ssl", lambda: None)
        parsed = conninfo_to_dict(config_module.get_procrastinate_conninfo())
        assert parsed["password"] == "p@ss"
        assert parsed["host"] == "db"
        assert parsed["dbname"] == "mcp"
        assert parsed.get("sslmode") == expected_sslmode

    def test_conninfo_connect_timeout_defaults_to_10(self, monkeypatch):
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+asyncpg://mcp:s@db/mcp",
        )
        monkeypatch.setattr(config_module, "get_database_ssl", lambda: None)
        parsed = conninfo_to_dict(config_module.get_procrastinate_conninfo())
        assert parsed["connect_timeout"] == "10"

    def test_conninfo_honors_url_connect_timeout(self, monkeypatch):
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+asyncpg://mcp:s@db/mcp?connect_timeout=3",
        )
        monkeypatch.setattr(config_module, "get_database_ssl", lambda: None)
        parsed = conninfo_to_dict(config_module.get_procrastinate_conninfo())
        assert parsed["connect_timeout"] == "3"

    def test_conninfo_ssl_mapping(self, monkeypatch):
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setattr(
            config_module,
            "get_database_url",
            lambda: "postgresql+asyncpg://mcp:s@db/mcp",
        )
        # verify off → encrypt without verifying.
        monkeypatch.setattr(config_module, "get_database_ssl", lambda: False)
        assert (
            conninfo_to_dict(config_module.get_procrastinate_conninfo())["sslmode"]
            == "require"
        )
        # verify on → verify-full.
        monkeypatch.setattr(config_module, "get_database_ssl", lambda: True)
        assert (
            conninfo_to_dict(config_module.get_procrastinate_conninfo())["sslmode"]
            == "verify-full"
        )

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
