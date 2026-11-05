"""``gs1.link_languages``: which languages get a link in the GS1 record.

Noviplast, 2026-11-05: with an nl and an fr link, GS1 makes both of them a per-language default
(``gs1:defaultLinkMulti``), however the fr link is flagged. A phone whose language list names
neither nl nor fr — English in the Philippines, English in Belgium — then gets id.gs1.org's
"linkset" choice screen instead of a product page. Removing the fr link by hand from one record
(08713195001593) sent every phone straight to the Dutch page. ``default_only`` is that, as a
setting, so a links run does not put the fr link back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    GS1LinkConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.link_set import build_links, link_is_stale, link_set_hash, resolver_pages
from lib.records import LocalisedText, ProductRecord, StateEntry

_GTIN = "02987653000001"
_NL = "https://wp.test/product/p-02987653000001/"
_FR = "https://wp.test/fr/product/p-02987653000001/"


class _Page:
    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self.title = title


def _cfg(link_languages: str = "all") -> ClientConfig:
    gs1: dict[str, Any] = {
        "account_number_test": "8712345000010",
        "client_id_env_test": "GS1_CID",
        "client_secret_env_test": "GS1_SEC",
        "link_languages": link_languages,
    }
    return ClientConfig(
        client_id="acme",
        display_name="Acme BV",
        gs1=GS1Config.model_validate(gs1),
        export=ExportConfig(path="input/acme.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            default_language="nl",
            languages=["nl", "fr"],
        ),
        template=TemplateConfig(override_dir=None),
        gs1_links=[
            GS1LinkConfig(link_type="gs1:pip", default=True, title_pattern="{product_name}")
        ],
    )


def _product() -> ProductRecord:
    return ProductRecord(
        gtin=_GTIN,
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Treklamp", "fr": "Lampe à tirette"}),
    )


_PAGES = {"nl": _Page(_NL, "Treklamp"), "fr": _Page(_FR, "Lampe à tirette")}


def test_every_language_gets_a_link_by_default() -> None:
    links = build_links(_cfg(), _product(), _PAGES)

    assert [(link["language"], link["default_link_type"]) for link in links] == [
        ("fr", False),
        ("nl", True),
    ]


def test_default_only_writes_the_default_language_link_alone() -> None:
    links = build_links(_cfg("default_only"), _product(), _PAGES)

    assert [(link["language"], link["target_url"]) for link in links] == [("nl", _NL)]
    assert links[0]["default_link_type"] is True


def test_resolver_pages_keeps_only_what_the_record_will_link() -> None:
    assert set(resolver_pages(_cfg(), _PAGES)) == {"nl", "fr"}
    assert set(resolver_pages(_cfg("default_only"), _PAGES)) == {"nl"}


def test_default_only_without_a_default_language_page_is_refused() -> None:
    # GS1 replaces the links array wholesale: an empty set would strip the record bare.
    with pytest.raises(ValueError, match="no nl page"):
        build_links(_cfg("default_only"), _product(), {"fr": _PAGES["fr"]})


def test_switching_to_default_only_makes_a_two_language_record_stale() -> None:
    # This is how the records already live get rewritten: the next plan marks them CHANGED, and
    # a links run writes them without the fr link.
    written = link_set_hash(build_links(_cfg(), _product(), _PAGES))
    entries = {
        language: StateEntry(
            wp_page_id=1,
            wp_featured_media_id=None,
            wp_url=page.url,
            title=page.title,
            content_hash="c",
            gs1_link_set_hash=written,
            last_run=datetime(2026, 11, 5, tzinfo=UTC),
        )
        for language, page in _PAGES.items()
    }

    assert link_is_stale(_cfg(), _product(), entries) is False
    assert link_is_stale(_cfg("default_only"), _product(), entries) is True


def test_an_unknown_value_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="link_languages"):
        _cfg("nl")
