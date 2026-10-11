"""The /mcp debug line must never log any part of a credential (hackbarn card 742)."""

import base64

from nextcloud_mcp_server.app import describe_authorization


def test_basic_credential_not_logged():
    secret = "watcher:YqkTjAMdbshUPcg67kgpvsvp1"
    header = "Basic " + base64.b64encode(secret.encode()).decode()
    out = describe_authorization(header)
    assert out.startswith("Basic sha256:")
    assert base64.b64encode(secret.encode()).decode()[:8] not in out
    assert "watcher" not in out


def test_bearer_token_not_logged():
    token = "eyJhbGciOiJSUzI1NiIsImtpZCI6IkNKakJvdlh2MGV.payload.sig"
    out = describe_authorization("Bearer " + token)
    assert out.startswith("Bearer sha256:")
    assert token[:6] not in out


def test_fingerprint_is_stable_and_short():
    a = describe_authorization("Bearer abc")
    assert a == describe_authorization("Bearer abc")
    assert len(a.split("sha256:")[1]) == 8


def test_malformed():
    assert describe_authorization("garbage") == "<malformed>"
