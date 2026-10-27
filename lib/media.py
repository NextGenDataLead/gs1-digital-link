"""Convert an arbitrary source image to a web-ready JPEG for WordPress upload (Phase 9.5).

The GDSN feed's referenced files are print masters — 92% are ``image/tiff`` and many run
10–45 MB (3200×3200) — which WordPress will not accept. This module bridges
:meth:`lib.wp_client.WordPressClient.download_image` (which returns raw ``bytes``) and
:meth:`~lib.wp_client.WordPressClient.upload_media` (which uploads a local file): decode the
bytes, flatten and downscale, and write a deterministic baseline JPEG the media library accepts.

Two contracts the run loop relies on:

* **E7 — a broken image must not stop publishing.** Bytes that cannot be decoded yield ``None``
  (logged), so the caller skips featured media and still creates the page.
* **Determinism — re-runs must not churn attachments.** The encoder is given fixed parameters and
  no metadata, so identical input bytes produce identical output bytes, hence an identical SHA-256;
  ``upload_media`` then reuses the existing attachment instead of creating a duplicate.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

_log = logging.getLogger(__name__)

#: Longest-edge cap, in pixels, for the uploaded image (defaults; real values come from config).
_DEFAULT_MAX_DIM = 1600
#: JPEG quality (1–100).
_DEFAULT_QUALITY = 85

#: Modes carrying transparency or a palette that must be flattened to RGB before JPEG encoding.
_NON_RGB_MODES = frozenset({"RGBA", "LA", "P"})
#: White background used when flattening transparency.
_FLATTEN_BACKGROUND = (255, 255, 255)


def convert_image_for_web(
    data: bytes,
    dest: Path,
    *,
    max_dim: int = _DEFAULT_MAX_DIM,
    quality: int = _DEFAULT_QUALITY,
) -> Path | None:
    """Decode ``data``, downscale to fit ``max_dim``, and write a web JPEG to ``dest``.

    Convert-all policy (operator-confirmed): TIFF, PNG, and already-JPEG inputs are all decoded
    and re-encoded to baseline JPEG, so the dimension cap and a stable dedupe hash apply
    uniformly. Transparency (``RGBA``/``LA``/``P``) is flattened onto white. The image is only
    ever downscaled (never upscaled), preserving aspect ratio. The output is metadata-free and
    encoded with fixed parameters, so identical input bytes yield byte-identical output.

    Args:
        data: Raw image bytes (e.g. from ``download_image``).
        dest: Destination path for the JPEG; parent directories are created.
        max_dim: Longest-edge cap in pixels.
        quality: JPEG quality (1–100).

    Returns:
        ``dest`` on success, or ``None`` if the bytes cannot be decoded (edge E7).
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            prepared = _flatten(img)
            prepared.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            prepared.save(dest, format="JPEG", quality=quality, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        _log.warning("Image decode/convert failed (%r); skipping featured media", exc)
        return None
    return dest


def _flatten(img: Image.Image) -> Image.Image:
    """Return an ``RGB`` copy of ``img``, compositing any transparency onto white."""
    if img.mode in _NON_RGB_MODES:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, _FLATTEN_BACKGROUND)
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")
