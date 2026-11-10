"""Tests for reading a live page's copy back (``lib.live_default_copy``).

Pinned against the generator's own assembly, so a change to the description's shape breaks here
rather than on the site: what :func:`lib.generator._assemble_description` writes is what
:func:`usps_from` must read back, and a block it did not write is refused, never guessed at.
"""

from __future__ import annotations

from typing import Any

import pytest

from lib.config import WordPressConfig
from lib.errors import WordPressAPIError
from lib.generator import _assemble_description, _labels
from lib.live_default_copy import read_live_copy, usps_from
from lib.records import LocalisedText, ProductRecord

NL = _labels("nl")


def _product() -> ProductRecord:
    return ProductRecord(
        gtin="08713195006109",
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Pet spa"}),
        net_content="1 H87",
        extras={"material": "kunststof"},
    )


def test_the_generators_own_description_reads_back_as_its_usps() -> None:
    usps = ["Verzorg je huisdier", "Borstel & vernevelaar in één", "Voor elke vacht"]
    description = _assemble_description(usps, _product(), "nl", "nl")

    assert usps_from(usps[0], description, NL) == usps


def test_a_tagline_with_no_bullets_reads_back_as_one_line() -> None:
    description = _assemble_description(["Alleen een slogan"], _product(), "nl", "nl")

    assert usps_from("Alleen een slogan", description, NL) == ["Alleen een slogan"]


def test_a_block_the_generator_does_not_write_is_refused() -> None:
    description = "<p><strong>Waarom kiezen</strong><br />• Handgeschreven</p>"

    assert usps_from("Slogan", description, NL) is None


def test_a_description_not_in_the_generators_shape_is_refused_not_read_as_a_tagline() -> None:
    """Read as tagline-only, it would rewrite the French page without its bullets."""
    listed = "<p><strong>T</strong></p><p><strong>Eigenschappen</strong></p><ul><li>a</li></ul>"

    assert usps_from("T", "", NL) is None
    assert usps_from("T", "<p>Handgeschreven tekst</p>", NL) is None
    assert usps_from("T", "<p><strong>T</strong></p>\n<p>Handgeschreven</p>", NL) is None
    assert usps_from("T", listed, NL) is None


def test_no_tagline_is_nothing_to_translate() -> None:
    assert usps_from("  ", "<p><strong>x</strong></p>", NL) is None


class _Client:
    def __init__(self, pages: dict[int, dict[str, Any]]) -> None:
        self.pages = pages
        self.asked: list[int] = []

    def read_page(self, _post_type: str, page_id: int) -> dict[str, Any] | None:
        self.asked.append(page_id)
        return self.pages.get(page_id)


def test_each_page_is_read_by_id_and_an_unreadable_one_is_left_out() -> None:
    wordpress = WordPressConfig(
        site_url="https://wp.test",
        username="bot",
        app_password_env="WP_PASS",
        acf_map={
            "product_title": "generated_tagline",
            "product_description": "generated_description",
        },
    )
    good = _assemble_description(["Slogan", "Punt"], _product(), "nl", "nl")
    client = _Client(
        {
            1: {"acf": {"product_title": "Slogan", "product_description": good}},
            2: {"acf": []},  # what a language-scoped read returns on this site
        }
    )

    found = read_live_copy(client, wordpress, {"a": 1, "b": 2, "c": 3}, NL)  # type: ignore[arg-type]

    assert found == {"a": ["Slogan", "Punt"]}
    assert client.asked == [1, 2, 3]


def _wordpress() -> WordPressConfig:
    return WordPressConfig(
        site_url="https://wp.test",
        username="bot",
        app_password_env="WP_PASS",
        acf_map={
            "product_title": "generated_tagline",
            "product_description": "generated_description",
        },
    )


class _Refusing(_Client):
    def __init__(self, pages: dict[int, dict[str, Any]], refuse: dict[int, int]) -> None:
        super().__init__(pages)
        self.refuse = refuse

    def read_page(self, post_type: str, page_id: int) -> dict[str, Any] | None:
        if page_id in self.refuse:
            raise WordPressAPIError(self.refuse[page_id], "no")
        return super().read_page(post_type, page_id)


def test_a_page_the_site_will_not_serve_costs_only_that_page() -> None:
    good = _assemble_description(["Slogan"], _product(), "nl", "nl")
    client = _Refusing(
        {2: {"acf": {"product_title": "Slogan", "product_description": good}}}, {1: 500}
    )

    found = read_live_copy(client, _wordpress(), {"a": 1, "b": 2}, NL)  # type: ignore[arg-type]

    assert found == {"b": ["Slogan"]}
    with pytest.raises(WordPressAPIError):
        read_live_copy(_Refusing({}, {1: 401}), _wordpress(), {"a": 1}, NL)  # type: ignore[arg-type]
