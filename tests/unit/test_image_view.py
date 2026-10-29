"""Unit tests for preparing images for a vision model (utils/image_view)."""

import base64
import io
import json
import threading
import time

import anyio
import pytest
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import ConfigDict, Field

from nextcloud_mcp_server.models.base import BaseResponse
from nextcloud_mcp_server.serialization import compact_json_dumps
from nextcloud_mcp_server.utils import image_view
from nextcloud_mcp_server.utils.image_view import (
    ImageError,
    PreparedImage,
    image_tool_result,
    prepare_image,
    prepare_image_in_thread,
)


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def _decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_large_photo_is_scaled_to_the_longest_edge():
    raw = _encode(Image.new("RGB", (4000, 3000), (10, 120, 200)), "JPEG")

    result = prepare_image(raw, max_size=1568)

    assert result.mime_type == "image/jpeg"
    assert (result.width, result.height) == (1568, 1176)
    assert (result.source_width, result.source_height) == (4000, 3000)
    assert _decode(result.data).size == (1568, 1176)


def test_small_image_is_never_upscaled():
    raw = _encode(Image.new("RGB", (300, 200), "white"), "PNG")

    result = prepare_image(raw, max_size=1568)

    assert (result.width, result.height) == (300, 200)


def test_exif_orientation_turns_the_photo_upright():
    """A phone stores portraits sideways plus an orientation tag (6 = 90° CW)."""
    image = Image.new("RGB", (4000, 3000), "gray")
    exif = image.getexif()
    exif[0x0112] = 6
    raw = _encode(image, "JPEG", exif=exif)

    result = prepare_image(raw, max_size=1568)

    assert (result.width, result.height) == (1176, 1568)
    assert (result.source_width, result.source_height) == (3000, 4000)


def test_transparency_is_kept_as_png():
    raw = _encode(Image.new("RGBA", (800, 400), (255, 0, 0, 0)), "PNG")

    result = prepare_image(raw, max_size=1568)

    assert result.mime_type == "image/png"
    assert _decode(result.data).mode == "RGBA"


def test_oversized_png_falls_back_to_jpeg(monkeypatch):
    """Keeps a big transparent screenshot under the per-image API limit."""
    monkeypatch.setattr(image_view, "MAX_ENCODED_BYTES", 10)
    raw = _encode(Image.new("RGBA", (800, 400), (255, 0, 0, 128)), "PNG")

    result = prepare_image(raw, max_size=1568)

    assert result.mime_type == "image/jpeg"
    assert _decode(result.data).mode == "RGB"


@pytest.mark.parametrize("mode", ["L", "CMYK", "P"])
def test_other_colour_modes_become_rgb_jpeg(mode):
    fmt = "JPEG" if mode == "CMYK" else "PNG"
    raw = _encode(Image.new(mode, (50, 50)), fmt)

    result = prepare_image(raw)

    assert result.mime_type == "image/jpeg"
    assert _decode(result.data).mode == "RGB"


def test_animated_gif_contributes_its_first_frame():
    frames = [Image.new("P", (60, 40), color) for color in (1, 2, 3)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:])

    result = prepare_image(out.getvalue())

    assert (result.width, result.height) == (60, 40)


def test_bytes_that_are_not_an_image_are_refused():
    with pytest.raises(ImageError, match="not an image format"):
        prepare_image(b"%PDF-1.7 definitely not an image")


def test_jpeg_quality_drops_until_the_image_fits(monkeypatch):
    """A noise-like photo near MAX_SIZE is the only case that needs it."""
    noise = Image.effect_noise((600, 600), 100).convert("RGB")
    first_try = _encode(noise, "JPEG", quality=85, optimize=True)
    monkeypatch.setattr(image_view, "MAX_ENCODED_BYTES", len(first_try) - 1)

    result = prepare_image(_encode(noise, "PNG"), max_size=600)

    assert result.mime_type == "image/jpeg"
    assert len(result.data) < len(first_try)


