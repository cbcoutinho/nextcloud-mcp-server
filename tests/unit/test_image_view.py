"""Unit tests for preparing images for a vision model (utils/image_view)."""

import io

import pytest
from PIL import Image

from nextcloud_mcp_server.utils import image_view
from nextcloud_mcp_server.utils.image_view import ImageError, prepare_image


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


def test_too_many_pixels_are_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(image_view, "MAX_SOURCE_PIXELS", 100)
    raw = _encode(Image.new("RGB", (20, 20)), "PNG")

    with pytest.raises(ImageError, match="20x20 pixels"):
        prepare_image(raw)
