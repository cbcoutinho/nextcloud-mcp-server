"""The core server must build without the semantic-search stack installed.

Semantic search, document processing and SAR redaction pull in heavy native
dependencies (qdrant-client, fastembed, pymupdf, numpy, the LLM-vendor SDKs).
With ``VECTOR_SYNC_ENABLED`` off none of that code runs, so none of it may be
*imported* either — otherwise an install without those packages cannot start.

Each check runs in a fresh interpreter with an import hook that makes every
semantic-only dependency raise ``ModuleNotFoundError``, i.e. exactly what a
bare install looks like. A module-level import of one of them anywhere on the
core path fails the test with the offending import chain in the traceback.
"""

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

# Top-level distributions only reachable from the semantic/SAR feature set.
SEMANTIC_ONLY = (
    "boto3",
    "botocore",
    "docx",
    "fastembed",
    "mistralai",
    "numpy",
    "olefile",
    "openai",
    "openpyxl",
    "pptx",
    "pymupdf",
    "pymupdf4llm",
    "fitz",
    "pypdfium2",
    "qdrant_client",
)

_BLOCKER = textwrap.dedent(
    f"""
    import importlib.abc, sys

    BLOCKED = {SEMANTIC_ONLY!r}

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
            return None

    sys.meta_path.insert(0, _Block())
    """
)


def _run(body: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "NEXTCLOUD_HOST": "http://nextcloud.invalid",
        "NEXTCLOUD_USERNAME": "admin",
        "NEXTCLOUD_PASSWORD": "admin",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + textwrap.dedent(body)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_ok(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode:
        pytest.fail(proc.stderr[-4000:], pytrace=False)


def test_http_app_builds_without_semantic_stack():
    _assert_ok(
        _run(
            """
            import sys
            from starlette.testclient import TestClient
            from nextcloud_mcp_server.app import get_app

            # Entering the client runs the full lifespan, not just construction.
            with TestClient(get_app(transport="streamable-http")) as client:
                status = client.get("/api/v1/status")
                assert status.status_code == 200, status.text
                body = status.json()
                assert body["vector_sync_enabled"] is False
                assert body["sar_available"] is False
                assert client.get("/health/live").status_code == 200

            leaked = sorted(m for m in sys.modules if m.startswith((
                "nextcloud_mcp_server.vector.",
                "nextcloud_mcp_server.search.",
                "nextcloud_mcp_server.document_processors",
            )))
            assert not leaked, leaked
            """,
            {},
        )
    )


def test_stdio_server_builds_without_semantic_stack():
    _assert_ok(
        _run(
            """
            import anyio
            from nextcloud_mcp_server.stdio import get_stdio_mcp
            mcp = get_stdio_mcp()
            tools = anyio.run(mcp.list_tools)
            assert any(t.name == "nc_notes_get_note" for t in tools), tools
            assert not any(t.name == "nc_semantic_search" for t in tools)
            """,
            {},
        )
    )


def test_cli_help_without_semantic_stack():
    _assert_ok(
        _run(
            """
            import sys
            from nextcloud_mcp_server.cli import cli
            sys.argv = ["nextcloud-mcp-server", "--help"]
            try:
                cli()
            except SystemExit as exc:
                assert exc.code == 0, exc.code
            """,
            {},
        )
    )
