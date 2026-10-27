"""Tests for Digital Link QR rendering (IMPLEMENTATION_SPEC §4.7, §6.4).

Covers the §6.4 determinism contract (byte-identical SVG), the uppercase-domain
optimisation, format/ordering behaviour, ECC mapping, and physical sizing.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from lib.qr import (
    _ECC_LEVELS,
    _MM_PER_INCH,
    _render_svg,
    _uppercase_domain,
    render_qr,
)

URI = "https://id.gs1.org/01/08712345678904"
GTIN = "08712345678904"


# --- Uppercase-domain optimisation (§4.7) ------------------------------------


def test_uppercase_domain_uppercases_scheme_and_host_only() -> None:
    assert _uppercase_domain(URI) == "HTTPS://ID.GS1.ORG/01/08712345678904"


def test_uppercase_domain_preserves_path_case() -> None:
    out = _uppercase_domain("https://id.gs1.org/01/12345/10/Lot-Ab")
    assert out == "HTTPS://ID.GS1.ORG/01/12345/10/Lot-Ab"


# --- §6.4 determinism --------------------------------------------------------


def test_svg_is_byte_identical_across_renders(tmp_path: Path) -> None:
    a = render_qr(URI, tmp_path / "a", GTIN, ["svg"], size_mm=20, ecc="M")
    b = render_qr(URI, tmp_path / "b", GTIN, ["svg"], size_mm=20, ecc="M")

    assert a[0].read_bytes() == b[0].read_bytes()


def test_png_is_pixel_identical_across_renders(tmp_path: Path) -> None:
    a = render_qr(URI, tmp_path / "a", GTIN, ["png"], size_mm=20, ecc="M")
    b = render_qr(URI, tmp_path / "b", GTIN, ["png"], size_mm=20, ecc="M")

    with Image.open(a[0]) as ia, Image.open(b[0]) as ib:
        assert ia.size == ib.size
        assert ia.convert("1").tobytes() == ib.convert("1").tobytes()


# --- Formats, ordering, output paths -----------------------------------------


def test_renders_requested_formats_in_order(tmp_path: Path) -> None:
    paths = render_qr(URI, tmp_path, GTIN, ["png", "svg", "eps"], size_mm=20, ecc="M")

    assert [p.name for p in paths] == [f"{GTIN}.png", f"{GTIN}.svg", f"{GTIN}.eps"]
    for path in paths:
        assert path.is_file()
        assert path.stat().st_size > 0


def test_creates_output_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "qr"
    paths = render_qr(URI, target, GTIN, ["svg"], size_mm=20, ecc="M")

    assert paths[0].parent == target
    assert target.is_dir()


# --- ECC mapping -------------------------------------------------------------


def test_ecc_levels_cover_all_four_grades() -> None:
    assert set(_ECC_LEVELS) == {"L", "M", "Q", "H"}
    # Distinct qrcode constants — no accidental aliasing.
    assert len(set(_ECC_LEVELS.values())) == 4


# --- Physical sizing ---------------------------------------------------------


def test_png_dimensions_track_size_mm_and_dpi(tmp_path: Path) -> None:
    size_mm, dpi = 20, 300
    paths = render_qr(URI, tmp_path, GTIN, ["png"], size_mm=size_mm, ecc="M", dpi=dpi)

    with Image.open(paths[0]) as img:
        assert img.width == img.height
        target_px = round(size_mm / _MM_PER_INCH * dpi)
        # box_size is a whole number of pixels per module, so the realised edge is the
        # nearest module-multiple of the target (rounding error < half a module).
        assert abs(img.width - target_px) <= target_px * 0.1


def test_svg_declares_physical_millimetre_size(tmp_path: Path) -> None:
    paths = render_qr(URI, tmp_path, GTIN, ["svg"], size_mm=25, ecc="M")
    svg = paths[0].read_text(encoding="utf-8")

    assert 'width="25mm"' in svg
    assert 'height="25mm"' in svg


def test_render_svg_draws_one_rect_per_dark_module() -> None:
    matrix = [[True, False], [False, True]]
    svg = _render_svg(matrix, 20)

    assert svg.count("h1v1h-1z") == 2
    assert 'viewBox="0 0 2 2"' in svg
