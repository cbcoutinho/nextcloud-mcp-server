"""Prepare an image for a vision model: decode, orient, downscale, re-encode.

No network or disk I/O. ``nc_webdav_view_image`` and
``nc_mail_view_attachment`` hand the bytes of an image to
:func:`prepare_image_in_thread` (the blocking :func:`prepare_image` on a bounded
number of worker threads) and wrap the result with :func:`image_tool_result`,
which returns it as an MCP image block that clients pass to the model as an
image rather than as text.
"""

import base64
import io
import math
from dataclasses import dataclass

import anyio
from anyio.to_thread import run_sync
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, ImageOps, UnidentifiedImageError

from nextcloud_mcp_server.models.base import BaseResponse
from nextcloud_mcp_server.serialization import compact_json_dumps

#: Longest edge sent by default. Every Claude model takes an image this size
#: without downscaling it, and it stays under the 2000 px per-image limit the
#: Claude API applies once a request carries more than 20 images, which a
#: long conversation about photos reaches.
DEFAULT_MAX_SIZE = 1568

#: Largest longest edge a caller can ask for: the native resolution of
#: Claude's high-resolution models (4.7 and later). The API downscales
#: anything larger, so more pixels would only cost transfer.
MAX_SIZE = 2576

#: Pixel count refused before decoding, for the formats that ``draft()``
#: decodes at reduced size (:data:`_DRAFT_FORMATS`: JPEG, and MPO, the
#: multi-picture JPEG an iPhone writes). The decoder skips the resolution we
#: throw away anyway, so an image this large peaks at about 140 MB. A photo
#: from a phone has 12-50 megapixels.
MAX_SOURCE_PIXELS = 80_000_000

#: Pixel count refused before decoding, for every other format (PNG, GIF, WebP,
#: BMP, TIFF). Those are decoded in full: 24 MP of RGBA is 96 MB per copy, and
#: ``exif_transpose`` and ``thumbnail`` each make another, so an image at the
#: cap peaks at about 300 MB. The 80 MP allowed for JPEG would take about 1 GB
#: for a PNG that compresses to a few hundred KB, which anyone who can send the
#: user an email can attach. Nextcloud previews are at most 4096x4096 (16.8 MP),
#: so the preview path of ``nc_webdav_view_image`` always fits under this.
MAX_FULL_DECODE_PIXELS = 24_000_000

#: Pillow format names whose ``draft()`` shrinks the decode, so that
#: :data:`MAX_SOURCE_PIXELS` applies to them. ``MPO`` is a ``JpegImageFile``
#: subclass and inherits ``draft()``.
_DRAFT_FORMATS = frozenset({"JPEG", "MPO"})

#: Encoded size an image is kept under, by sending PNG as JPEG and lowering
#: the JPEG quality. Base64 inflates it by a third, which keeps it under the
#: strictest per-image limit Claude has (5 MB on Amazon Bedrock; the Claude
#: API allows 10 MB).
MAX_ENCODED_BYTES = 3_500_000

#: Decodes allowed to run at once, across all tools and sessions. Each takes
#: up to :data:`MAX_FULL_DECODE_PIXELS` worth of memory (about 300 MB), and the
#: default anyio thread pool would let 40 of them run side by side. Two keep the
#: worst case near 600 MB and still use two cores.
MAX_CONCURRENT_DECODES = 2

#: Shared by every call of :func:`prepare_image_in_thread`. Created at import,
#: outside any event loop: anyio then hands out an adapter that builds the real
#: limiter on first use, which holds no loop-bound state while uncontended.
_decode_limiter = anyio.CapacityLimiter(MAX_CONCURRENT_DECODES)

#: JPEG qualities tried in turn until the image fits MAX_ENCODED_BYTES.
#: The first is used for nearly every photo at these sizes.
_JPEG_QUALITIES = (85, 70, 55)

#: EXIF orientations that rotate by 90 or 270 degrees, swapping width and height.
_TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})


class ImageError(ValueError):
    """Bytes that cannot be turned into an image. The message is for the caller."""


@dataclass(frozen=True)
class PreparedImage:
    """An image ready to send, and the size of what it was made from."""

    data: bytes
    mime_type: str
    width: int
    height: int
    source_width: int
    source_height: int


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )


def _encode_jpeg(image: Image.Image) -> bytes:
    if _has_alpha(image):
        # JPEG has no alpha channel: flatten onto white, the usual page colour.
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    elif image.mode != "RGB":
        image = image.convert("RGB")
    for quality in _JPEG_QUALITIES:
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_ENCODED_BYTES:
            break
    # Still above it at the lowest quality, which takes a noise-like image
    # near MAX_SIZE: send it anyway. Only Bedrock's 5 MB limit refuses it.
    return out.getvalue()


