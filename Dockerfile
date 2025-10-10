FROM ghcr.io/astral-sh/uv:0.9.2-python3.11-alpine@sha256:59c7cb3e4a4fe9ccff6a5bf0d952a0b1b0101adda48e305c02beea3c22256208

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
