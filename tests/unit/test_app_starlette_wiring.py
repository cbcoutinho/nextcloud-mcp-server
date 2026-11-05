"""Guard the Starlette app wiring that the 1.0 decorator removal broke (#1764).

``get_app()`` used ``@app.middleware`` / ``@app.exception_handler``, which
Starlette 1.0 removed; the server then crashed at startup while every unit
test still passed, because none built the real app.
"""

import logging

import pytest
from starlette.testclient import TestClient

from nextcloud_mcp_server.app import get_app
from nextcloud_mcp_server.config import _reload_config

pytestmark = pytest.mark.unit


def test_get_app_builds_and_logs_user_agent(monkeypatch, caplog):
    monkeypatch.setenv("NEXTCLOUD_HOST", "http://nextcloud.invalid")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "user")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "pass")
    _reload_config()

    app = get_app()

    # No `with`: skip the lifespan, which would dial Nextcloud.
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.app"):
        client.get("/api/v1/status", headers={"user-agent": "Nextcloud-Astrolabe/9.9"})

    assert any("Nextcloud-Astrolabe/9.9" in r.getMessage() for r in caplog.records)
