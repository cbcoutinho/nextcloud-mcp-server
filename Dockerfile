FROM ghcr.io/astral-sh/uv:0.8.11-python3.11-alpine@sha256:15785547766d048c6caf6b4168b1374a14de2458d0da56b3b3308a14e4e7f7ad

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

CMD ["/app/.venv/bin/mcp", "run", "--transport", "sse", "/app/nextcloud_mcp_server/app.py:mcp"]
