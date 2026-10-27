"""Prepare an image for a vision model: decode, orient, downscale, re-encode.

Pure function, no I/O. ``nc_webdav_view_image`` hands the bytes of a Nextcloud
preview (or of the original file) to :func:`prepare_image` and returns the
result as an MCP image block, which clients pass to the model as an image
rather than as text.
"""

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

#: Longest edge sent by default. Every Claude model takes an image this size
#: without downscaling it, and it stays under the 2000 px per-image limit the
#: Claude API applies once a request carries more than 20 images, which a
#: long conversation about photos reaches.
DEFAULT_MAX_SIZE = 1568

#: Largest longest edge a caller can ask for: the native resolution of
#: Claude's high-resolution models (4.7 and later). The API downscales
#: anything larger, so more pixels would only cost transfer.
MAX_SIZE = 2576

#: Pixel count refused before decoding. A photo from a phone has 12-50
#: megapixels, and decoding one this large to RGB already takes several
#: hundred MB of memory.
MAX_SOURCE_PIXELS = 80_000_000

#: Encoded size an image is kept under, by sending PNG as JPEG and lowering
#: the JPEG quality. Base64 inflates it by a third, which keeps it under the
#: strictest per-image limit Claude has (5 MB on Amazon Bedrock; the Claude
#: API allows 10 MB).
MAX_ENCODED_BYTES = 3_500_000

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
            has more than :data:`MAX_SOURCE_PIXELS` pixels.
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
        # MAX_SOURCE_PIXELS, before the check below gets to run.
        raise ImageError(f"the image has too many pixels to decode ({e})") from e

    with image:
        # Image.open only reads the header, so this is checked before any
        # pixel data is decoded.
        if image.width * image.height > MAX_SOURCE_PIXELS:
            raise ImageError(
                f"the image has {image.width}x{image.height} pixels, more than "
                f"the {MAX_SOURCE_PIXELS:,} this server decodes"
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