def prepare_image(raw: bytes, max_size: int = DEFAULT_MAX_SIZE) -> PreparedImage:
    """Turn image bytes into a JPEG or PNG no larger than ``max_size`` per edge.

    - The EXIF orientation is applied, so a portrait photo arrives upright.
    - Images are only ever scaled down, never up.
    - Animated images contribute their first frame.
    - Transparency is kept by sending PNG, unless the PNG would be larger
      than :data:`MAX_ENCODED_BYTES`, in which case the image is flattened
      onto white and sent as JPEG. Everything else is sent as JPEG, at a
      lower quality if needed to fit :data:`MAX_ENCODED_BYTES`.

    Raises:
        ImageError: The bytes are not an image Pillow can decode, or the image
            has more pixels than it is decoded with: :data:`MAX_SOURCE_PIXELS`
            for JPEG, :data:`MAX_FULL_DECODE_PIXELS` for every other format.
    """
    try:
        image = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError as e:
        raise ImageError(
            "the file is not an image format this server can decode (JPEG, "
            "PNG, GIF, WebP, BMP, TIFF)"
        ) from e
    except Image.DecompressionBombError as e:
        # Pillow's own guard, which fires in open() for images far beyond
        # either cap, before the check below gets to run.
        raise ImageError(f"the image has too many pixels to decode ({e})") from e

    with image:
        # Image.open only reads the header, so this is checked before any
        # pixel data is decoded.
        image_format = image.format or "unknown"
        max_pixels = (
            MAX_SOURCE_PIXELS
            if image_format in _DRAFT_FORMATS
            else MAX_FULL_DECODE_PIXELS
        )
        if image.width * image.height > max_pixels:
            raise ImageError(
                f"the image has {image.width}x{image.height} pixels, more than "
                f"the {max_pixels:,} this server decodes for {image_format} images"
            )
        source_width, source_height = image.size
        if image.getexif().get(0x0112) in _TRANSPOSING_ORIENTATIONS:
            source_width, source_height = source_height, source_width

        # For JPEG, let the decoder skip resolution we would throw away anyway.
        # draft() keeps both edges at or above the box it is given, so it gets
        # the scaled size, not the square: with the square, the short edge
        # would block any reduction of a landscape photo.
        scale = min(max_size / image.width, max_size / image.height, 1.0)
        image.draft(
            "RGB",
            (math.ceil(image.width * scale), math.ceil(image.height * scale)),
        )
        try:
            oriented = ImageOps.exif_transpose(image)
        except (OSError, Image.DecompressionBombError) as e:
            raise ImageError(f"the image could not be decoded ({e})") from e

    oriented.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

    if _has_alpha(oriented):
        out = io.BytesIO()
        oriented.save(out, format="PNG", optimize=True)
        data, mime_type = out.getvalue(), "image/png"
        if len(data) > MAX_ENCODED_BYTES:
            data, mime_type = _encode_jpeg(oriented), "image/jpeg"
    else:
        data, mime_type = _encode_jpeg(oriented), "image/jpeg"

    return PreparedImage(
        data=data,
        mime_type=mime_type,
        width=oriented.width,
        height=oriented.height,
        source_width=source_width,
        source_height=source_height,
    )


async def prepare_image_in_thread(
    raw: bytes, max_size: int = DEFAULT_MAX_SIZE
) -> PreparedImage:
    """Run :func:`prepare_image` on a worker thread, a few at a time.

    At most :data:`MAX_CONCURRENT_DECODES` run at once, process-wide. Further
    callers wait their turn, so a burst of requests queues up instead of
    holding a decoded image each in memory.
    """
    return await run_sync(prepare_image, raw, max_size, limiter=_decode_limiter)


def image_tool_result(
    prepared: PreparedImage, response: BaseResponse
) -> CallToolResult:
    """The result of an image tool: the image, then ``response`` as JSON.

    A tool that declares ``Annotated[CallToolResult, <Response>]`` returns this.
    The image block comes first, which is what a client hands to the model as a
    picture. The text block carries the same JSON as ``structured_content``,
    compact like the rest of the server's JSON, for clients that read only
    text. The SDK validates the structured part against the response model.
    """
    structured = response.model_dump(mode="json", by_alias=True)
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(prepared.data).decode("ascii"),
                mime_type=prepared.mime_type,
            ),
            TextContent(type="text", text=compact_json_dumps(structured)),
        ],
        structured_content=structured,
    )
