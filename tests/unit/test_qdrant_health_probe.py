"""Unit tests for the background Qdrant readiness probe.

``_check_qdrant_health`` populates the ``checks.qdrant`` entry that
``/health/ready`` reports. It probes the tenant's **collection** (not the
cluster's ``/readyz``) whenever vector sync is on, because the deployed api-key
is a collection-scoped JWT: a token that is expired, revoked, or scoped to the
wrong collection still passes ``/readyz`` while every real query fails.

That distinction is load-bearing — the control plane reads ``checks.qdrant`` to
decide whether a JWT rotation actually reached this Pod
(astrolabe-cloud-website board 6 #723), so a probe that can't fail on a dead
token silently manufactures confidence.
"""

import httpx
import pytest

from nextcloud_mcp_server import app as app_module
from nextcloud_mcp_server.app import _check_qdrant_health, _readiness_cache


def _settings(
    *,
    vector_sync: bool,
    url: str | None = "https://qdrant:6333",
    collection: str = "tenant_abc123",
    collection_raises: bool = False,
):
    """Minimal stand-in for the Settings object this probe reads."""

    class _S:
        qdrant_url = url
        qdrant_api_key = "tenant-jwt"
        vector_sync_enabled = vector_sync

        def get_collection_name(self) -> str:
            if collection_raises:
                raise RuntimeError("cannot derive collection name")
            return collection

    return _S()


@pytest.fixture
def probe(monkeypatch):
    """Drive ``_check_qdrant_health`` against a scripted transport, returning the
    resulting cache entry plus the URL that was actually requested."""

    async def _run(
        *,
        vector_sync: bool,
        handler,
        url: str | None = "https://qdrant:6333",
        collection: str = "tenant_abc123",
        collection_raises: bool = False,
    ):
        seen: dict[str, str] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["api_key"] = request.headers.get("api-key", "")
            return handler(request)

        real_client = httpx.AsyncClient

        def _client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(
            "nextcloud_mcp_server.app.get_settings",
            lambda: _settings(
                vector_sync=vector_sync,
                url=url,
                collection=collection,
                collection_raises=collection_raises,
            ),
        )
        monkeypatch.setattr("nextcloud_mcp_server.app.httpx.AsyncClient", _client)
        await _check_qdrant_health()
        return seen, _readiness_cache.snapshot().get("qdrant")

    return _run


@pytest.mark.unit
class TestQdrantHealthProbeTarget:
    async def test_probes_the_collection_when_vector_sync_enabled(self, probe):
        """The collection endpoint is what exercises the collection-scoped JWT."""
        seen, status = await probe(
            vector_sync=True, handler=lambda r: httpx.Response(200, json={"result": {}})
        )
        assert seen["url"] == "https://qdrant:6333/collections/tenant_abc123"
        assert status.detail == "ok"
        assert status.healthy is True

    async def test_forwards_the_api_key(self, probe):
        """Qdrant Cloud's gateway 403s unauthenticated reads."""
        seen, _ = await probe(
            vector_sync=True, handler=lambda r: httpx.Response(200, json={"result": {}})
        )
        assert seen["api_key"] == "tenant-jwt"

    async def test_noop_without_a_qdrant_url(self, probe, monkeypatch):
        """Embedded/local mode has no URL to probe — must not blow up."""
        seen, _ = await probe(
            vector_sync=True, handler=lambda r: httpx.Response(200), url=None
        )
        assert seen == {}  # no request issued


@pytest.mark.unit
class TestQdrantHealthProbeFailureModes:
    @pytest.mark.parametrize(
        ("status_code", "label"),
        [
            (401, "expired/invalid JWT"),
            (403, "revoked or wrong-scoped JWT"),
            (404, "collection deleted"),
        ],
    )
    async def test_unhealthy_on_error_status(self, probe, status_code, label):
        """Each of these is a real outage for the tenant and MUST report unhealthy.

        Before probing the collection, every one of them returned a 200 from
        /readyz — which is precisely how a rotation could report success over a
        Pod holding a dead token.
        """
        _seen, status = await probe(
            vector_sync=True, handler=lambda r: httpx.Response(status_code)
        )
        assert status.healthy is False, label
        assert str(status_code) in status.detail
        # The failure names what was probed so an operator can triage from the body.
        assert "tenant_abc123" in status.detail

    async def test_unhealthy_on_transport_error(self, probe):
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _seen, status = await probe(vector_sync=True, handler=_boom)
        assert status.healthy is False
        assert "error:" in status.detail

    async def test_healthy_detail_is_exactly_ok(self, probe):
        """The control plane's rotate-verify gates on this literal string — a
        decorated success value ("ok (collection ...)") would break it."""
        _seen, status = await probe(
            vector_sync=True, handler=lambda r: httpx.Response(200, json={"result": {}})
        )
        assert status.detail == "ok"


