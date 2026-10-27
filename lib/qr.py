"""Digital Link QR rendering (IMPLEMENTATION_SPEC §4.7, §4.6, §6.4).

Renders a GS1 Digital Link URI into scannable QR files (SVG / PNG / EPS) sized for
physical print. Two properties matter:

* **Uppercase-domain optimisation** — the scheme and host are uppercased (path case
  preserved) so the whole URI falls inside the QR *alphanumeric* character set, yielding
  a smaller, denser-safe symbol. Scheme and host are case-insensitive per RFC 3986, so
  ``HTTPS://ID.GS1.ORG/01/...`` resolves identically to the lowercase form; the GTIN in
  the path stays untouched.
* **Determinism (§6.4)** — identical inputs produce a byte-identical SVG. The SVG is
  emitted directly from the QR module matrix (no library-inserted timestamps or ids), and
  the PNG/EPS rasters are pixel-deterministic for the same inputs.

The SVG is hand-emitted for exact millimetre sizing and byte-stability; PNG and EPS reuse
``qrcode``'s Pillow image (EPS written through Pillow).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

import qrcode
from qrcode.constants import (
    ERROR_CORRECT_H,
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
)

QRFormat = Literal["svg", "png", "eps"]
ErrorCorrection = Literal["L", "M", "Q", "H"]

#: Standard QR quiet zone, in modules (§4.6).
_QUIET_ZONE: Final = 4
_MM_PER_INCH: Final = 25.4

#: Map the public single-letter ECC level to the ``qrcode`` constant.
_ECC_LEVELS: Final[dict[ErrorCorrection, int]] = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def render_qr(  # noqa: PLR0913 — signature fixed by the spec (§4.7)
    uri: str,
    output_dir: str | Path,
    gtin: str,
    formats: list[QRFormat],
    size_mm: int,
    ecc: ErrorCorrection,
    dpi: int = 300,
) -> list[Path]:
    """Render ``uri`` as QR files, one per requested format (§4.7).

    Args:
        uri: The Digital Link URI to encode (lowercase form; the domain is uppercased
            internally for the alphanumeric-mode size optimisation).
        output_dir: Directory to write into (created if absent). Files are named
            ``{gtin}.{ext}``.
        gtin: GTIN, used as the output filename stem.
        formats: Formats to render, from ``svg``/``png``/``eps``; output order matches.
        size_mm: Target physical edge length of the symbol, in millimetres.
        ecc: Error-correction level (``L``/``M``/``Q``/``H``).
        dpi: Raster resolution for PNG/EPS; ignored by the vector SVG.

    Returns:
        The written file paths, in the same order as ``formats``.
    """
    payload = _uppercase_domain(uri)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    qr = qrcode.QRCode(error_correction=_ECC_LEVELS[ecc], border=_QUIET_ZONE)
    qr.add_data(payload)
    qr.make(fit=True)

    paths: list[Path] = []
    for fmt in formats:
        path = out_dir / f"{gtin}.{fmt}"
        if fmt == "svg":
            path.write_text(_render_svg(qr.get_matrix(), size_mm), encoding="utf-8")
        else:
            image = _render_raster(qr, size_mm, dpi)
            if fmt == "png":
                image.save(path, format="PNG", dpi=(dpi, dpi))
            else:  # eps — Pillow's EPS writer needs L/RGB/CMYK, not the 1-bit mode.
                image.convert("L").save(path, format="EPS")
        paths.append(path)
    return paths


def _uppercase_domain(uri: str) -> str:
    """Uppercase the scheme and host of ``uri``; preserve path/query/fragment case."""
    parts = urlsplit(uri)
    return urlunsplit(
        (parts.scheme.upper(), parts.netloc.upper(), parts.path, parts.query, parts.fragment)
    )


def _render_svg(matrix: list[list[bool]], size_mm: int) -> str:
    """Emit a deterministic SVG for the QR ``matrix`` at ``size_mm`` physical size.

    The matrix already includes the quiet-zone border. Each module is one user unit; the
    root scales the whole symbol to ``size_mm`` millimetres via ``width``/``height``.
    """
    n = len(matrix)
    segments = [
        f"M{col} {row}h1v1h-1z"
        for row, cells in enumerate(matrix)
        for col, dark in enumerate(cells)
        if dark
    ]
    path = "".join(segments)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size_mm}mm" height="{size_mm}mm" '
        f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges">\n'
        f'<rect width="{n}" height="{n}" fill="#ffffff"/>\n'
        f'<path d="{path}" fill="#000000"/>\n'
        "</svg>\n"
    )


def _render_raster(qr: qrcode.QRCode, size_mm: int, dpi: int) -> Any:
    """Build a Pillow image for ``qr`` at roughly ``size_mm`` at ``dpi``."""
    pixel_edge = round(size_mm / _MM_PER_INCH * dpi)
    total_modules = qr.modules_count + 2 * _QUIET_ZONE
    qr.box_size = max(1, round(pixel_edge / total_modules))
    return qr.make_image().get_image()
