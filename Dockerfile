FROM docker.io/library/python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /uvx /bin/

# Install dependencies
# 1. curl (required for container healthcheck probes)
# 2. git (required for caldav dependency from git)
# 3. sqlite for development with token db
RUN apt update && apt install --no-install-recommends --no-install-suggests -y \
    curl \
    git \
    tesseract-ocr \
    sqlite3 && apt clean

# Build in /src, run in /app, keep the venv in /opt/venv. The three have
# different lifetimes: /opt/venv is immutable code, /app is mutable runtime
# state (settings.toml, data/), /src is build-only. UV_PROJECT_ENVIRONMENT is
# what stops uv defaulting the environment to <project>/.venv.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

COPY pyproject.toml uv.lock README.md .

# --no-build: every third-party dependency must arrive as a wheel, so no
# dependency's setup.py executes at image-build time (docker:S8541). This is
# the sync that installs them all; the second one only adds the project itself,
# which by definition has to be built and cannot carry the flag.
RUN uv sync --locked --no-dev --no-install-project --no-build --no-cache --extra postgres --extra observability

COPY . .

RUN uv sync --locked --no-dev --no-editable --no-cache --extra postgres --extra observability

ENV PYTHONUNBUFFERED=1
# Dump a Python + C-level traceback to stderr on a fatal native fault
# (SIGSEGV/SIGABRT/SIGFPE/SIGBUS). In-process native code -- pymupdf's
# classify/metadata open and embedded Qdrant -- can segfault the interpreter
# during indexing, and without faulthandler the container just exits 139/133
# with no logs (see issue #926). The handler is cheap and writes nothing in
# normal operation.
ENV PYTHONFAULTHANDLER=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:$PATH
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Runtime directory: the settings.toml mount and data/ only. Pre-creating
# data/ with the right owner matters -- docker seeds a named volume's
# ownership from the image path, so a fresh volume comes up writable instead
# of root-owned 0755.
#
# A real account rather than a bare numeric USER: appuser gets a passwd
# entry, so getpwuid() resolves and HOME lands in /home/appuser instead of
# the base image's /root. No password and a nologin shell -- it is only ever
# switched to by the runtime, never logged into.
#
# The home directory is load-bearing, not cosmetic. /root is mode 0700
# root-owned, so a non-root process cannot even traverse it; huggingface_hub
# trips over that while resolving paths and fastembed's BM25 weight fetch
# dies with "Could not load model Qdrant/bm25 from any source", taking
# hybrid search's sparse vectors with it. Verified under a read-only root
# filesystem too, matching the chart's readOnlyRootFilesystem pods.
#
# uid 1000 / gid 0 reproduces what the helm chart already runs
# (runAsUser: 1000, no runAsGroup -> gid 0), so nothing about the pod
# changes. The venv stays root-owned: a compromised process parsing a
# hostile document cannot rewrite its own code.
RUN useradd --uid 1000 --gid 0 --create-home --home-dir /home/appuser \
    --shell /usr/sbin/nologin appuser \
 && passwd --lock appuser \
 && mkdir -p /app/data \
 && chown 1000:0 /app /app/data

WORKDIR /app
USER appuser

ENTRYPOINT ["/opt/venv/bin/nextcloud-mcp-server", "run", "--host", "0.0.0.0"]
