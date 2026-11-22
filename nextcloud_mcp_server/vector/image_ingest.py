"""Image embeddings at ingest (ADR-042 §5).

A tagged image file gets one extra point, carrying the ``image`` vector next to
whatever text a processor extracted from it. This module decides which files
take part and turns one file into that vector, or into the reason it was
skipped. The ingest worker (``vector/processor.py``) writes the points.

Pillow is imported lazily: the scanner (API role) reads :func:`image_mime_types`
for discovery and must not load an image decoder to do it.
"""

from __future__ import annotations

import io
import logging
import math
from functools import cache

import anyio

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.document_source import DocumentSource
from nextcloud_mcp_server.providers import get_provider
from nextcloud_mcp_server.providers.base import (
    ImageInput,
    ImageInputLimits,
    ImageInputRejected,
)
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.qdrant_client import image_vector_ready

logger = logging.getLogger(__name__)

# Values of payload_keys.IMAGE_SKIPPED_REASON. A skip is permanent for this
# content and image model; the scanner reads it as fresh (ADR-042 §3).
SKIP_TOO_LARGE = "too_large"  # over IMAGE_EMBEDDING_MAX_SOURCE_BYTES
SKIP_REJECTED = "rejected"  # undecodable, or ImageInputRejected by the provider


def base_mime(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


@cache
def _decodable_mime_types() -> frozenset[str]:
    from PIL import Image  # noqa: PLC0415

    Image.init()
    return frozenset(Image.MIME.values())


def image_mime_types() -> frozenset[str]:
    """Image types this deployment embeds, empty while the feature is off.

    The configured model's accepted types, narrowed to what the worker can
    decode for downscaling. Read from the provider, never hardcoded: Titan
    takes PNG and JPEG only, Cohere v4 also WebP and GIF.
    """
    if not image_vector_ready():
        return frozenset()
    limits = get_provider().image_input_limits
    if limits is None:
        return frozenset()
    return limits.mime_types & _decodable_mime_types()


def in_image_scope(content_type: str, index_mode: str) -> bool:
    """Whether a file of this type and mode gets an image point (ADR-042 §3).

    Only ``hybrid`` files: ``keyword-index`` opts out of paid embeddings.
    """
    return (
        index_mode == payload_keys.INDEX_MODE_HYBRID
        and base_mime(content_type) in image_mime_types()
    )


def _fit(data: bytes, mime_type: str, limits: ImageInputLimits) -> ImageInput:
    """Downscale/re-encode only when the model's limits require it.

    An animated GIF/WebP is re-encoded from its first frame. Raises
    ``OSError`` (Pillow's ``UnidentifiedImageError`` included) or
    ``DecompressionBombError`` for an image that can't be decoded.
    """
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(data)) as img:
        pixels = img.width * img.height
        over_pixels = limits.max_pixels is not None and pixels > limits.max_pixels
        over_bytes = limits.max_bytes is not None and len(data) > limits.max_bytes
        if not (over_pixels or over_bytes or getattr(img, "is_animated", False)):
            return ImageInput(data=data, mime_type=mime_type)
        scale = math.sqrt(limits.max_pixels / pixels) if over_pixels else 1.0
        frame = img.convert("RGB")  # frame 0; alpha doesn't matter to an embedding
    out_format, out_mime = (
        ("JPEG", "image/jpeg")
        if "image/jpeg" in limits.mime_types
        else ("PNG", "image/png")
    )
    while True:
        size = (max(1, int(frame.width * scale)), max(1, int(frame.height * scale)))
        buf = io.BytesIO()
        frame.resize(size).save(buf, format=out_format, quality=85)
        out = buf.getvalue()
        if limits.max_bytes is None or len(out) <= limits.max_bytes:
            return ImageInput(data=out, mime_type=out_mime)
        scale *= 0.7  # terminates: a 1x1 image is a few hundred bytes


async def embed_document_image(
    source: DocumentSource, content_type: str, settings: Settings
) -> tuple[list[float] | None, str | None]:
    """``(vector, None)``, or ``(None, skip_reason)`` for a permanent skip.

    Anything else (timeouts, 5xx, 429) raises, so the job retries and nothing
    is written: a document's text and image points succeed or fail together.
    """
    from PIL import Image  # noqa: PLC0415

    name = source.filename
    if source.size > settings.image_embedding_max_source_bytes:
        logger.info(
            "Image %s is %s bytes, over IMAGE_EMBEDDING_MAX_SOURCE_BYTES=%s; "
            "skipping its image embedding",
            name,
            source.size,
            settings.image_embedding_max_source_bytes,
        )
        return None, SKIP_TOO_LARGE
    provider = get_provider()
    limits = provider.image_input_limits
    if limits is None:
        # in_image_scope() was true, which needs limits: a provider bug.
        raise RuntimeError(f"{type(provider).__name__} has no image_input_limits")
    data = await anyio.to_thread.run_sync(source.read_bytes)
    try:
        image = await anyio.to_thread.run_sync(
            _fit, data, base_mime(content_type), limits
        )
    except (OSError, Image.DecompressionBombError) as exc:
        logger.warning(
            "Image %s could not be decoded (%s); skipping its image embedding",
            name,
            exc,
        )
        return None, SKIP_REJECTED
    try:
        return await provider.embed_image(image), None
    except ImageInputRejected as exc:
        logger.warning(
            "Image %s rejected by %s (%s); skipping its image embedding",
            name,
            settings.image_embedding_model,
            exc.reason,
        )
        return None, SKIP_REJECTED
