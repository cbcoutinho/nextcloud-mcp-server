# pdf.js (vendored)

Unmodified copies of the pdf.js files pinned by the cdnjs `<script>` tags in
`../../document_viewer.html` (currently `pdf.min.mjs` and `pdf.worker.min.mjs`;
Apache-2.0, Mozilla Foundation).

`document_viewer.py` inlines them into the `open_document` viewer in place of
the cdnjs `<script src>` tags in `document_viewer.html`, after checking each
file against that tag's `integrity` hash. They are inlined because hosts do not
reliably honour `_meta.ui.csp.resourceDomains`: claude.ai serves MCP App views
with `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:` and blocks
cdnjs.

Do not edit these files. Renovate's `html` manager bumps the URLs in
`document_viewer.html`. Then run

    uv run python scripts/sync_pdfjs.py --repin

which downloads the pinned files, rewrites the `integrity` hashes (cdnjs
publishes none for pdf.js 4+, so Renovate cannot) and replaces these copies.
Without `--repin` it only verifies and re-downloads.
