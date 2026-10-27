"""Decode GDSN measurement-unit codes to their per-language functional names.

The GDSN feed carries ``net_content`` as a value plus a raw ``MeasurementUnitCode`` — e.g.
``"5 H87"`` — where ``H87`` is a code, not a word a shopper reads. This module turns the code
into its functional name per language (``H87`` → ``Stuk`` / ``Piece`` / ``Pièce``) using
``reference/measurement_units.json``, a committed extract of the GS1 datamodel's
``MeasurementUnitCode_GDSN`` picklist (a global GS1 code list, not client-specific).

Decoding happens at render time, per language, so ``net_content`` stays language-agnostic on
the record (and in the content hash); the shared decoder is reusable by any per-language
consumer (templates now, the Technische-details generator later).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

_REFERENCE_PATH = Path(__file__).resolve().parent.parent / "reference" / "measurement_units.json"

#: Language whose label is used when a value's language and fallback are both absent.
_LAST_RESORT_LANGUAGE = "en"


@lru_cache(maxsize=1)
def load_measurement_units() -> dict[str, dict[str, str]]:
    """Load the ``MeasurementUnitCode`` → ``{language: label}`` reference table (cached)."""
    data: dict[str, dict[str, str]] = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
    return data


def decode_unit(code: str, language: str, *, fallback_language: str | None = None) -> str | None:
    """Return the functional name for ``code`` in ``language``.

    Falls back to ``fallback_language`` then to English, and returns ``None`` for an unknown
    code so the caller can leave the raw value untouched rather than guess.
    """
    entry = load_measurement_units().get(code)
    if entry is None:
        return None
    fallback = entry.get(fallback_language, "") if fallback_language else ""
    return entry.get(language) or fallback or entry.get(_LAST_RESORT_LANGUAGE) or None


def decode_net_content(
    value: str | None, language: str, *, fallback_language: str | None = None
) -> str | None:
    """Decode the trailing GDSN unit code in a ``net_content`` string to a language word.

    ``net_content`` is stored as ``"<value> <MeasurementUnitCode>"`` (e.g. ``"5 H87"``). The
    trailing code is replaced with its functional name for ``language`` (``"5 Stuk"`` nl,
    ``"5 Piece"`` en, ``"5 Pièce"`` fr). A value with no decodable trailing code — a plain
    number, or one already worded — is returned unchanged; an unknown code is left as-is rather
    than dropped, so nothing is silently mangled.

    Args:
        value: The raw ``net_content`` (``"<number> <code>"``), or ``None``.
        language: The render language (ISO 639-1).
        fallback_language: Language to fall back to when ``language`` has no label.

    Returns:
        The decoded string, the input unchanged when nothing decodes, or ``None`` for ``None``.
    """
    if not value:
        return value
    head, separator, tail = value.rpartition(" ")
    if not separator:
        return value  # a single token — no unit code to decode
    label = decode_unit(tail, language, fallback_language=fallback_language)
    if label is None:
        return value  # unknown code — leave the raw value untouched
    return f"{head} {label}"