@pytest.mark.unit
class TestQdrantHealthProbeRobustness:
    async def test_collection_name_failure_reports_unhealthy_not_raises(self, probe):
        """A config error must surface as unhealthy, not escape the task.

        ``get_collection_name()`` derives from the embedding provider/hostname and
        can raise. This probe runs under ``tg.start_soon``, so an escaping
        exception also cancels the sibling Nextcloud probe for that cycle — and
        leaves ``checks.qdrant`` simply not updated rather than reporting a
        problem, which is the silent skip this probe exists to eliminate.
        """
        _seen, status = await probe(
            vector_sync=True,
            handler=lambda r: httpx.Response(200, json={"result": {}}),
            collection_raises=True,
        )
        assert status.healthy is False
        assert "error:" in status.detail

    async def test_collection_name_is_url_encoded(self, probe):
        """This is the one place the collection name is spliced into a URL path
        rather than handed to the qdrant-client SDK. ``get_collection_name()``'s
        explicit-override branch does no sanitisation, so an operator-set
        QDRANT_COLLECTION containing "/" would otherwise probe a different path
        entirely — and could read as healthy off the wrong resource.
        """
        seen, _status = await probe(
            vector_sync=True,
            handler=lambda r: httpx.Response(200, json={"result": {}}),
            collection="weird/name with space",
        )
        assert (
            seen["url"] == "https://qdrant:6333/collections/weird%2Fname%20with%20space"
        )


@pytest.mark.unit
class TestQdrantHealthProbeScheduling:
    """Pins the CALLER's gate, which is what decides whether the probe runs.

    An earlier revision of this file tested a `/readyz` fallback inside
    `_check_qdrant_health` for the vector-sync-disabled case. That branch was
    unreachable in the running server — `_refresh_dependency_health` never
    schedules the probe unless vector sync is on — so the test "passed" only by
    calling the function directly and bypassing the gate, proving nothing about
    production. These tests exercise the gate itself instead.
    """

    @staticmethod
    def _settings_for(*, vector_sync: bool, url: str | None):
        class _S:
            vector_sync_enabled = vector_sync
            qdrant_url = url

        return _S()

    async def test_probe_scheduled_only_with_vector_sync_and_a_url(self, monkeypatch):
        called: list[str] = []

        async def _fake_qdrant() -> None:
            called.append("qdrant")

        async def _fake_nextcloud() -> None:
            called.append("nextcloud")

        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_qdrant_health", _fake_qdrant
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_nextcloud_health", _fake_nextcloud
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app.get_settings",
            lambda: self._settings_for(vector_sync=True, url="https://qdrant:6333"),
        )
        await app_module._refresh_dependency_health()
        assert "qdrant" in called

    async def test_probe_not_scheduled_when_vector_sync_disabled(self, monkeypatch):
        """With vector sync off there is no collection and nothing populates
        checks.qdrant — which is why a /readyz fallback inside the probe would be
        dead code."""
        called: list[str] = []

        async def _fake_qdrant() -> None:
            called.append("qdrant")

        async def _fake_nextcloud() -> None:
            called.append("nextcloud")

        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_qdrant_health", _fake_qdrant
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_nextcloud_health", _fake_nextcloud
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app.get_settings",
            lambda: self._settings_for(vector_sync=False, url="https://qdrant:6333"),
        )
        await app_module._refresh_dependency_health()
        assert "qdrant" not in called

    async def test_embedded_mode_reports_without_probing(self, monkeypatch):
        """Vector sync on but no URL = embedded Qdrant; reported directly."""
        called: list[str] = []

        async def _fake_qdrant() -> None:
            called.append("qdrant")

        async def _fake_nextcloud() -> None:
            called.append("nextcloud")

        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_qdrant_health", _fake_qdrant
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app._check_nextcloud_health", _fake_nextcloud
        )
        monkeypatch.setattr(
            "nextcloud_mcp_server.app.get_settings",
            lambda: self._settings_for(vector_sync=True, url=None),
        )
        await app_module._refresh_dependency_health()
        assert "qdrant" not in called
        assert _readiness_cache.snapshot()["qdrant"].detail == "embedded"
