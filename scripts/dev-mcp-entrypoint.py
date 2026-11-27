"""Dev/CI entrypoint for the single-user ``mcp`` compose service.

Swaps the login password in NEXTCLOUD_PASSWORD for an app password, then
starts the server. NextcloudClient strips cookies, so Nextcloud re-verifies a
login password on every request: about 0.26 s even with the cheap dev hashing
cost, against about 0.06 s for an app password. The server makes most of the
integration lanes' Nextcloud requests, so this is the largest cost left.

The app password is cached in the mcp-data volume and reused while it still
authenticates, so restarts do not pile up tokens. Any failure keeps the login
password: the stack still works, only slower.

Dev/CI only. A real deployment configures an app password directly.
Stdlib only, so it runs on the image's venv Python with nothing installed.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

# Fixed, not taken from argv: the compose service is this script's only caller.
SERVER = [
    "/opt/venv/bin/nextcloud-mcp-server",
    "run",
    "--host",
    "0.0.0.0",
    "--transport",
    "streamable-http",
]
CACHE = "/app/data/nextcloud-app-password"


def _get(path: str, password: str) -> dict:
    """GET an OCS path as NEXTCLOUD_USERNAME; raises HTTPError on 4xx/5xx."""
    user = os.environ["NEXTCLOUD_USERNAME"]
    basic = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(
        os.environ["NEXTCLOUD_HOST"].rstrip("/") + path + "?format=json",
        headers={
            "Authorization": f"Basic {basic}",
            "OCS-APIRequest": "true",
            # Nextcloud names the app password after the user agent.
            "User-Agent": "nextcloud-mcp-server dev compose",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def app_password(login_password: str) -> str:
    """The cached app password if it still works, else a freshly issued one."""
    try:
        with open(CACHE) as f:
            cached = f.read().strip()
    except FileNotFoundError:
        cached = ""
    if cached:
        try:
            _get("/ocs/v2.php/cloud/user", cached)
            return cached
        except urllib.error.HTTPError as e:
            # Revoked, or from an old Nextcloud volume: reissue.
            if e.code not in (401, 403):
                raise
    # 403 when NEXTCLOUD_PASSWORD already is an app password (handled by main).
    token = _get("/ocs/v2.php/core/getapppassword", login_password)["ocs"]["data"][
        "apppassword"
    ]
    try:
        fd = os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
    except OSError as e:  # still use the token; the next start issues another
        print(
            f"dev entrypoint: could not cache the app password ({e!r})", file=sys.stderr
        )
    return token


def main() -> None:
    try:
        os.environ["NEXTCLOUD_PASSWORD"] = app_password(
            os.environ["NEXTCLOUD_PASSWORD"]
        )
        print("dev entrypoint: using a Nextcloud app password", file=sys.stderr)
    except (OSError, KeyError, ValueError, TypeError) as e:
        print(
            f"dev entrypoint: keeping NEXTCLOUD_PASSWORD as given ({e!r})",
            file=sys.stderr,
        )
    os.execv(SERVER[0], SERVER)


if __name__ == "__main__":
    main()
