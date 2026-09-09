"""Ownership check for completed Login Flow v2 grants (GHSA-84qv-22q6-x82r).

Login Flow v2 hands out a *transferable* URL: whoever opens it and clicks
"Grant access" produces the app password, and nothing in the flow itself ties
that person to the OAuth caller who started it. Storing the result under the
caller's identity without checking hands an attacker the victim's Nextcloud
credential after a single click.

Both sides are resolved the same way — by asking Nextcloud who a credential
authenticates as (OCS ``/cloud/user``, the move ``api/passwords.py`` makes for
GHSA-x88r-fhx7-52h6) — and the canonical UIDs it returns are compared:

* the **granter** is authenticated with the fresh app password. This maps the
  Login Flow ``loginName`` (which may be an email alias, or an LDAP login that
  differs from the UID — GH #980) onto the account Nextcloud will act as.
* the **caller** is authenticated with their own OAuth bearer token. Only
  Nextcloud can answer this one: with ``user_oidc``'s ``--unique-uid`` (the
  documented external-IdP setup, and what this repo's own Keycloak hook
  configures) the UID is a *hash* of the IdP ``sub``, so no claim in the token
  equals it. The OAuth ``sub`` is accepted alongside whatever the lookup
  returns, since it is itself the UID when Nextcloud is the IdP.

An IdP-supplied ``preferred_username`` is deliberately *not* accepted as an
identity: it is a claim the IdP — and on some IdPs the user themselves —
controls, so honouring it would let an attacker name the victim's UID as their
own and walk straight back into the advisory. External-IdP deployments
therefore need Nextcloud to accept the IdP's bearer tokens
(``user_oidc --check-bearer=1``), which ADR-002 already requires.

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


async def _ocs_whoami(
    *,
    auth: tuple[str, str] | None = None,
    bearer: str | None = None,
) -> str | None:
    """Canonical Nextcloud UID a credential authenticates as, else ``None``."""
    host = get_settings().nextcloud_host
    if not host:
        return None

    headers = dict(_OCS_HEADERS)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        async with nextcloud_httpx_client(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{host.rstrip('/')}/ocs/v2.php/cloud/user",
                auth=auth,
                params={"format": "json"},
                headers=headers,
            )
            response.raise_for_status()
            payload: Any = response.json()
    except Exception as e:
        logger.warning(
            "Could not resolve a Nextcloud UID for a Login Flow grant: %s", e
        )
        return None

    ocs = payload.get("ocs") if isinstance(payload, dict) else None
    data = ocs.get("data") if isinstance(ocs, dict) else None
    uid = data.get("id") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) and uid else None


async def caller_identities(user_id: str, access_token: str | None = None) -> set[str]:
    """Casefolded Nextcloud identifiers the OAuth caller may legitimately be.

    Always contains the OAuth ``sub`` — that *is* the UID when Nextcloud is the
    IdP. With an external IdP it is not, and the UID cannot be derived from the
    token at all (``user_oidc --unique-uid`` hashes the ``sub``), so Nextcloud
    is also asked directly, using the caller's own bearer token. That lookup
    runs for every token, native IdP included: telling the two deployments apart
    here would cost more than the one request a completed grant makes.
    """
    identities = {user_id.casefold()}
    if not access_token:
        return identities

    caller_uid = await _ocs_whoami(bearer=access_token)
    if caller_uid:
        identities.add(caller_uid.casefold())
    return identities


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

    granter_uid = await _ocs_whoami(auth=(login_name, app_password))
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
