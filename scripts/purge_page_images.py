#!/usr/bin/env python3
"""Purge legacy `highlighted_page_image` payloads from Qdrant (Deck #76).

Iterates all points in the configured Qdrant collection and deletes the
legacy payload keys `highlighted_page_image`, `highlighted_page_number`,
and `highlight_count`. This relieves disk pressure caused by inline
base64 PNGs that the new code path no longer writes.

Idempotent: deleting non-existent keys is a no-op, so re-runs are safe.

Usage:
    uv run python scripts/purge_page_images.py [--dry-run] [--batch-size 256]

Connection settings (Qdrant URL/API key, collection name) are read from
the same `Settings` object the server uses.
"""

from __future__ import annotations

import argparse
import logging
import sys
from functools import partial

import anyio
from qdrant_client import AsyncQdrantClient

from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger("purge_page_images")

LEGACY_FIELDS = [
    "highlighted_page_image",
    "highlighted_page_number",
    "highlight_count",
]


async def purge(dry_run: bool, batch_size: int) -> None:
    settings = get_settings()
    if not settings.qdrant_url:
        raise SystemExit(
            "qdrant_url is not configured. Set QDRANT_URL (and QDRANT_API_KEY "
            "if required) before running this script."
        )
    collection = settings.get_collection_name()

    # AsyncQdrantClient doesn't implement __aenter__/__aexit__, so use
    # try/finally to guarantee the underlying aiohttp session is closed.
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=60,
    )
    try:
        next_offset = None
        total_seen = 0
        total_updated = 0

        logger.info(
            "Scanning collection %s; will delete keys %s%s",
            collection,
            LEGACY_FIELDS,
            " (dry run)" if dry_run else "",
        )

        while True:
            points, next_offset = await client.scroll(
                collection_name=collection,
                limit=batch_size,
                offset=next_offset,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                break

            ids = [p.id for p in points]
            total_seen += len(ids)

            if not dry_run:
                await client.delete_payload(
                    collection_name=collection,
                    keys=LEGACY_FIELDS,
                    points=ids,
                )
                total_updated += len(ids)

            logger.info(
                "Batch: ids=%d total_seen=%d total_updated=%d",
                len(ids),
                total_seen,
                total_updated,
            )

            if next_offset is None:
                break

        logger.info(
            "Done. total_seen=%d total_updated=%d%s",
            total_seen,
            total_updated,
            " (dry run, no writes)" if dry_run else "",
        )
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only; do not write any changes.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Points per scroll/update batch (default: 256).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    anyio.run(partial(purge, dry_run=args.dry_run, batch_size=args.batch_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
