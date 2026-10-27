"""Unit tests for GDSN measurement-unit decoding (net_content H87 → functional name)."""

from __future__ import annotations

import pytest

from lib.units import decode_net_content, decode_unit, load_measurement_units


def test_reference_table_loads_and_has_h87() -> None:
    units = load_measurement_units()
    assert units["H87"] == {"nl": "Stuk", "en": "Piece", "fr": "Pièce"}


# --- decode_unit -------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [("nl", "Stuk"), ("en", "Piece"), ("fr", "Pièce")],
)
def test_decode_unit_per_language(language: str, expected: str) -> None:
    assert decode_unit("H87", language) == expected


def test_decode_unit_unknown_code_returns_none() -> None:
    assert decode_unit("NOPE", "nl") is None


def test_decode_unit_falls_back_when_language_absent() -> None:
    # A language the table doesn't carry falls back, then to English.
    assert decode_unit("H87", "de", fallback_language="nl") == "Stuk"
    assert decode_unit("H87", "de") == "Piece"  # last resort: English


# --- decode_net_content ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "language", "expected"),
    [
        ("5 H87", "nl", "5 Stuk"),
        ("5 H87", "fr", "5 Pièce"),
        ("4 H87", "en", "4 Piece"),
        ("10 LTR", "nl", "10 Liter"),
    ],
)
def test_decode_net_content_decodes_trailing_code(value: str, language: str, expected: str) -> None:
    assert decode_net_content(value, language) == expected


def test_decode_net_content_unknown_code_left_unchanged() -> None:
    # "L" is not a GDSN code — leave the raw value rather than mangle it.
    assert decode_net_content("10 L", "nl") == "10 L"


def test_decode_net_content_single_token_unchanged() -> None:
    assert decode_net_content("5", "nl") == "5"


def test_decode_net_content_none_and_empty() -> None:
    assert decode_net_content(None, "nl") is None
    assert decode_net_content("", "nl") == ""


def test_decode_net_content_fallback_language() -> None:
    assert decode_net_content("5 H87", "de", fallback_language="nl") == "5 Stuk"
