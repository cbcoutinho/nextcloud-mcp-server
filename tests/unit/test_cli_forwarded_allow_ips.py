"""Unit tests for the proxy trust-list startup report (GH #1284).

``cli._log_forwarded_allow_ips`` exists because uvicorn silently demotes any
entry it cannot parse to a string literal that matches no real client, so a
typo'd CIDR looks configured while every request keeps getting logged as the
proxy's address.
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from nextcloud_mcp_server.cli import (
    _is_trusted_proxy_token,
    _log_forwarded_allow_ips,
    run,
)
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "token",
    ["*", "127.0.0.1", "192.168.1.5", "10.0.0.0/8", "::1", "fd00::/8"],
)
def test_parseable_tokens(token):
    assert _is_trusted_proxy_token(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "proxy.internal",  # hostname: uvicorn compares against an IP string
        "10.0.0.1/8",  # host bits set — uvicorn's ip_network() is strict
        "10.0.0.256",
        "",
    ],
)
def test_unparseable_tokens(token):
    assert _is_trusted_proxy_token(token) is False


def test_warns_only_about_the_bad_entries(caplog):
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.cli"):
        _log_forwarded_allow_ips("10.42.0.0/16, proxy.internal ,192.168.1.5")

    assert "10.42.0.0/16, proxy.internal ,192.168.1.5" in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "proxy.internal" in warnings[0].getMessage()
    assert "192.168.1.5" not in warnings[0].getMessage()


def test_no_warning_when_all_entries_parse(caplog):
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.cli"):
        _log_forwarded_allow_ips("10.0.0.0/8,192.168.1.5,*")

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "Trusting X-Forwarded-* headers from" in caplog.text


@pytest.mark.parametrize("value", [None, ""])
def test_silent_when_unset(value, caplog):
    """Unset is the default; uvicorn's own 127.0.0.1 resolution applies."""
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.cli"):
        _log_forwarded_allow_ips(value)

    assert not caplog.records


def test_run_configures_logging_before_reporting(mocker):
    """The startup report must not out-run the log config that formats it.

    `uvicorn.run()` applies `log_config` itself, but only once it is called —
    anything logged before that lands wherever the MCP SDK's `basicConfig()`
    rich handler left the root logger, i.e. rich text even under
    LOG_FORMAT=json. `caplog` attaches its own handler, so the tests above
    cannot see that ordering; this one asserts it directly.
    """
    calls: list[str] = []
    mocker.patch("nextcloud_mcp_server.cli.set_override")  # keep dynaconf pristine
    mocker.patch("nextcloud_mcp_server.cli.get_app")
    mocker.patch(
        "nextcloud_mcp_server.cli.get_settings",
        return_value=Settings(forwarded_allow_ips="10.0.0.0/8"),
    )
    mocker.patch(
        "logging.config.dictConfig", side_effect=lambda _cfg: calls.append("dictConfig")
    )
    mocker.patch(
        "nextcloud_mcp_server.cli._log_forwarded_allow_ips",
        side_effect=lambda _v: calls.append("report"),
    )
    uvicorn_run = mocker.patch(
        "nextcloud_mcp_server.cli.uvicorn.run",
        side_effect=lambda **_kw: calls.append("uvicorn.run"),
    )

    result = CliRunner().invoke(run, [])

    assert result.exit_code == 0, result.output
    assert calls == ["dictConfig", "report", "uvicorn.run"]
    assert uvicorn_run.call_args.kwargs["forwarded_allow_ips"] == "10.0.0.0/8"
