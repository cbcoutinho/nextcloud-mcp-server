FROM ghcr.io/astral-sh/uv:0.9.4-python3.11-alpine@sha256:4992e5c63570a6f5c7c3195fdf98099dc82b8874dea425f3d0a3b98437cbd969

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
