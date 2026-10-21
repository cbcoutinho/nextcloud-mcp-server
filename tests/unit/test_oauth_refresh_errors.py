"""How the AS proxy relays a failed ``refresh_token`` grant to the client.

MCP clients act on the OAuth error code: on ``invalid_grant`` they discard
their refresh token and send the user back through the browser login. The
proxy used to answer every non-200 from the IdP with ``invalid_grant``, so an
IdP outage, a rate limit, or a misconfigured MCP server credential all looked
like an expired refresh token and logged the user out. These tests pin the
mapping that replaced it.
"""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from nextcloud_mcp_server.auth import oauth_routes

pytestmark = pytest.mark.unit

_REFRESH_TOKEN = "REFRESH-TOKEN-SECRET-7d1e0c9b4a3f2e18"
_CLIENT_SECRET = "CLIENT-SECRET-SECRET-5b6a7c8d9e0f1a2b"
_TOKEN_ENDPOINT = "https://idp.example.test/apps/oidc/token"


def _request(form: dict[str, str] | None = None):
    """Minimal request carrying the AS proxy's OAuth context and a form.

    The form carries a ``client_id`` and the context a token storage whose
    client-binding lookup finds nothing, so these tests also hold for a
    proxy that checks which client a refresh token was issued to.
    """
    from unittest.mock import AsyncMock

    from starlette.datastructures import FormData

    storage = AsyncMock()
    storage.get_refresh_token_client.return_value = None
    oauth_context = {
        "storage": storage,
        "config": {
            "client_id": "mcp-server-client",
            "client_secret": _CLIENT_SECRET,
            "mcp_server_url": "https://mcp.example.test",
            "discovery_url": "https://idp.example.test/.well-known/openid-configuration",
        },
    }
    data = form or {
        "grant_type": "refresh_token",
        "refresh_token": _REFRESH_TOKEN,
        "client_id": "https://client.example.test/metadata.json",
    }

    class _Req:
        app = SimpleNamespace(state=SimpleNamespace(oauth_context=oauth_context))

        async def form(self):
            return FormData(data)

    return _Req()


