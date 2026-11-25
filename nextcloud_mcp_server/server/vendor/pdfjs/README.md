# pdf.js (vendored)

Unmodified copies of the pdf.js files pinned in `../../document_viewer.html`
(Apache-2.0, Mozilla Foundation):

- `pdf.min.mjs`, `pdf.worker.min.mjs`, pinned by the cdnjs `<script>` tags.
  `document_viewer.py` inlines them in place of those tags, after checking each
  file against the tag's `integrity` hash. They are inlined because hosts do not
  reliably honour `_meta.ui.csp.resourceDomains`: claude.ai serves MCP App views
  with `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:` and blocks
  cdnjs.
- `openjpeg.wasm` (JPEG 2000; OpenJPEG, BSD-2-Clause) and `jbig2.wasm` (JBIG2 and
  CCITT; PDFium, BSD-3-Clause), pinned by the `<script type="application/wasm">`
  data blocks. pdf.js 4+ has no JavaScript decoder for these image formats, which
  are what scanners write. Without the wasm, scanned pages render blank. The
  server embeds each file in its block as base64, and the viewer hands the bytes
  to pdf.js through a custom `BinaryDataFactory`. cdnjs does not carry them, so
  they come from the `pdfjs-dist` npm package on jsDelivr. Their licenses are the
  `LICENSE_*` files here.

Do not edit these files. Renovate's `html` manager bumps the cdnjs URLs in
`document_viewer.html`, and the `pdf.js repin` workflow
(`.github/workflows/pdfjs-repin.yml`) then runs

    python3 scripts/sync_pdfjs.py --repin

on Renovate's branch and commits the result. It moves the wasm pins to the same
pdf.js version, downloads the pinned files, rewrites the `integrity` hashes
(cdnjs publishes none for pdf.js 4+, so Renovate cannot) and replaces these
copies. Run it by hand only for a manual bump. Without `--repin` it just
verifies and re-downloads. The `LICENSE_*` files are not managed by the
script, so re-fetch them from `pdfjs-dist/wasm/` if the decoders change
license.
