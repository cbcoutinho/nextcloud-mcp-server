"""Canned stand-in for the embedding gateway's ``POST /v1/ner`` (ADR-040).

Served by the ``ner-stub`` compose service so the integration tier exercises the
real redaction flow without a model. Stdlib only, so it runs on a bare
``python`` image. It "detects" exactly the synthetic names and addresses below,
at every occurrence, for the labels requested, and returns them in the
gateway's wire format.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANNED = {
    "person": ("Jane Doe", "Karen Smith", "Tom Brown"),
    "address": ("14 Mill Lane",),
}


def entities(text: str, labels: list[str]) -> list[dict]:
    found = []
    for label in labels:
        for surface in CANNED.get(label, ()):
            start = text.find(surface)
            while start != -1:
                end = start + len(surface)
                found.append(
                    {
                        "start": start,
                        "end": end,
                        "text": surface,
                        "label": label,
                        "score": 0.99,
                    }
                )
                start = text.find(surface, end)
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
                    {"index": i, "entities": entities(t, body["labels"])}
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
