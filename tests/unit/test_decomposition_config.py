"""Tests for the MCP decomposition hook-point settings (design §10).

Every default must reproduce the monolith; the opt-in settings are validated
in ``Settings.__post_init__``.
"""

import pytest

from nextcloud_mcp_server.canonical import canonical_json
from nextcloud_mcp_server.config import Settings


class TestDecompositionDefaults:
    """With nothing set, behavior matches today's monolith."""

    def test_defaults_are_monolith(self):
        s = Settings()
        assert s.embedding_provider == "autodetect"
        assert s.ingest_mode == "local"
        assert s.status_backend == "local"
        assert s.collection_metadata_source == "qdrant"
        assert s.fact_event_emitter == "none"
        assert s.ingest_bus_url is None
        assert s.embedding_gateway_url is None
        assert s.tenant_id is None
        assert s.ingest_bus_num_replicas == 1

    def test_enum_values_normalized(self):
        # Mixed case / surrounding whitespace is normalized before validation.
        s = Settings(
            collection_metadata_source=" QDRANT ",
            fact_event_emitter="NONE",
        )
        assert s.collection_metadata_source == "qdrant"
        assert s.fact_event_emitter == "none"


class TestEnumValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("embedding_provider", "openai"),
            ("ingest_mode", "remote"),
            ("status_backend", "redis"),
            ("collection_metadata_source", "postgres"),
            ("fact_event_emitter", "kafka"),
        ],
    )
    def test_invalid_enum_rejected(self, field, value):
        with pytest.raises(ValueError, match=field.upper()):
            Settings(**{field: value})


class TestFailFast:
    def test_external_with_local_status_crashes(self):
        with pytest.raises(
            RuntimeError,
            match="STATUS_BACKEND=local is incompatible with INGEST_MODE=external",
        ):
            Settings(
                ingest_mode="external",
                status_backend="local",
                ingest_bus_url="nats://nats:4222",
                tenant_id="tenant-uuid",
            )


class TestConditionalRequired:
    def test_external_requires_bus_url(self):
        with pytest.raises(ValueError, match="INGEST_BUS_URL is required"):
            Settings(ingest_mode="external", status_backend="bus", tenant_id="t1")

    def test_external_requires_tenant_id(self):
        with pytest.raises(ValueError, match="TENANT_ID is required"):
            Settings(
                ingest_mode="external",
                status_backend="bus",
                ingest_bus_url="nats://nats:4222",
            )

    def test_gateway_requires_gateway_url(self):
        with pytest.raises(ValueError, match="EMBEDDING_GATEWAY_URL is required"):
            Settings(embedding_provider="gateway")

    def test_external_happy_path(self):
        s = Settings(
            ingest_mode="external",
            status_backend="bus",
            ingest_bus_url="nats://nats:4222",
            tenant_id="0a1b2c3d-0000-0000-0000-000000000000",
        )
        assert s.ingest_mode == "external"
        assert s.status_backend == "bus"

    def test_gateway_happy_path(self):
        s = Settings(
            embedding_provider="gateway",
            embedding_gateway_url="https://gateway:8083",
        )
        assert s.embedding_provider == "gateway"


class TestTenantIdSubjectToken:
    @pytest.mark.parametrize(
        "tenant_id",
        ["a.b", "a*b", "a>b", "a b", "a\tb"],
    )
    def test_illegal_subject_chars_rejected(self, tenant_id):
        with pytest.raises(ValueError, match="TENANT_ID must not contain"):
            Settings(tenant_id=tenant_id)

    def test_uuid_form_accepted(self):
        s = Settings(tenant_id="0a1b2c3d-0000-0000-0000-000000000000")
        assert s.tenant_id == "0a1b2c3d-0000-0000-0000-000000000000"


class TestReplicas:
    def test_zero_replicas_rejected(self):
        with pytest.raises(ValueError, match="INGEST_BUS_NUM_REPLICAS must be >= 1"):
            Settings(ingest_bus_num_replicas=0)


class TestCanonicalJson:
    def test_sorted_keys_no_whitespace(self):
        assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_non_ascii_preserved(self):
        # ensure_ascii=False keeps the literal UTF-8 bytes.
        assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode("utf-8")

    def test_stable_across_calls(self):
        obj = {"tenant_id": "t", "doc_id": "d", "modified_at": "2026-01-01T00:00:00Z"}
        assert canonical_json(obj) == canonical_json(dict(reversed(list(obj.items()))))
