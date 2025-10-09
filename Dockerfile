FROM ghcr.io/astral-sh/uv:0.9.1-python3.11-alpine@sha256:c916d811124ace1edc7a7fe1f541ff48ca5a1a72ebe2b968ce49653cb2d9e82a

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
