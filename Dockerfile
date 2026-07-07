FROM docker.io/library/python:3.12-slim-trixie@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

COPY --from=ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419 /uv /uvx /bin/

# Install dependencies
# 1. curl (required for container healthcheck probes)
# 2. git (required for caldav dependency from git)
# 3. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    curl \
    git \
    tesseract-ocr \
    sqlite3 && apt clean

WORKDIR /app

COPY pyproject.toml uv.lock README.md .

RUN uv sync --locked --no-dev --no-install-project --no-cache --extra postgres

COPY . .

RUN uv sync --locked --no-dev --no-editable --no-cache --extra postgres

ENV PYTHONUNBUFFERED=1
# Dump a Python + C-level traceback to stderr on a fatal native fault
# (SIGSEGV/SIGABRT/SIGFPE/SIGBUS). In-process native code -- pymupdf's
# classify/metadata open and embedded Qdrant -- can segfault the interpreter
# during indexing, and without faulthandler the container just exits 139/133
# with no logs (see issue #926). The handler is cheap and writes nothing in
# normal operation.
ENV PYTHONFAULTHANDLER=1
ENV VIRTUAL_ENV=/app/.venv
ENV PATH=/app/.venv/bin:$PATH
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "run", "--host", "0.0.0.0"]
