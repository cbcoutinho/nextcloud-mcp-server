FROM ghcr.io/astral-sh/uv:0.9.7-python3.11-alpine@sha256:0006b77df7ebf46e68959fdc8d3af9d19f1adfae8c2e7e77907ad257e5d05be4

# Install git (required for caldav dependency from git)
RUN apk add --no-cache git

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
