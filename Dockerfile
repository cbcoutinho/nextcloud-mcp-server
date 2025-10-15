FROM ghcr.io/astral-sh/uv:0.9.3-python3.11-alpine@sha256:c5c8e9241027c384aa5e0d0368a6fd013945a23b7a5f25c754ed55ea7ef64f92

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
