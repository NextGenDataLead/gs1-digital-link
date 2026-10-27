"""Tests for the ACF payload assembler (docs/clients/democlient-page-adapter.md §3-§4.1)."""

from __future__ import annotations

import pytest

from lib.acf import build_acf_payload
from lib.records import LocalisedText, ProductRecord

_MAP = {"product_title": "description_short", "product_header_video_text": "description_short"}


def _product(**overrides: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": "02987653007359",
        "brand": "Democlient",
        "product_name": LocalisedText(values={"nl": "Rugsteun", "fr": "Support"}),
        "description_short": LocalisedText(
            values={"nl": "Steun voor je rug", "fr": "Support pour le dos"}
        ),
    }
    base.update(overrides)
    return ProductRecord(**base)


def test_localised_source_yields_this_languages_value() -> None:
    assert build_acf_payload(_product(), "nl", _MAP) == {
        "product_title": "Steun voor je rug",
        "product_header_video_text": "Steun voor je rug",
    }
    assert build_acf_payload(_product(), "fr", _MAP) == {
        "product_title": "Support pour le dos",
        "product_header_video_text": "Support pour le dos",
    }


def test_one_source_can_feed_several_acf_fields() -> None:
    """Democlient's tagline is one value written to two fields, as on the live pages."""
    payload = build_acf_payload(_product(), "nl", _MAP)

    assert payload["product_title"] == payload["product_header_video_text"]


def test_missing_language_omits_the_field_rather_than_falling_back() -> None:
    """A French page must not be given Dutch text — that is the silent-wrong-content shape."""
    product = _product(description_short=LocalisedText(values={"nl": "Alleen NL"}))

    assert build_acf_payload(product, "fr", _MAP) == {}
    assert build_acf_payload(product, "nl", _MAP)["product_title"] == "Alleen NL"


def test_absent_source_field_is_warned_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """A missing tagline must not stop the page being published with its title."""
    product = _product(description_short=None)

    with caplog.at_level("WARNING", logger="lib.acf"):
        payload = build_acf_payload(product, "nl", _MAP)

    assert payload == {}
    assert "no value for acf.product_title" in caplog.text
    assert "02987653007359" in caplog.text


def test_scalar_source_is_stringified() -> None:
    product = _product(net_content="5 H87")

    assert build_acf_payload(product, "nl", {"tech": "net_content"}) == {"tech": "5 H87"}


def test_extras_are_reachable_by_dotted_path() -> None:
    product = _product(extras={"functional_name": "microvezeldoek"})

    payload = build_acf_payload(product, "nl", {"fn": "extras.functional_name"})

    assert payload == {"fn": "microvezeldoek"}


def test_a_localised_extra_yields_this_languages_value() -> None:
    """The no-falling-back rule above applies to extras too, and used not to.

    ``extras.{name}`` returned one flat string whatever the page's language, so a French page
    took the Dutch token — exactly the failure the localised branch exists to prevent.
    """
    product = _product(
        extras_localised={
            "functional_name": LocalisedText(values={"nl": "microvezeldoek", "fr": "microfibre"})
        }
    )

    fn_map = {"fn": "extras.functional_name"}

    assert build_acf_payload(product, "fr", fn_map) == {"fn": "microfibre"}
    assert build_acf_payload(product, "nl", fn_map) == {"fn": "microvezeldoek"}


def test_a_localised_extra_missing_this_language_omits_the_field() -> None:
    product = _product(
        extras_localised={"functional_name": LocalisedText(values={"nl": "microvezeldoek"})}
    )

    assert build_acf_payload(product, "fr", {"fn": "extras.functional_name"}) == {}


def test_empty_map_yields_empty_payload() -> None:
    """A client with no acf_map renders from the body template — nothing to assemble."""
    assert build_acf_payload(_product(), "nl", {}) == {}


#: Democlient's live acf_map (generator SPEC, commit 8): the tagline feeds two slots, the
#: assembled description block feeds the third. Kept in sync with clients.yml / clients.example.yml.
_GENERATOR_MAP = {
    "product_title": "generated_tagline",
    "product_header_video_text": "generated_tagline",
    "product_description": "generated_description",
}


def test_generated_fields_resolve_to_acf_per_language() -> None:
    """The generator's LocalisedText fields reach ACF by getattr — no acf.py change needed."""
    product = _product(
        generated_tagline=LocalisedText(values={"nl": "Frisse voegen", "fr": "Joints frais"}),
        generated_description=LocalisedText(
            values={"nl": "<p>NL blok</p>", "fr": "<p>Bloc FR</p>"}
        ),
    )

    assert build_acf_payload(product, "nl", _GENERATOR_MAP) == {
        "product_title": "Frisse voegen",
        "product_header_video_text": "Frisse voegen",
        "product_description": "<p>NL blok</p>",
    }
    assert build_acf_payload(product, "fr", _GENERATOR_MAP) == {
        "product_title": "Joints frais",
        "product_header_video_text": "Joints frais",
        "product_description": "<p>Bloc FR</p>",
    }


def test_generated_gap_omits_field_so_page_still_publishes() -> None:
    """A missing generated value drops only that ACF field, never blocks the write."""
    product = _product(
        generated_tagline=LocalisedText(values={"nl": "Alleen NL"})  # no fr, no description
    )

    assert build_acf_payload(product, "fr", _GENERATOR_MAP) == {}
    nl = build_acf_payload(product, "nl", _GENERATOR_MAP)
    assert nl == {"product_title": "Alleen NL", "product_header_video_text": "Alleen NL"}
