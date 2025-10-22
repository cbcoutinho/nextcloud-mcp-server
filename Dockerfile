FROM ghcr.io/astral-sh/uv:0.9.5-python3.11-alpine@sha256:64ecec379ff82bea84b8a80c0b374f6392bcd54aa52f8c63c12f510f9c0b214d

# Install git (required for caldav dependency from git)
RUN apk add --no-cache git

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
