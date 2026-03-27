FROM docker.io/library/python:3.12-slim-trixie@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /uvx /bin/

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    git \
    tesseract-ocr \
    sqlite3 && apt clean

WORKDIR /app

COPY pyproject.toml uv.lock README.md .

RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY . .

RUN uv sync --locked --no-dev --no-editable --no-cache

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv
ENV PATH=/app/.venv/bin:$PATH
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "run", "--host", "0.0.0.0"]