@pytest.fixture
def idp(mocker, monkeypatch):
    """Route the proxy's IdP calls to a handler the test sets."""
    monkeypatch.delenv("MCP_SERVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("MCP_SERVER_CLIENT_SECRET", raising=False)
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return state["respond"](request)

    mocker.patch.object(
        oauth_routes,
        "get_oidc_discovery",
        mocker.AsyncMock(return_value={"token_endpoint": _TOKEN_ENDPOINT}),
    )
    mocker.patch.object(
        oauth_routes,
        "nextcloud_httpx_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return state


async def _refresh(idp_state, respond):
    idp_state["respond"] = respond
    return await oauth_routes._token_refresh(_request(), await _request().form())


def _body(response) -> dict:
    return json.loads(bytes(response.body))


class TestOAuthErrorsFromTheIdP:
    async def test_invalid_grant_is_relayed_with_its_description(self, idp):
        """The case that should log the user out still does, and the client
        now learns why instead of a generic 'Token refresh failed'."""
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Refresh token is expired.",
                },
            ),
        )
        assert response.status_code == 400
        assert _body(response) == {
            "error": "invalid_grant",
            "error_description": "Refresh token is expired.",
        }

    @pytest.mark.parametrize(
        "code", ["invalid_request", "invalid_grant", "invalid_scope", "invalid_target"]
    )
    async def test_client_facing_codes_pass_through(self, idp, code):
        response = await _refresh(
            idp, lambda r: httpx.Response(400, json={"error": code})
        )
        assert response.status_code == 400
        assert _body(response) == {"error": code}

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (401, "invalid_client"),
            (400, "unauthorized_client"),
            (400, "unsupported_grant_type"),
            (400, "something_nonstandard"),
        ],
    )
    async def test_errors_about_the_server_itself_are_server_error(
        self, idp, status, code
    ):
        """The proxy refreshes with its own client credentials, so an IdP
        complaint about the client is the MCP server's misconfiguration.
        Relaying ``invalid_client`` would make the downstream client discard
        its own registration, and ``invalid_grant`` its tokens; neither helps."""
        response = await _refresh(
            idp, lambda r: httpx.Response(status, json={"error": code})
        )
        assert response.status_code == 502
        assert _body(response)["error"] == "server_error"

    async def test_non_oauth_4xx_body_is_server_error(self, idp):
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                404,
                text="<html>Not Found</html>",
                headers={"content-type": "text/html"},
            ),
        )
        assert response.status_code == 502
        assert _body(response)["error"] == "server_error"

    @pytest.mark.parametrize(
        "description",
        [
            'contains a "quote"',
            "non-ascii \u00e9",
            "",
            42,
        ],
    )
    async def test_description_outside_rfc_charset_or_bound_is_dropped(
        self, idp, description
    ):
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": description},
            ),
        )
        assert response.status_code == 400
        assert _body(response) == {"error": "invalid_grant"}

    @pytest.mark.parametrize(
        ("length", "kept"), [(199, True), (200, True), (201, False)]
    )
    async def test_description_length_boundary(self, idp, length, kept):
        description = "a" * length
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": description},
            ),
        )
        assert response.status_code == 400
        expected = description if kept else None
        assert _body(response).get("error_description") == expected

    @pytest.mark.parametrize(
        "description",
        ["bad\r\nSet-Cookie: x=1", "tab\there", "nul\x00", "back\\slash"],
        ids=["crlf", "tab", "nul", "backslash"],
    )
    async def test_description_with_control_or_escape_chars_is_dropped(
        self, idp, description
    ):
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": description},
            ),
        )
        assert _body(response) == {"error": "invalid_grant"}

    @pytest.mark.parametrize(
        "error", [["invalid_grant"], {"code": "invalid_grant"}], ids=["list", "dict"]
    )
    async def test_non_string_error_is_server_error(self, idp, error):
        """An unhashable ``error`` must not crash the membership check."""
        response = await _refresh(
            idp, lambda r: httpx.Response(400, json={"error": error})
        )
        assert response.status_code == 502
        assert _body(response)["error"] == "server_error"

    @pytest.mark.parametrize(
        "respond",
        [
            lambda r: httpx.Response(400),
            lambda r: httpx.Response(400, text="Bad Request"),
        ],
        ids=["empty", "text"],
    )
    async def test_non_oauth_400_is_not_reported_as_invalid_grant(self, idp, respond):
        """A 400 that is not an OAuth error says nothing about the refresh
        token, so it must not make the client discard it."""
        response = await _refresh(idp, respond)
        assert response.status_code == 502
        assert _body(response)["error"] == "server_error"

    async def test_redirect_with_oauth_body_is_not_relayed(self, idp):
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                302,
                headers={"Location": "https://elsewhere.example.test/"},
                json={"error": "invalid_grant"},
            ),
        )
        assert response.status_code == 502
        assert _body(response)["error"] == "server_error"