def test_pillows_own_bomb_guard_is_reported_as_an_image_error(monkeypatch):
    """Pillow refuses far-too-large images inside open(), before our check."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    raw = _encode(Image.new("RGB", (20, 20)), "PNG")

    with pytest.raises(ImageError, match="too many pixels"):
        prepare_image(raw)


def _mpo(size: tuple[int, int]) -> bytes:
    """A multi-picture JPEG, as an iPhone writes: Pillow opens it as ``MPO``."""
    first = Image.new("RGB", size, (200, 30, 30))
    second = Image.new("RGB", size, (30, 30, 200))
    out = io.BytesIO()
    first.save(out, format="MPO", save_all=True, append_images=[second])
    return out.getvalue()


def _forbid_decoding(monkeypatch) -> None:
    """Fail the test if the pixels are touched: Image.open only reads the header."""

    def decoded(*args, **kwargs):
        raise AssertionError("the image was decoded")

    monkeypatch.setattr(image_view.ImageOps, "exif_transpose", decoded)


def test_the_largest_nextcloud_preview_fits_the_lower_cap():
    """Nextcloud previews are at most 4096x4096, so the preview path of
    nc_webdav_view_image never meets the cap for fully decoded formats."""
    assert (
        4096 * 4096 <= image_view.MAX_FULL_DECODE_PIXELS < image_view.MAX_SOURCE_PIXELS
    )


@pytest.mark.parametrize("fmt", ["PNG", "GIF", "WEBP", "BMP", "TIFF"])
def test_formats_decoded_in_full_are_refused_above_the_lower_cap(monkeypatch, fmt):
    monkeypatch.setattr(image_view, "MAX_FULL_DECODE_PIXELS", 100)
    _forbid_decoding(monkeypatch)
    raw = _encode(Image.new("RGB", (20, 20)), fmt)

    with pytest.raises(
        ImageError,
        match=rf"20x20 pixels, more than the 100 this server decodes for {fmt} images",
    ):
        prepare_image(raw)


def test_a_png_is_refused_where_a_jpeg_of_the_same_size_is_not(monkeypatch):
    """The two caps: 400 pixels is between them."""
    monkeypatch.setattr(image_view, "MAX_FULL_DECODE_PIXELS", 100)
    monkeypatch.setattr(image_view, "MAX_SOURCE_PIXELS", 1_000)
    image = Image.new("RGB", (20, 20), (10, 120, 200))

    shown = prepare_image(_encode(image, "JPEG"))
    with pytest.raises(ImageError, match="for PNG images"):
        prepare_image(_encode(image, "PNG"))

    assert (shown.width, shown.height) == (20, 20)


def test_jpeg_above_the_upper_cap_is_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(image_view, "MAX_SOURCE_PIXELS", 100)
    _forbid_decoding(monkeypatch)
    raw = _encode(Image.new("RGB", (20, 20)), "JPEG")

    with pytest.raises(
        ImageError, match="20x20 pixels, more than the 100 this server decodes for JPEG"
    ):
        prepare_image(raw)


def test_multi_picture_jpeg_gets_the_upper_cap(monkeypatch):
    """MPO is a JpegImageFile subclass, so draft() shrinks its decode too."""
    monkeypatch.setattr(image_view, "MAX_FULL_DECODE_PIXELS", 100)
    monkeypatch.setattr(image_view, "MAX_SOURCE_PIXELS", 1_000)
    raw = _mpo((20, 20))
    assert Image.open(io.BytesIO(raw)).format == "MPO"

    shown = prepare_image(raw)
    monkeypatch.setattr(image_view, "MAX_SOURCE_PIXELS", 100)
    with pytest.raises(ImageError, match="for MPO images"):
        prepare_image(raw)

    assert shown.mime_type == "image/jpeg"
    assert (shown.width, shown.height) == (20, 20)


def test_draft_shrinks_the_decode_of_a_multi_picture_jpeg():
    """The reason MPO is allowed the upper cap."""
    image = Image.open(io.BytesIO(_mpo((4000, 3000))))

    image.draft("RGB", (1000, 750))

    assert image.size == (1000, 750)


async def _peak_concurrent_decodes(monkeypatch, calls: int) -> int:
    """Run ``calls`` decodes at once, with a slow stand-in, and report how many
    ran at the same time."""
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def slow_prepare(raw: bytes, max_size: int) -> PreparedImage:
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.1)
        with lock:
            state["running"] -= 1
        return PreparedImage(
            data=raw,
            mime_type="image/jpeg",
            width=1,
            height=1,
            source_width=1,
            source_height=1,
        )

    monkeypatch.setattr(image_view, "prepare_image", slow_prepare)
    results: list[PreparedImage] = []

    async def decode() -> None:
        results.append(await prepare_image_in_thread(b"x", 100))

    async with anyio.create_task_group() as tg:
        for _ in range(calls):
            tg.start_soon(decode)

    assert len(results) == calls
    return state["peak"]


async def test_at_most_two_decodes_run_at_once(monkeypatch):
    """Each decode can take hundreds of MB, and the default thread pool would
    run about 40 of them side by side."""
    assert image_view.MAX_CONCURRENT_DECODES == 2

    assert await _peak_concurrent_decodes(monkeypatch, calls=5) == 2


def test_the_decode_limiter_works_across_event_loops(monkeypatch):
    """It is created at import, outside any loop, and shared by every loop
    that follows: one per test under the anyio plugin, one per anyio.run here."""
    first = anyio.run(_peak_concurrent_decodes, monkeypatch, 5)
    second = anyio.run(_peak_concurrent_decodes, monkeypatch, 5)

    assert (first, second) == (2, 2)


async def test_prepare_image_in_thread_returns_what_prepare_image_returns():
    raw = _encode(Image.new("RGB", (300, 200), "white"), "PNG")

    assert await prepare_image_in_thread(raw, 100) == prepare_image(raw, 100)


class _Response(BaseResponse):
    """A response with an aliased field, to show the dump uses the alias."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    mime_type: str = Field(alias="mimeType")


def test_image_tool_result_is_the_image_then_the_response_as_json():
    prepared = PreparedImage(
        data=b"\xff\xd8jpeg bytes",
        mime_type="image/jpeg",
        width=10,
        height=5,
        source_width=20,
        source_height=10,
    )
    response = _Response(name="a.jpg", mime_type="image/jpeg")

    result = image_tool_result(prepared, response)

    image, text = result.content
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == prepared.data
    expected = response.model_dump(mode="json", by_alias=True)
    assert result.structured_content == expected
    assert expected["mimeType"] == "image/jpeg"
    assert isinstance(text, TextContent)
    assert json.loads(text.text) == expected
    assert text.text == compact_json_dumps(expected)
    assert "\n" not in text.text
    assert result.is_error is False
