FROM docker.io/library/python:3.12-slim-trixie@sha256:b43ff04d5df04ad5cabb80890b7ef74e8410e3395b19af970dcd52d7a4bff921

COPY --from=ghcr.io/astral-sh/uv:0.9.14@sha256:fef8e5fb8809f4b57069e919ffcd1529c92b432a2c8d8ad1768087b0b018d840 /uv /uvx /bin/

# Install dependencies
# 1. git (required for caldav dependency from git)
# 2. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    git \
    tesseract-ocr \
    sqlite3 && apt clean

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev --no-editable --no-cache

ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv
ENV PATH=/app/.vnev/bin:$PATH
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
