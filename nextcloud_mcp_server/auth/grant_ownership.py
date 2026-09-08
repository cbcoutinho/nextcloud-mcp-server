"""Ownership check for completed Login Flow v2 grants (GHSA-84qv-22q6-x82r).

Login Flow v2 hands out a *transferable* URL: whoever opens it and clicks
"Grant access" produces the app password, and nothing in the flow itself ties
that person to the OAuth caller who started it. Storing the result under the
caller's identity without checking hands an attacker the victim's Nextcloud
credential after a single click.

The check compares canonical Nextcloud UIDs, never names:

* the **granter** is resolved by authenticating the fresh app password against
  OCS ``/cloud/user`` — the same move ``api/passwords.py`` makes for
  GHSA-x88r-fhx7-52h6. This maps the Login Flow ``loginName`` (which may be an
  email alias, or an LDAP login that differs from the UID — GH #980) onto the
  account Nextcloud will actually act as.
* the **caller** is the OAuth ``sub``. When Nextcloud is the IdP that already
  *is* the UID. With an external IdP (Keycloak) ``sub`` is an opaque UUID, so
  the IdP's ``preferred_username`` is consulted as well — that is the claim
  ``user_oidc`` maps onto the Nextcloud account.

Anything we cannot verify is refused: a grant that fails the check is never
stored, and the app password it produced is revoked so nobody keeps it.
"""

import logging
from typing import Any

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.http import nextcloud_httpx_client

logger = logging.getLogger(__name__)

_OCS_HEADERS = {"OCS-APIRequest": "true"}
_TIMEOUT = 10.0

# Userinfo endpoint per discovery URL. It does not change while the process
# runs, and this path is only reached in external-IdP mode.
_userinfo_endpoints: dict[str, str] = {}


def _discovery_url() -> str | None:
    """OIDC discovery URL, mirroring how ``app.py`` derives it at startup."""
    settings = get_settings()
    if settings.oidc_discovery_url:
        return settings.oidc_discovery_url
    host = settings.nextcloud_host
    return f"{host.rstrip('/')}/.well-known/openid-configuration" if host else None


async def _userinfo_endpoint() -> str | None:
    discovery_url = _discovery_url()
    if not discovery_url:
        return None
    if discovery_url in _userinfo_endpoints:
        return _userinfo_endpoints[discovery_url]
    try:
        async with nextcloud_httpx_client(timeout=_TIMEOUT) as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            endpoint = response.json().get("userinfo_endpoint")
    except Exception as e:
        logger.warning("Could not read userinfo_endpoint from %s: %s", discovery_url, e)
        return None
    if not isinstance(endpoint, str) or not endpoint:
        return None
    _userinfo_endpoints[discovery_url] = endpoint
    return endpoint


async def caller_identities(user_id: str, access_token: str | None = None) -> set[str]:
    """Casefolded Nextcloud identifiers the OAuth caller may legitimately be.

    Always contains the OAuth ``sub``. In external-IdP deployments ``sub`` is an
    opaque UUID, so ``preferred_username`` from the IdP's userinfo endpoint is
    added when an access token is available — that is the claim ``user_oidc``
    maps onto the Nextcloud account.
    """
    identities = {user_id.casefold()}
    if not access_token:
        return identities

    endpoint = await _userinfo_endpoint()
    if not endpoint:
        return identities

    try:
        async with nextcloud_httpx_client(timeout=_TIMEOUT) as client:
            response = await client.get(
                endpoint, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            claims: Any = response.json()
    except Exception as e:
        logger.warning("Could not query IdP userinfo for caller identity: %s", e)
        return identities

    if isinstance(claims, dict):
        for claim in ("sub", "preferred_username"):
            value = claims.get(claim)
            if isinstance(value, str) and value:
                identities.add(value.casefold())
    return identities


async def _ocs_whoami(login_name: str, app_password: str) -> str | None:
    """Canonical Nextcloud UID a credential authenticates as, else ``None``."""
    host = get_settings().nextcloud_host
    if not host:
        return None
    try:
        async with nextcloud_httpx_client(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{host.rstrip('/')}/ocs/v2.php/cloud/user",
                auth=(login_name, app_password),
                params={"format": "json"},
                headers=_OCS_HEADERS,
            )
            response.raise_for_status()
            payload: Any = response.json()
    except Exception as e:
        logger.warning("Could not resolve Nextcloud UID for a Login Flow grant: %s", e)
        return None

    ocs = payload.get("ocs") if isinstance(payload, dict) else None
    data = ocs.get("data") if isinstance(ocs, dict) else None
    uid = data.get("id") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) and uid else None


async def revoke_app_password(login_name: str, app_password: str) -> None:
    """Delete an app password using the password itself (best effort).

    Called when a grant is refused, so a credential the server will not use does
    not linger in the granting account.
    """
    host = get_settings().nextcloud_host
    if not host:
        return
    try:
        async with nextcloud_httpx_client(timeout=_TIMEOUT) as client:
            await client.delete(
                f"{host.rstrip('/')}/ocs/v2.php/core/apppassword",
                auth=(login_name, app_password),
                headers=_OCS_HEADERS,
            )
    except Exception as e:
        logger.warning("Could not revoke the refused Login Flow app password: %s", e)


async def grant_belongs_to_caller(
    identities: set[str],
    login_name: str | None,
    app_password: str,
) -> bool:
    """True when the account that completed the flow is the OAuth caller.

    Fails closed: an unauthenticatable credential, a Nextcloud we cannot reach,
    or a missing ``loginName`` all return ``False``.
    """
    if not login_name:
        logger.warning("Login Flow v2 grant carried no loginName; refusing to store it")
        return False

    granter_uid = await _ocs_whoami(login_name, app_password)
    if not granter_uid:
        return False

    if granter_uid.casefold() in identities:
        return True

    # Deliberately identifier-free: which account granted, and which one asked,
    # are both user identities and this line lands in shared log aggregation.
    logger.warning(
        "Login Flow v2 grant was completed by a different Nextcloud account "
        "than the OAuth caller who started it; refusing to store it "
        "(GHSA-84qv-22q6-x82r)"
    )
    return False
