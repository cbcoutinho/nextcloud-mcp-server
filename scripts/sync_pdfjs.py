#!/usr/bin/env python3
"""Refresh nextcloud_mcp_server/server/vendor/pdfjs/ from the pins in the viewer.

The cdnjs <script> tags in document_viewer.html are the pin: Renovate's `html`
manager bumps their URLs. This downloads each pinned file, checks it against
the tag's `integrity` hash, writes it into vendor/pdfjs/, and removes files no
tag pins any more.

cdnjs publishes no SRI hashes for pdf.js 4+, so a Renovate bump changes the
URL but cannot update `integrity`. --repin recomputes the hash from the
downloaded file and writes it back into the tag, where it shows up in the PR
diff. The `pdfjs-repin` workflow runs this on Renovate's pdf.js branches.
Without --repin a mismatch is an error.

Standard library only, and paths are relative to this checkout, so CI can run
it with a bare `python3` (the installed package would point at site-packages).

    python3 scripts/sync_pdfjs.py [--repin]
"""

import argparse
import base64
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "nextcloud_mcp_server" / "server"
VIEWER_FILE = SERVER_DIR / "document_viewer.html"
VENDOR_DIR = SERVER_DIR / "vendor" / "pdfjs"
# Must equal document_viewer._SCRIPT_TAG (a unit test checks).
SCRIPT_TAG = re.compile(
    r'<script\b[^>]*?\bsrc="(?P<url>https://cdnjs\.cloudflare\.com/[^"]*/(?P<name>[\w.-]+\.m?js))"'
    r'[^>]*?\bintegrity="(?P<sri>(?P<alg>sha256|sha384|sha512)-[A-Za-z0-9+/=]+)"[^>]*></script>'
)


def sri_digest(alg: str, data: bytes) -> str:
    return f"{alg}-" + base64.b64encode(hashlib.new(alg, data).digest()).decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repin",
        action="store_true",
        help="rewrite each tag's integrity hash to match what cdnjs serves",
    )
    args = parser.parse_args()

    html = VIEWER_FILE.read_text()
    pins = list(SCRIPT_TAG.finditer(html))
    if not pins:
        print(f"no pinned cdnjs <script> tags in {VIEWER_FILE}", file=sys.stderr)
        return 1
    # Download and check every file before writing any, so a failed download or
    # a mismatch leaves the vendored files and the HTML pins consistent.
    fetched: list[tuple[str, bytes]] = []
    for m in pins:
        with urllib.request.urlopen(m["url"], timeout=60) as resp:  # noqa: S310 (https cdnjs URL)
            data = resp.read()
        digest = sri_digest(m["alg"], data)
        if digest != m["sri"]:
            if not args.repin:
                print(
                    f"{m['url']}: got {digest}, pinned {m['sri']} (pass --repin after a version bump)",
                    file=sys.stderr,
                )
                return 1
            # Replacing by hash string is safe: a pin's hash occurs only in its own tag.
            html = html.replace(m["sri"], digest)
            print(f"{m['name']}: repinned {m['sri']} -> {digest}")
        # The server inlines these into a <script> body, which "</script" would end.
        if b"</script" in data.lower():
            print(
                f"{m['url']}: contains '</script', cannot be inlined", file=sys.stderr
            )
            return 1
        fetched.append((m["name"], data))
        print(f"{m['name']}: {len(data)} bytes, {digest}")
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in fetched:
        (VENDOR_DIR / name).write_bytes(data)
    VIEWER_FILE.write_text(html)
    pinned = {m["name"] for m in pins}
    for stale in [*VENDOR_DIR.glob("*.js"), *VENDOR_DIR.glob("*.mjs")]:
        if stale.name not in pinned:
            stale.unlink()
            print(f"removed {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
