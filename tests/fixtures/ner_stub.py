"""Canned stand-in for the embedding gateway's ``POST /v1/ner`` (ADR-038).

Served by the ``ner-stub`` compose service so the integration tier exercises the
real redaction flow without a model. Stdlib only, so it runs on a bare
``python`` image. It "detects" exactly the synthetic names below, at every
occurrence, and returns them in the gateway's wire format.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAMES = ("Jane Doe", "Karen Smith", "Tom Brown")


def entities(text: str) -> list[dict]:
    found = []
    for name in NAMES:
        start = text.find(name)
        while start != -1:
            end = start + len(name)
            found.append(
                {
                    "start": start,
                    "end": end,
                    "text": name,
                    "label": "person",
                    "score": 0.99,
                }
            )
            start = text.find(name, end)
    return found


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        if self.path.rstrip("/") != "/v1/ner":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.dumps(
            {
                "results": [
                    {"index": i, "entities": entities(t)}
                    for i, t in enumerate(body["texts"])
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
