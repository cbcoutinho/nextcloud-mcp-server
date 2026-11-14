"""Per-credential Nextcloud session cookies for bearer pass-through.

In multi-user pass-through mode every MCP request builds a fresh
``NextcloudClient``, and ``AsyncDisableCookieTransport`` drops Nextcloud's
``Set-Cookie``. For Basic app passwords that is harmless. For an OAuth bearer
token it is not: with no session cookie, Nextcloud treats every API call as a
new login (``OC_User::loginWithApache``), which generates a 4096-bit RSA key
and stores a remembered ``oc_authtoken`` row that lives for 15 days.

This module keeps the session cookies Nextcloud hands out, one jar per bearer
token, and replays them on that token's later requests:

* ``SessionCookieStore`` maps ``sha256(token)`` to a cookie jar. The token
  itself is never stored or logged. Entries are bounded (LRU) and expire, so a
  jar never outlives the token it belongs to by more than the TTL.
* ``AsyncSessionCookieTransport`` applies one jar to one client. It works at
  the transport layer because the client-level jar cannot do the job: httpx
  copies it into a default-policy ``CookieJar`` for every request, and that
  policy never returns a ``Secure`` cookie for an ``http://`` URL. Nextcloud
  behind a TLS-terminating proxy (``overwriteprotocol=https``) marks every
  session cookie ``Secure`` while in-cluster callers reach it over plain http,
  so a stock jar stores the cookies and silently never sends them.
"""

import base64
import binascii
import hashlib
import json
import threading
import time
from collections import OrderedDict
from http.cookiejar import CookieJar, DefaultCookiePolicy

from httpx import AsyncBaseTransport, Cookies, Request, Response

# A jar is dropped this long after it was created, whether or not it is still
# in use. A session Nextcloud already holds is accepted on its cookie alone
# (WebDAV does not look at the Authorization header again), so the lifetime of
# a jar is how long a session can stand in for its token.
SESSION_TTL_SECONDS = 3600.0
# For a token whose expiry cannot be read (not a JWT, or no ``exp``).
SESSION_TTL_UNKNOWN_EXPIRY_SECONDS = 300.0
SESSION_STORE_MAX_ENTRIES = 256

# ``http`` is added so Secure cookies are replayed to an http:// Nextcloud URL.
# The jar only ever talks to NEXTCLOUD_HOST, and the bearer token it stands in
# for already travels over the same connection.
_SECURE_PROTOCOLS = ("https", "wss", "http")


def new_session_cookies() -> Cookies:
    """An empty jar that also returns ``Secure`` cookies over plain http."""
    return Cookies(CookieJar(DefaultCookiePolicy(secure_protocols=_SECURE_PROTOCOLS)))


def _token_expiry(token: str) -> float | None:
    """``exp`` of a JWT as a Unix timestamp, else ``None``. Not verified.

    Only ever used to shorten how long a jar is kept; Nextcloud is what
    validates the token.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (ValueError, binascii.Error):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return float(exp)


class SessionCookieStore:
    """Bounded, expiring map of credential fingerprint to cookie jar.

    Safe to call from concurrent requests and from threads: the map is guarded
    by a lock, and the jars are ``http.cookiejar.CookieJar`` objects, which
    lock internally. Two concurrent requests with the same token share one jar.
    """

    def __init__(
        self,
        *,
        max_entries: int = SESSION_STORE_MAX_ENTRIES,
        ttl: float = SESSION_TTL_SECONDS,
        ttl_unknown_expiry: float = SESSION_TTL_UNKNOWN_EXPIRY_SECONDS,
    ) -> None:
        self._max_entries = max_entries
        self._ttl = ttl
        self._ttl_unknown_expiry = ttl_unknown_expiry
        self._lock = threading.Lock()
        # fingerprint -> (jar, monotonic deadline), least recently used first.
        self._entries: OrderedDict[str, tuple[Cookies, float]] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, fingerprint: object) -> bool:
        with self._lock:
            return fingerprint in self._entries

    @staticmethod
    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _lifetime(self, token: str) -> float:
        expiry = _token_expiry(token)
        if expiry is None:
            return min(self._ttl, self._ttl_unknown_expiry)
        return min(self._ttl, expiry - time.time())

    def get(self, token: str) -> Cookies:
        """The jar for ``token``, created empty on first use or after expiry."""
        key = self.fingerprint(token)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[1] > now:
                self._entries.move_to_end(key)
                return entry[0]
            self._entries.pop(key, None)

            cookies = new_session_cookies()
            lifetime = self._lifetime(token)
            if lifetime <= 0:
                # Already expired by its own claim: Nextcloud will refuse it,
                # and nothing it sets is worth keeping.
                return cookies

            for stale in [k for k, (_, end) in self._entries.items() if end <= now]:
                del self._entries[stale]
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[key] = (cookies, now + lifetime)
            return cookies


class AsyncSessionCookieTransport(AsyncBaseTransport):
    """Replay and collect Nextcloud session cookies through one jar.

    Sends the jar's cookies with every request, stores what the response sets,
    then removes ``Set-Cookie`` so the httpx client's own jar stays empty (as
    with ``AsyncDisableCookieTransport``). The ``Authorization`` header is left
    alone: it is still sent on every request.
    """

    def __init__(self, transport: AsyncBaseTransport, cookies: Cookies) -> None:
        self.transport = transport
        self.cookies = cookies

    async def handle_async_request(self, request: Request) -> Response:
        self.cookies.set_cookie_header(request)
        response = await self.transport.handle_async_request(request)
        # extract_cookies() reads the URL from response.request, which the
        # client only assigns after the transport returns.
        response.request = request
        self.cookies.extract_cookies(response)
        response.headers.pop("set-cookie", None)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
