FROM docker.io/library/python:3.12-slim-trixie@sha256:87b49ee9d18db77b0afc7e3adbd994acb9544695217f6e8b4ff352a076a9e6a6

COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /bin/

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
