"""The Full Text Search tool against live apps with Elasticsearch.

Gated on ``FULLTEXTSEARCH_TESTS``. Run the stack with the "fulltextsearch" profile,
whose post-installation hook sets up the apps, and set the variable in this process:

    docker compose --profile single-user --profile fulltextsearch up -d --build
    FULLTEXTSEARCH_TESTS=1 uv run pytest -m fulltextsearch
"""

import json
import os
import subprocess
import uuid
from typing import NamedTuple

import pytest
from mcp.client.session import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.fulltextsearch,
    pytest.mark.skipif(
        not os.getenv("FULLTEXTSEARCH_TESTS"), reason="needs the fulltextsearch profile"
    ),
]

TOOL = "nc_fulltextsearch_search_files"


class Words(NamedTuple):
    alpha: str
    beta: str


def _index_files() -> None:
    subprocess.run(
        [
            "docker", "compose", "exec", "-T", "--user", "www-data", "app",
            "php", "occ", "fulltextsearch:index", json.dumps({"provider": "files"}),
        ],
        check=True,
        capture_output=True,
        timeout=170,
    )  # fmt: skip


@pytest.fixture(scope="module")
async def indexed(nc_client: NextcloudClient):
    """Upload files with unique words, index them, and remove them afterwards."""
    run = uuid.uuid4().hex
    words = Words(f"alpha{run}", f"beta{run}")
    folder = f"fts-{run}"
    await nc_client.webdav.create_directory(folder)
    files = {
        f"{folder}/both.txt": f"{words.alpha} {words.beta}",
        f"{folder}/alpha-only.txt": f"Some text with the word {words.alpha} in it.",
        f"{folder}/UPPER.MD": f"{words.alpha}",
        f"{folder}/{words.beta}.txt": "The name matches, the content does not.",
    }
    for path, content in files.items():
        await nc_client.webdav.write_file(path, content.encode(), "text/plain")
    _index_files()
    yield folder, words
    await nc_client.webdav.delete_resource(folder)


async def _search(session: ClientSession, **arguments) -> dict:
    result = await session.call_tool(TOOL, arguments)
    assert result.is_error is not True, result.content
    return result.structured_content


def _names(answer: dict) -> set[str]:
    return {hit["path"].rsplit("/", 1)[-1] for hit in answer["results"]}


async def test_finds_files_by_content(nc_mcp_client, indexed):
    folder, words = indexed

    answer = await _search(nc_mcp_client, query=words.alpha)

    assert answer["total"] == 3
    assert _names(answer) == {"both.txt", "alpha-only.txt", "UPPER.MD"}
    hit = next(h for h in answer["results"] if h["path"].endswith("alpha-only.txt"))
    assert hit["path"] == f"{folder}/alpha-only.txt"
    assert hit["mime_type"] == "text/plain"
    assert any(words.alpha in excerpt for excerpt in hit["excerpts"])


async def test_required_and_excluded_words(nc_mcp_client, indexed):
    _, words = indexed

    both = await _search(nc_mcp_client, query=f"+{words.alpha} +{words.beta}")
    without = await _search(nc_mcp_client, query=f"+{words.alpha} -{words.beta}")

    assert _names(both) == {"both.txt"}
    assert _names(without) == {"alpha-only.txt", "UPPER.MD"}


async def test_extension_matches_either_case(nc_mcp_client, indexed):
    _, words = indexed

    answer = await _search(nc_mcp_client, query=words.alpha, extension="md")

    assert _names(answer) == {"UPPER.MD"}


async def test_match_limits_where_to_look(nc_mcp_client, indexed):
    _, words = indexed

    in_name = await _search(nc_mcp_client, query=words.beta, match="filename")
    in_content = await _search(nc_mcp_client, query=words.beta, match="content")

    assert _names(in_name) == {f"{words.beta}.txt"}
    assert _names(in_content) == {"both.txt"}


async def test_pages(nc_mcp_client, indexed):
    _, words = indexed

    first = await _search(nc_mcp_client, query=words.alpha, limit=2)
    second = await _search(nc_mcp_client, query=words.alpha, limit=2, page=2)

    assert (first["total"], first["has_more"], len(first["results"])) == (3, True, 2)
    assert (second["has_more"], len(second["results"])) == (False, 1)
    assert _names(first) | _names(second) == {"both.txt", "alpha-only.txt", "UPPER.MD"}


async def test_no_hits(nc_mcp_client, indexed):
    answer = await _search(nc_mcp_client, query=f"missing{uuid.uuid4().hex}")

    assert (answer["total"], answer["results"], answer["note"]) == (0, [], None)
