#!/usr/bin/env python3
"""Refresh nextcloud_mcp_server/server/vendor/pdfjs/ from the pins in the viewer.

The cdnjs <script> tags in document_viewer.html are the pin: Renovate's `html`
manager bumps their URLs. This downloads each pinned file, checks it against
the tag's `integrity` hash, writes it into vendor/pdfjs/, and removes files no
tag pins any more.

The viewer also pins pdf.js's wasm image decoders (JPEG 2000, JBIG2) with
`<script type="application/wasm" data-src=…>` tags. cdnjs doesn't carry them,
so they come from the pdfjs-dist npm package on jsDelivr, which Renovate's html
manager doesn't track. Their version is therefore not a pin of its own: --repin
rewrites it to the version of the cdnjs pdf.js tags, so a pdf.js bump moves the
decoders with it.

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
# Must equal document_viewer._WASM_TAG (a unit test checks).
WASM_TAG = re.compile(
    r'<script type="application/wasm" data-src="(?P<url>https://cdn\.jsdelivr\.net/npm/'
    r'pdfjs-dist@(?P<ver>[\w.-]+)/wasm/(?P<name>[\w.-]+\.wasm))"'
    r' integrity="(?P<sri>(?P<alg>sha256|sha384|sha512)-[A-Za-z0-9+/=]+)"></script>'
)
PDFJS_VERSION = re.compile(r"/pdf\.js/(?P<ver>[\w.-]+)/")


def sri_digest(alg: str, data: bytes) -> str:
    return f"{alg}-" + base64.b64encode(hashlib.new(alg, data).digest()).decode()


def follow_pdfjs_version(html: str) -> str:
    """Point every wasm pin at the pdfjs-dist version of the cdnjs pdf.js tags."""
    versions = {
        v["ver"]
        for m in SCRIPT_TAG.finditer(html)
        if (v := PDFJS_VERSION.search(m["url"]))
    }
    if len(versions) != 1:
        raise SystemExit(
            f"expected one pdf.js version in the cdnjs tags, found {versions}"
        )
    (version,) = versions
    return WASM_TAG.sub(
        lambda m: m[0].replace(f"pdfjs-dist@{m['ver']}/", f"pdfjs-dist@{version}/"),
        html,
    )


def fetch_pinned(m: re.Match[str], repin: bool) -> tuple[bytes, str]:
    """Download one pinned file and check it; return it with its digest.

    Exits on a hash mismatch (unless `repin`) or a script that can't be inlined.
    """
    with urllib.request.urlopen(m["url"], timeout=60) as resp:  # noqa: S310 (pinned https CDN URL)
        data = resp.read()
    digest = sri_digest(m["alg"], data)
    if digest != m["sri"] and not repin:
        raise SystemExit(
            f"{m['url']}: got {digest}, pinned {m['sri']} (pass --repin after a version bump)"
        )
    # The server inlines scripts into a <script> body, which "</script" would
    # end. wasm is embedded as base64, which can contain neither.
    is_script = m["name"].endswith((".js", ".mjs"))
    if is_script and (b"</script" in data.lower() or b"<!--" in data):
        raise SystemExit(
            f"{m['url']}: contains '</script' or '<!--', cannot be inlined"
        )
    print(f"{m['name']}: {len(data)} bytes, {digest}")
    return data, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repin",
        action="store_true",
        help="rewrite each tag's integrity hash to match what the CDN serves",
    )
    args = parser.parse_args()

    html = VIEWER_FILE.read_text()
    if not SCRIPT_TAG.search(html):
        print(f"no pinned cdnjs <script> tags in {VIEWER_FILE}", file=sys.stderr)
        return 1
    followed = follow_pdfjs_version(html)
    if followed != html and not args.repin:
        print(
            "the wasm decoder pins lag the pdf.js version (pass --repin after a version bump)",
            file=sys.stderr,
        )
        return 1
    html = followed
    pins = [*SCRIPT_TAG.finditer(html), *WASM_TAG.finditer(html)]
    # Download and check every file before writing any, so a failed download or
    # a mismatch leaves the vendored files and the HTML pins consistent.
    fetched: list[tuple[str, bytes]] = []
    for m in pins:
        data, digest = fetch_pinned(m, args.repin)
        if digest != m["sri"]:
            # Replacing by hash string is safe: a pin's hash occurs only in its own tag.
            html = html.replace(m["sri"], digest)
            print(f"{m['name']}: repinned {m['sri']} -> {digest}")
        fetched.append((m["name"], data))
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in fetched:
        (VENDOR_DIR / name).write_bytes(data)
    VIEWER_FILE.write_text(html)
    pinned = {m["name"] for m in pins}
    for pattern in ("*.js", "*.mjs", "*.wasm"):
        for stale in VENDOR_DIR.glob(pattern):
            if stale.name not in pinned:
                stale.unlink()
                print(f"removed {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
