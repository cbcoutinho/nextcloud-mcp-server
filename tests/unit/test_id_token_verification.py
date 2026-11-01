"""Unit tests for OIDC ID token verification (issue #626 finding 1).

The OAuth callback handlers used to call
`jwt.decode(id_token, options={"verify_signature": False})` and trust the
result. They now go through `verify_id_token`, which checks signature
against JWKS and validates issuer / audience / exp / nonce per OIDC core
spec §3.1.3.7.
"""

import json
import time
from base64 import urlsafe_b64encode
from unittest.mock import patch

import anyio
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nextcloud_mcp_server.auth import token_utils
from nextcloud_mcp_server.auth.token_utils import (
    IdTokenVerificationError,
    verify_id_token,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_oidc_caches():
    """Reset the discovery+JWKS caches so tests don't share fetched data."""
    token_utils._discovery_cache.clear()
    token_utils._jwks_cache.clear()
    token_utils._fetch_locks.clear()
    yield
    token_utils._discovery_cache.clear()
    token_utils._jwks_cache.clear()
    token_utils._fetch_locks.clear()


# Generated once per process — RSA keypair generation is slow.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIVATE_PEM = _OTHER_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)

ISSUER = "https://idp.example.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"


def _b64u_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _build_jwks() -> dict:
    pub = _KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "test-key-1",
                "alg": "RS256",
                "n": _b64u_uint(pub.n),
                "e": _b64u_uint(pub.e),
            }
        ]
    }


def _sign(
    claims: dict, *, kid: str = "test-key-1", key_pem: bytes = _PRIVATE_PEM
) -> str:
    return jwt.encode(claims, key_pem, algorithm="RS256", headers={"kid": kid})


def _idp_handler(request: httpx2.Request) -> httpx2.Response:
    if str(request.url) == DISCOVERY_URL:
        return httpx2.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
    if str(request.url) == JWKS_URI:
        return httpx2.Response(
            200,
            content=json.dumps(_build_jwks()).encode(),
            headers={"content-type": "application/json"},
        )
    return httpx2.Response(404)