class TestTransientFailures:
    """None of these say anything about the refresh token, so the client must
    keep it and retry rather than re-run the browser login."""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    async def test_rate_limit_and_5xx_are_temporarily_unavailable(self, idp, status):
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                status, json={"error": "invalid_grant", "error_description": "x"}
            ),
        )
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"

    async def test_unparseable_5xx_is_temporarily_unavailable(self, idp):
        response = await _refresh(
            idp, lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"

    async def test_unreachable_idp_is_temporarily_unavailable(self, idp):
        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        response = await _refresh(idp, refuse)
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"

    async def test_idp_timeout_is_temporarily_unavailable(self, idp):
        def slow(request):
            raise httpx.ReadTimeout("timed out", request=request)

        response = await _refresh(idp, slow)
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"

    async def test_failed_discovery_is_temporarily_unavailable(self, idp, mocker):
        mocker.patch.object(
            oauth_routes,
            "get_oidc_discovery",
            mocker.AsyncMock(side_effect=httpx.ConnectError("no route")),
        )
        response = await _refresh(idp, lambda r: httpx.Response(200, json={}))
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"

    @pytest.mark.parametrize(
        "discovery",
        [
            {"issuer": "https://idp.example.test"},
            ValueError("discovery body is not JSON"),
        ],
        ids=["no-token_endpoint", "non-json"],
    )
    async def test_unusable_discovery_is_temporarily_unavailable(
        self, idp, mocker, caplog, discovery
    ):
        if isinstance(discovery, Exception):
            lookup = mocker.AsyncMock(side_effect=discovery)
        else:
            lookup = mocker.AsyncMock(return_value=discovery)
        mocker.patch.object(oauth_routes, "get_oidc_discovery", lookup)
        caplog.set_level(
            logging.WARNING, logger="nextcloud_mcp_server.auth.oauth_routes"
        )
        response = await _refresh(idp, lambda r: httpx.Response(200, json={}))
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"
        assert "discovery body is not JSON" not in caplog.text

    @pytest.mark.parametrize(
        "token_endpoint",
        [None, 42, "", "/apps/oidc/token", [_TOKEN_ENDPOINT]],
        ids=["none", "int", "empty", "relative", "list"],
    )
    async def test_unusable_token_endpoint_is_temporarily_unavailable(
        self, idp, mocker, caplog, token_endpoint
    ):
        mocker.patch.object(
            oauth_routes,
            "get_oidc_discovery",
            mocker.AsyncMock(return_value={"token_endpoint": token_endpoint}),
        )
        caplog.set_level(logging.DEBUG, logger="nextcloud_mcp_server.auth.oauth_routes")
        response = await _refresh(idp, lambda r: httpx.Response(200, json={}))
        assert response.status_code == 503
        assert _body(response)["error"] == "temporarily_unavailable"
        assert "unusable token_endpoint" in caplog.text
        assert "/apps/oidc/token" not in caplog.text


class TestRegressionGuards:
    async def test_success_is_returned_unchanged(self, idp):
        tokens = {
            "access_token": "a",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "r2",
        }
        seen: dict = {}

        def ok(request):
            seen["form"] = dict(
                httpx.QueryParams(request.content.decode("ascii")).multi_items()
            )
            return httpx.Response(200, json=tokens)

        response = await _refresh(idp, ok)
        assert response.status_code == 200
        assert _body(response) == tokens
        assert seen["form"]["grant_type"] == "refresh_token"
        assert seen["form"]["refresh_token"] == _REFRESH_TOKEN

    @pytest.mark.parametrize("status", [400, 401, 503])
    async def test_no_secret_reaches_the_client_or_the_log(self, idp, caplog, status):
        """An IdP that echoes the submitted credentials in its error body must
        not get them relayed to the client or written to the log."""
        caplog.set_level(logging.DEBUG, logger="nextcloud_mcp_server.auth.oauth_routes")
        response = await _refresh(
            idp,
            lambda r: httpx.Response(
                status,
                json={
                    "error": "invalid_grant",
                    "refresh_token": _REFRESH_TOKEN,
                    "client_secret": _CLIENT_SECRET,
                },
            ),
        )
        text = bytes(response.body).decode()
        assert _REFRESH_TOKEN not in text
        assert _CLIENT_SECRET not in text
        assert _REFRESH_TOKEN not in caplog.text
        assert _CLIENT_SECRET not in caplog.text

    async def test_unreachable_idp_logs_no_url_or_secret(self, idp, caplog):
        caplog.set_level(logging.DEBUG, logger="nextcloud_mcp_server.auth.oauth_routes")

        def refuse(request):
            raise httpx.ConnectError(f"refused {request.url}", request=request)

        await _refresh(idp, refuse)
        assert "ConnectError" in caplog.text
        assert _TOKEN_ENDPOINT not in caplog.text
        assert _CLIENT_SECRET not in caplog.text

    async def test_endpoint_still_counts_relayed_failures(self, idp, metric_sample):
        """oauth_token_endpoint counts every non-200; the new statuses must
        still reach that counter."""
        labels = {
            "grant_type": "refresh_token",
            "result": "error",
            "refresh_token": "unknown",
        }
        before = metric_sample("mcp_oauth_grants_total", labels)
        idp["respond"] = lambda r: httpx.Response(503)
        response = await oauth_routes.oauth_token_endpoint(_request())
        assert response.status_code == 503
        assert metric_sample("mcp_oauth_grants_total", labels) == before + 1
