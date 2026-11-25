#!/usr/bin/env python3
"""Refresh nextcloud_mcp_server/server/vendor/pdfjs/ from the pins in the viewer.

The cdnjs <script> tags in document_viewer.html are the pin: Renovate's `html`
manager bumps their URLs. This downloads each pinned file, checks it against
the tag's `integrity` hash, writes it into vendor/pdfjs/, and removes files no
tag pins any more.

cdnjs publishes no SRI hashes for pdf.js 4+, so a Renovate bump changes the
URL but cannot update `integrity`. On such a PR, pass --repin: the hash is
recomputed from the downloaded file and written back into the tag, where it
shows up in the PR diff. Without --repin a mismatch is an error.

    uv run python scripts/sync_pdfjs.py [--repin]
"""

import argparse
import sys
import urllib.request

from nextcloud_mcp_server.server.document_viewer import (
    _SCRIPT_TAG,
    _VENDOR_DIR,
    _VIEWER_FILE,
    sri_digest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repin",
        action="store_true",
        help="rewrite each tag's integrity hash to match what cdnjs serves",
    )
    args = parser.parse_args()

    html = _VIEWER_FILE.read_text()
    pins = list(_SCRIPT_TAG.finditer(html))
    if not pins:
        print(f"no pinned cdnjs <script> tags in {_VIEWER_FILE}", file=sys.stderr)
        return 1
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
            html = html.replace(m["sri"], digest)
            print(f"{m['name']}: repinned {m['sri']} -> {digest}")
        (_VENDOR_DIR / m["name"]).write_bytes(data)
        print(f"{m['name']}: {len(data)} bytes, {digest}")
    _VIEWER_FILE.write_text(html)
    pinned = {m["name"] for m in pins}
    for stale in [*_VENDOR_DIR.glob("*.js"), *_VENDOR_DIR.glob("*.mjs")]:
        if stale.name not in pinned:
            stale.unlink()
            print(f"removed {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
