"""Tests for the parser's language-balance check — marketing text far shorter in one language.

The generator writes each language only from that language's own source text and never invents a
claim, so a Dutch marketing message of three words beside a French one of two hundred produces a
Dutch page with one bullet and a French page with four. Found on the e2e run (the Bottle Lamp,
…5898); measured on the pilot export, 23 products sit below a quarter and the next pair is far
above it.
"""

from __future__ import annotations

from lib.gdsn import THIN_TEXT_ISSUE, check_language_balance

GTIN = "02987653005898"
SOURCE = "MarketingInformation attr 1083"
LONG_FR = " ".join(["mot"] * 189)


def _check(values: dict[str, str]) -> list:
    return check_language_balance(values, "description_short", SOURCE, GTIN)


def test_a_few_words_beside_a_long_text_is_flagged_on_the_short_language() -> None:
    issues = _check({"nl": "Draadloze led-wijnfleslamp", "fr": LONG_FR})

    assert [(i.field, i.issue, i.source) for i in issues] == [
        ("description_short.nl", THIN_TEXT_ISSUE, SOURCE)
    ]
    assert "2 words" in issues[0].detail and "189" in issues[0].detail


def test_comparable_texts_are_not_flagged() -> None:
    """The Desk lamp: 75 Dutch words, 81 French."""
    assert _check({"nl": " ".join(["woord"] * 75), "fr": " ".join(["mot"] * 81)}) == []


def test_two_short_texts_are_not_a_mismatch() -> None:
    """Below 20 words in the longer one there is little to lose; that is a different finding."""
    assert _check({"nl": "Kort", "fr": "Une phrase de six mots ici"}) == []


def test_a_blank_language_is_left_to_the_gap_checks() -> None:
    """A missing value is §0's ○ and the translation path's job, not a length comparison."""
    assert _check({"nl": "", "fr": LONG_FR}) == []
    assert _check({"fr": LONG_FR}) == []


def test_it_works_either_way_round() -> None:
    issues = _check({"nl": " ".join(["woord"] * 150), "fr": "Un texte de treize mots"})

    assert [i.field for i in issues] == ["description_short.fr"]
