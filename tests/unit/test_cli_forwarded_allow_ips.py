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
from uvicorn.middleware.proxy_headers import _TrustedHosts

from nextcloud_mcp_server.cli import (
    _is_ip_or_network,
    _log_forwarded_allow_ips,
    run,
)
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "token",
    ["127.0.0.1", "192.168.1.5", "10.0.0.0/8", "::1", "fd00::/8"],
)
def test_parseable_tokens(token):
    assert _is_ip_or_network(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "proxy.internal",  # hostname: uvicorn compares against an IP string
        "10.0.0.1/8",  # host bits set — uvicorn's ip_network() is strict
        "10.0.0.256",
        "*",  # only a wildcard as the whole value, never as one entry
        "",
    ],
)
def test_unparseable_tokens(token):
    assert _is_ip_or_network(token) is False


@pytest.mark.parametrize(
    ("value", "trusts_a_public_address"),
    [
        ("*", True),
        ("10.0.0.0/8,*", False),
        (" * ", False),
    ],
)
def test_wildcard_semantics_match_uvicorn(value, trusts_a_public_address):
    """Pin the behavior `_log_forwarded_allow_ips` mirrors, against real uvicorn.

    `_TrustedHosts.always_trust` is an exact match on the whole raw value, so a
    "*" that is not the entire setting parses as neither address nor network
    and becomes an inert literal — narrowing the list instead of widening it.
    """
    hosts = _TrustedHosts(value)

    assert hosts.always_trust is (value == "*")
    assert ("203.0.113.9" in hosts) is trusts_a_public_address


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
        _log_forwarded_allow_ips("10.0.0.0/8,192.168.1.5")

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "Trusting X-Forwarded-* headers from" in caplog.text


def test_bare_wildcard_is_not_flagged(caplog):
    """The one form uvicorn honors as trust-everything."""
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.cli"):
        _log_forwarded_allow_ips("*")

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "Trusting X-Forwarded-* headers from: *" in caplog.text


@pytest.mark.parametrize("value", ["10.0.0.0/8,*", " * "])
def test_inert_wildcard_is_flagged(value, caplog):
    """A "*" that isn't the whole value silently narrows the list — say so."""
    with caplog.at_level(logging.INFO, logger="nextcloud_mcp_server.cli"):
        _log_forwarded_allow_ips(value)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "*" in warnings[0].getMessage()


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
