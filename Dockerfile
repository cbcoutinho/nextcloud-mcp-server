FROM ghcr.io/astral-sh/uv:0.9.4-python3.11-alpine@sha256:1a51c7710eaf839fa3365329ad993b48d17ddd9ab0f0672efaa9b09f407ebf44

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
