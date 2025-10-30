FROM ghcr.io/astral-sh/uv:0.9.6-python3.11-alpine@sha256:b2a366adae7002a23dbba79791baac4e607ee5af5d45039d072d30115c505666

# Install git (required for caldav dependency from git)
RUN apk add --no-cache git

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
