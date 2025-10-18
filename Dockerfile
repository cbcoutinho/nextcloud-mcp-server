FROM ghcr.io/astral-sh/uv:0.9.4-python3.11-alpine@sha256:a77a52d51f4382049d92547279040c1154e296bf2da8a380da90e28587195789

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
