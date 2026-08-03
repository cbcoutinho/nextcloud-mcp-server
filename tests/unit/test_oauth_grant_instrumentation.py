"""Redaction and grant metrics for the AS proxy token endpoint.

These exist because the question "is this client refreshing, or re-running the
whole authorization flow every hour?" was unanswerable from logs — the token
exchange recorded only ``token_type``. The instrumentation added to answer it
handles live credentials, so the load-bearing test here is the negative one:
no secret may reach a log sink verbatim.
"""

import json

import pytest

from nextcloud_mcp_server.auth.oauth_routes import (
    _SECRET_FIELDS,
    _fingerprint,
    _redact,
    _redact_error_body,
)
from nextcloud_mcp_server.observability.metrics import record_oauth_grant

pytestmark = pytest.mark.unit


# A realistically-shaped Nextcloud OIDC token response. The secret values are
# deliberately long and distinctive so a leak is unmistakable in an assertion.
_SECRET_VALUES = {
    "access_token": "eyJhbGciOiJSUzI1NiJ9.QUNDRVNTX1RPS0VOX1NFQ1JFVA.sig-aaaaaa",
    "refresh_token": "REFRESH-TOKEN-SECRET-4f8a2c1e9b7d6350aabbccddeeff0011",
    "id_token": "eyJhbGciOiJSUzI1NiJ9.SURfVE9LRU5fU0VDUkVU.sig-bbbbbb",
}
_TOKEN_RESPONSE = {
    **_SECRET_VALUES,
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "openid profile email offline_access notes.read",
}


class TestRedactionNeverLeaks:
    """The property that matters: no secret survives into the log record."""

    def test_no_secret_value_appears_in_output(self):
        rendered = repr(_redact(_TOKEN_RESPONSE))
        for field, secret in _SECRET_VALUES.items():
            assert secret not in rendered, f"{field} leaked verbatim"

    def test_no_secret_fragment_appears_in_output(self):
        """A prefix/suffix of a token is still credential material."""
        rendered = repr(_redact(_TOKEN_RESPONSE))
        for secret in _SECRET_VALUES.values():
            assert secret[:16] not in rendered
            assert secret[-16:] not in rendered

    def test_every_declared_secret_field_is_actually_redacted(self):
        """Guards the registry itself.

        Adding a name to ``_SECRET_FIELDS`` without redaction logic, or
        redaction logic that silently skips a declared field, both fail here.
        """
        payload = {field: f"secret-value-for-{field}" for field in _SECRET_FIELDS}
        redacted = _redact(payload)
        for field in _SECRET_FIELDS:
            assert redacted[field].startswith("<len=")
            assert "secret-value-for" not in redacted[field]

    def test_secret_field_matching_is_case_insensitive(self):
        """Form bodies and IdP responses do not agree on casing."""
        redacted = _redact({"Refresh_Token": "SECRET", "AUTHORIZATION": "Bearer x"})
        assert "SECRET" not in repr(redacted)
        assert "Bearer x" not in repr(redacted)


class TestRedactionPreservesDiagnostics:
    """Redaction is worthless if it also hides the answer."""

    def test_non_secret_fields_pass_through_untouched(self):
        redacted = _redact(_TOKEN_RESPONSE)
        assert redacted["token_type"] == "Bearer"
        assert redacted["expires_in"] == 3600
        assert redacted["scope"] == "openid profile email offline_access notes.read"

    def test_presence_of_a_secret_is_still_visible(self):
        """'Was a refresh_token issued' must survive redaction — it is the
        entire point of the instrumentation."""
        assert "refresh_token" in _redact(_TOKEN_RESPONSE)
        assert "refresh_token" not in _redact(
            {k: v for k, v in _TOKEN_RESPONSE.items() if k != "refresh_token"}
        )

    def test_length_is_preserved(self):
        redacted = _redact({"refresh_token": "x" * 42})
        assert "len=42" in redacted["refresh_token"]

    def test_present_but_empty_is_distinct_from_absent(self):
        """An IdP returning ``refresh_token: ""`` is a different bug from one
        omitting the field, and the log must tell them apart."""
        assert _redact({"refresh_token": ""})["refresh_token"] == "<empty>"
        assert "refresh_token" not in _redact({"token_type": "Bearer"})


class TestFingerprintCorrelation:
    """The fingerprint exists to follow one token across hops."""

    def test_same_secret_gives_same_fingerprint(self):
        token = _SECRET_VALUES["refresh_token"]
        assert _fingerprint(token) == _fingerprint(token)

    def test_different_secrets_give_different_fingerprints(self):
        assert _fingerprint("token-a") != _fingerprint("token-b")

    def test_fingerprint_is_short_enough_to_be_useless_for_reversal(self):
        assert len(_fingerprint("anything")) == 8

    def test_same_token_correlates_across_two_redacted_payloads(self):
        """The IdP response and what we hand the client carry the same token;
        the log must make that provable without printing it."""
        issued = _redact(_TOKEN_RESPONSE)
        returned = _redact(dict(_TOKEN_RESPONSE))
        assert issued["refresh_token"] == returned["refresh_token"]


class TestErrorBodyRedaction:
    """IdP error bodies are external input and not guaranteed to be benign."""

    def test_standard_oauth_error_is_readable(self):
        body = json.dumps({"error": "invalid_grant", "error_description": "expired"})
        assert _redact_error_body(body) == {
            "error": "invalid_grant",
            "error_description": "expired",
        }

    def test_secret_echoed_in_an_error_body_is_redacted(self):
        body = json.dumps({"error": "invalid_grant", "refresh_token": "LEAKED-SECRET"})
        assert "LEAKED-SECRET" not in repr(_redact_error_body(body))

    def test_unparseable_body_reports_length_only(self):
        result = _redact_error_body("<html>502 Bad Gateway</html>")
        assert result == "<unparseable len=28>"

    def test_empty_body_does_not_raise(self):
        assert "unparseable" in _redact_error_body("")

    def test_non_dict_json_does_not_raise(self):
        """A bare JSON array or string is valid JSON but not a mapping."""
        assert _redact_error_body("[1, 2, 3]") == {"<non-dict response>": "list"}


class TestGrantMetric:
    def test_records_refresh_token_issued(self, metric_sample):
        labels = {
            "grant_type": "authorization_code",
            "result": "success",
            "refresh_token": "issued",
        }
        before = metric_sample("mcp_oauth_grants_total", labels)
        record_oauth_grant("authorization_code", "success", "issued")
        assert metric_sample("mcp_oauth_grants_total", labels) == before + 1

    def test_records_refresh_token_absent(self, metric_sample):
        """The signature of the disconnect: a grant that yields no refresh
        token, forcing the client back through the full flow next hour."""
        labels = {
            "grant_type": "authorization_code",
            "result": "success",
            "refresh_token": "absent",
        }
        before = metric_sample("mcp_oauth_grants_total", labels)
        record_oauth_grant("authorization_code", "success", "absent")
        assert metric_sample("mcp_oauth_grants_total", labels) == before + 1

    def test_refresh_token_defaults_to_unknown_on_failure(self, metric_sample):
        labels = {
            "grant_type": "refresh_token",
            "result": "error",
            "refresh_token": "unknown",
        }
        before = metric_sample("mcp_oauth_grants_total", labels)
        record_oauth_grant("refresh_token", "error")
        assert metric_sample("mcp_oauth_grants_total", labels) == before + 1
