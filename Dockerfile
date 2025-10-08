FROM ghcr.io/astral-sh/uv:0.9.0-python3.11-alpine@sha256:8d304012855f0ef78c67ed1970fa5744adcd9514c967ec3263a520b8d18d7344

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
