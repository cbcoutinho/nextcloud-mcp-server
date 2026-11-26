"""One read of a doc type's points that answers the scanner's per-document lookups.

qdrant-client's local mode (``QDRANT_LOCATION``) keeps no payload indexes, so
every filtered ``scroll`` is a linear Python pass over the whole collection. The
tagged-file loop runs up to four such lookups per file (dedup claim,
dead-letter check, real + placeholder metadata), so a scan cost
O(files x points): ~85 s of a full core per cycle at ~1,200 files / ~9,500
points (GH #1941). The scanner instead reads the points once into an
:class:`IndexSnapshot` and the lookups answer from it.

Network mode keeps the per-document scrolls. They are index-backed there, and a
tenant-wide pass over a large collection every scan would cost more than it
saves.

Each map keeps the FIRST point seen for its key. That equals what the replaced
``scroll(..., limit=1)`` returned, because both modes scroll in ascending
point-id order.

The snapshot is taken once, before the loop. The scanner's own writes for a
file come after that file's lookups, so it never needs to see them. A
processor worker that finishes a file mid-scan is not seen either; the worst
case is one redundant enqueue, which the next scan corrects.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field

from qdrant_client.models import Record

from nextcloud_mcp_server.vector.dead_letter import DEAD_LETTER_KEY

# Per-chunk fields that none of the lookups read. Excluding them keeps the
# snapshot to metadata (``excerpt`` alone is up to chunk_size chars per point).
EXCLUDED_PAYLOAD_FIELDS = ["excerpt", "chunk_bbox", "bbox_source"]


@dataclass
class IndexSnapshot:
    """Payloads of one doc type's points, keyed the way each lookup filters.

    Holds a single ``doc_type``, so no key carries it. A lookup for any other
    doc type ignores the snapshot and scrolls (see :meth:`answers`).
    """

    doc_type: str
    # (doc_id, etag) -> a real point: find_indexed_content (tenant-wide).
    real_by_etag: dict[tuple[str, str], dict] = field(default_factory=dict)
    # (user_id, doc_id, is_placeholder) -> a point: query_document_metadata.
    by_user: dict[tuple[str, str, bool], dict] = field(default_factory=dict)
    # doc_id -> its terminal dead-letter marker: is_dead_lettered.
    dead_letters: dict[str, dict] = field(default_factory=dict)

    def answers(self, doc_type: str) -> bool:
        """Whether this snapshot can answer a lookup for ``doc_type``."""
        return self.doc_type == doc_type

    @classmethod
    async def collect(
        cls, points: AsyncIterable[Record], doc_type: str, doc_ids: set[str]
    ) -> IndexSnapshot:
        """Index ``points`` of ``doc_type``, keeping only documents in ``doc_ids``.

        Matching is exact, like Qdrant's ``MatchValue``: a legacy integer
        ``doc_id`` or a non-bool ``is_placeholder`` matches nothing.
        """
        snap = cls(doc_type=doc_type)
        async for point in points:
            payload = dict(point.payload or {})
            doc_id = payload.get("doc_id")
            if payload.get("doc_type") != doc_type or doc_id not in doc_ids:
                continue
            is_placeholder = payload.get("is_placeholder")
            if not isinstance(is_placeholder, bool):
                continue
            if is_placeholder and payload.get(DEAD_LETTER_KEY) is True:
                snap.dead_letters.setdefault(doc_id, payload)
            etag = payload.get("etag")
            if not is_placeholder and etag:
                snap.real_by_etag.setdefault((doc_id, etag), payload)
            user_id = payload.get("user_id")
            if user_id is not None:
                snap.by_user.setdefault((user_id, doc_id, is_placeholder), payload)
        return snap
