import logging
import pathlib

import anyio
import pymupdf
import pymupdf.layout

from nextcloud_mcp_server.client import NextcloudClient

pymupdf.layout.activate()
import pymupdf4llm  # noqa: E402

client = NextcloudClient.from_env()
logger = logging.getLogger(__name__)

TMP_DIR = pathlib.Path("/tmp/tmp-images")
TMP_DIR.mkdir(exist_ok=True, parents=True)


async def print_markdown(filename):
    content, _ = await client.webdav.read_file(filename)
    doc = pymupdf.open("pdf", content)
    md_text = pymupdf4llm.to_markdown(doc, write_images=True, image_path=str(TMP_DIR))
    print(md_text)


async def run1():
    response = await client.webdav.find_by_type("application/pdf")
    # print(response)
    for file in response:
        await print_markdown(file["path"])


async def run():
    tags = await client.tags.get_all_tags()
    print(tags)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    anyio.run(run)
