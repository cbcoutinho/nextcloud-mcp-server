FROM ghcr.io/astral-sh/uv:python3.11-alpine@sha256:20ee214166393ad07a950e4f69d84282ffa47635c47437e9b7c8dcaa8982fd54

WORKDIR /app

COPY . .

RUN uv sync --locked

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV FASTMCP_LOG_LEVEL=DEBUG

CMD ["mcp", "run", "--transport", "sse", "nextcloud_mcp_server/server.py:mcp"]