@pytest.fixture
def mock_idp():
    """Patch nextcloud_httpx_client used inside token_utils.verify_id_token."""
    transport = httpx2.MockTransport(_idp_handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        yield


async def test_verify_id_token_accepts_valid_token(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    payload = await verify_id_token(
        token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
    )
    assert payload["sub"] == "alice"


async def test_verify_id_token_rejects_wrong_audience(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "other-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_expired_token(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now - 120,
            "exp": now - 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_wrong_issuer(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": "https://evil.example.com",
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_wrong_signature(mock_idp):
    """Token signed with a different key but matching kid header must fail."""
    now = int(time.time())
    forged = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        key_pem=_OTHER_PRIVATE_PEM,
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            forged, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_unknown_kid(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        kid="not-in-jwks",
    )
    with pytest.raises(IdTokenVerificationError, match="No JWKS key matches"):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_nonce_mismatch_rejected(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
            "nonce": "actual",
        }
    )
    with pytest.raises(IdTokenVerificationError, match="nonce"):
        await verify_id_token(
            token,
            discovery_url=DISCOVERY_URL,
            expected_audience="test-client",
            expected_nonce="expected",
        )


async def test_verify_id_token_missing_token_rejected():
    with pytest.raises(IdTokenVerificationError, match="missing"):
        await verify_id_token(
            "", discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_recovers_after_kid_rotation():
    """Unknown kid → JWKS is refetched once and verification succeeds.

    Pins the fix for the PR #758 follow-up review: previously a kid-miss
    raised immediately, so every login failed for up to _OIDC_CACHE_TTL
    after the IdP rotated its signing key.
    """
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_pem = rotated_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _build_rotated_jwks() -> dict:
        pub = rotated_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "kid": "rotated-key",
                    "alg": "RS256",
                    "n": _b64u_uint(pub.n),
                    "e": _b64u_uint(pub.e),
                }
            ]
        }

    jwks_fetches = {"count": 0}

    def rotation_handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx2.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if url == JWKS_URI:
            jwks_fetches["count"] += 1
            # First fetch: stale JWKS (without rotated kid).
            # Subsequent fetches: post-rotation JWKS (with rotated kid).
            jwks = (
                _build_jwks() if jwks_fetches["count"] == 1 else _build_rotated_jwks()
            )
            return httpx2.Response(
                200,
                content=json.dumps(jwks).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx2.Response(404)

    transport = httpx2.MockTransport(rotation_handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        rotated_pem,
        algorithm="RS256",
        headers={"kid": "rotated-key"},
    )

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        # Prime the cache with the stale JWKS by triggering a verification
        # that misses on the rotated kid.
        payload = await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )

    assert payload["sub"] == "alice"
    assert jwks_fetches["count"] == 2, (
        "JWKS should be refetched once on kid miss "
        f"(actual fetches: {jwks_fetches['count']})"
    )


async def test_verify_id_token_rotation_retry_still_misses():
    """Refresh that still doesn't include the kid surfaces the original error."""
    fetches = {"count": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx2.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if url == JWKS_URI:
            fetches["count"] += 1
            return httpx2.Response(
                200,
                content=json.dumps(_build_jwks()).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        kid="never-existed",
    )

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        with pytest.raises(IdTokenVerificationError, match="No JWKS key matches"):
            await verify_id_token(
                token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
            )

    assert fetches["count"] == 2, "JWKS should be refetched once before raising"


async def test_verify_id_token_rotation_retry_network_error_wraps():
    """A 500 on the kid-miss refresh fetch surfaces as IdTokenVerificationError.

    Pins the fail-closed branch in the new refresh block: a network error
    during JWKS refetch must not bubble out as a bare exception — it has
    to be wrapped in IdTokenVerificationError so the caller's existing
    error handling stays correct.
    """
    fetches = {"jwks": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx2.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if url == JWKS_URI:
            fetches["jwks"] += 1
            # First fetch: stale-but-valid JWKS. Second (refresh): 500.
            if fetches["jwks"] == 1:
                return httpx2.Response(
                    200,
                    content=json.dumps(_build_jwks()).encode(),
                    headers={"content-type": "application/json"},
                )
            return httpx2.Response(500, content=b"upstream broke")
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        kid="not-cached-yet",
    )

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        with pytest.raises(
            IdTokenVerificationError, match="Failed to refresh JWKS after kid miss"
        ):
            await verify_id_token(
                token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
            )

    assert fetches["jwks"] == 2


async def test_verify_id_token_caches_discovery_and_jwks():
    """Discovery + JWKS must be cached: two verifications, one fetch each.

    Pins the fix for PR #758 finding 4 — every login previously made two
    extra HTTP round-trips to the IdP for the same metadata.
    """
    fetches: dict[str, int] = {}

    def counting_handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        fetches[url] = fetches.get(url, 0) + 1
        return _idp_handler(request)

    transport = httpx2.MockTransport(counting_handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )

    assert fetches.get(DISCOVERY_URL) == 1, "discovery fetched more than once"
    assert fetches.get(JWKS_URI) == 1, "JWKS fetched more than once"


async def test_get_cached_coalesces_concurrent_misses():
    """Concurrent cache misses must collapse into a single HTTP fetch.

    PR #758 round-3 review: without the per-URL lock in ``_get_cached``,
    N simultaneous callers at cache expiry would each fire their own
    request to the IdP, potentially tripping rate limits. The async
    handler yields with ``anyio.sleep(0.01)`` so all 10 callers reach
    the cache-miss branch concurrently — without coalescing the count
    would be 10.
    """
    fetch_count = {"n": 0}

    async def slow_handler(request: httpx2.Request) -> httpx2.Response:
        fetch_count["n"] += 1
        # Yield so concurrent waiters all reach the lock acquisition
        # while the first holder is still mid-fetch.
        await anyio.sleep(0.01)
        return _idp_handler(request)

    transport = httpx2.MockTransport(slow_handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    results: list[dict] = []

    async def fetch_once():
        results.append(await token_utils._get_cached(token_utils._jwks_cache, JWKS_URI))

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(fetch_once)

    assert fetch_count["n"] == 1, (
        f"expected exactly one fetch via lock coalescing, got {fetch_count['n']}"
    )
    assert len(results) == 10
    assert all(r == results[0] for r in results), (
        "concurrent callers received divergent cached data"
    )
    # Pin the round-4 cleanup invariant: _fetch_locks must drain after the
    # fetch completes so a probed deployment can't accumulate locks for
    # arbitrary URLs.
    assert len(token_utils._fetch_locks) == 0, (
        "expected _fetch_locks to be empty after fetch, "
        f"found {list(token_utils._fetch_locks)}"
    )
