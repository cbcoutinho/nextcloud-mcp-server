FROM docker.io/library/python:3.12-slim-trixie@sha256:b43ff04d5df04ad5cabb80890b7ef74e8410e3395b19af970dcd52d7a4bff921

COPY --from=ghcr.io/astral-sh/uv:0.9.10@sha256:29bd45092ea8902c0bbb7f0a338f0494a382b1f4b18355df5be270ade679ff1d /uv /uvx /bin/

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    git \
    sqlite3 && apt clean

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev --no-editable --no-cache

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
