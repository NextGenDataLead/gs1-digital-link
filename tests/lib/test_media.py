"""Unit tests for the image convert/resize step (Phase 9.5 media)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from lib.media import convert_image_for_web


def _image_bytes(size: tuple[int, int], mode: str = "RGB", image_format: str = "TIFF") -> bytes:
    """Synthesize an in-memory image of ``size``/``mode`` encoded as ``image_format``."""
    colour: int | tuple[int, ...] = 128 if mode in {"L", "LA"} else (10, 120, 200)
    if mode in {"RGBA", "LA"}:
        colour = (10, 120, 200, 128) if mode == "RGBA" else (128, 128)
    img = Image.new(mode, size, colour)
    buf = io.BytesIO()
    img.save(buf, format=image_format)
    return buf.getvalue()


def _open(dest: Path) -> Image.Image:
    img = Image.open(dest)
    img.load()
    return img


def test_tiff_converts_to_web_jpeg(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    result = convert_image_for_web(_image_bytes((800, 600), image_format="TIFF"), dest)
    assert result == dest
    assert dest.exists()
    assert _open(dest).format == "JPEG"


def test_png_alpha_flattened_to_rgb(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    convert_image_for_web(_image_bytes((300, 300), mode="RGBA", image_format="PNG"), dest)
    img = _open(dest)
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_large_downscaled_to_max_dim(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    convert_image_for_web(_image_bytes((3200, 2400), image_format="TIFF"), dest, max_dim=1600)
    img = _open(dest)
    assert max(img.size) == 1600
    # aspect ratio preserved (4:3)
    assert img.size == (1600, 1200)


def test_small_image_not_upscaled(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    convert_image_for_web(_image_bytes((400, 300), image_format="PNG"), dest, max_dim=1600)
    assert _open(dest).size == (400, 300)


def test_jpeg_master_reencoded_and_capped(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    convert_image_for_web(_image_bytes((2000, 2000), image_format="JPEG"), dest, max_dim=1600)
    img = _open(dest)
    assert img.format == "JPEG"
    assert max(img.size) == 1600


def test_output_bytes_deterministic(tmp_path: Path) -> None:
    src = _image_bytes((1200, 900), image_format="TIFF")
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    convert_image_for_web(src, a)
    convert_image_for_web(src, b)
    assert a.read_bytes() == b.read_bytes()


def test_undecodable_bytes_returns_none(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    assert convert_image_for_web(b"this is not an image", dest) is None
    assert not dest.exists()


def test_creates_dest_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "deeper" / "out.jpg"
    result = convert_image_for_web(_image_bytes((200, 200), image_format="PNG"), dest)
    assert result == dest
    assert dest.exists()


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "LA", "P"])
def test_various_modes_produce_rgb_jpeg(tmp_path: Path, mode: str) -> None:
    dest = tmp_path / "out.jpg"
    # "P" (palette) is only meaningful for PNG/GIF; use PNG for all here.
    result = convert_image_for_web(_image_bytes((256, 256), mode=mode, image_format="PNG"), dest)
    assert result == dest
    assert _open(dest).mode == "RGB"
