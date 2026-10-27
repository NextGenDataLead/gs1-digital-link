"""Tests for scripts/set_categories.py — live pages into their category, nothing else changed.

Operator, 2026-10-08: pages "are created but are they also being shown on category pages?" — none
was. New pages get their category from run_execute now; this puts the ones already live there
without rewriting them (a pages run would regenerate their copy).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lib.config import (
    CategoryConfig,
    ClientConfig,
    ExportConfig,
    GS1Config,
    TaxonomyConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.errors import GtinMismatchError
from lib.records import LocalisedText, ProductRecord, State, StateEntry
from lib.state import save_state
from scripts import set_categories

A, B, C = "02987653000527", "02987653000961", "02987653003344"
_TAX = "product-categories"


def _cfg(**wordpress: Any) -> ClientConfig:
    return ClientConfig(
        client_id="acme",
        display_name="Acme BV",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
            digital_link_url_pattern="https://id.gs1.org/01/{gtin14}",
        ),
        export=ExportConfig(path="input/acme.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl", "fr"],
            **{"taxonomies": {_TAX: TaxonomyConfig(map_from_column="category")}, **wordpress},
        ),
        template=TemplateConfig(override_dir=None),
        categories=CategoryConfig(
            terms=["keuken", "outdoor_dier"],
            brick_category_map={"10002147": "keuken", "10003254": "outdoor_dier"},
        ),
    )


def _product(gtin: str, brick: str | None) -> dict[str, Any]:
    return ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values={"nl": "x", "fr": "x"}),
        gpc_brick_code=brick,
    ).model_dump(mode="json")


def _entry(gtin: str, lang: str, page_id: int, **over: Any) -> StateEntry:
    return StateEntry(
        wp_page_id=page_id,
        wp_url=f"https://wp.test/{lang}/p-{gtin}/",
        wp_featured_media_id=None,
        content_hash="c",
        gs1_link_set_hash="g",
        last_run=datetime(2026, 10, 8, tzinfo=UTC),
        **over,
    )


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A: keuken. B: outdoor_dier. C: an unmapped brick. Plus one taken-down product."""
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "output" / "acme" / "data"
    data.mkdir(parents=True)
    products = [_product(A, "10002147"), _product(B, "10003254"), _product(C, "99999999")]
    (data / "products.json").write_text(json.dumps(products), encoding="utf-8")
    down = "02987653000473"
    save_state(
        State(
            client_id="acme",
            entries={
                A: {"nl": _entry(A, "nl", 1), "fr": _entry(A, "fr", 2)},
                B: {"nl": _entry(B, "nl", 3)},
                C: {"nl": _entry(C, "nl", 5)},
                down: {"nl": _entry(down, "nl", 9, wp_status="draft")},
            },
        )
    )


class _Site:
    def __init__(self, terms: dict[tuple[str, str], int], on_page: dict[int, list[int]]) -> None:
        self.terms = terms
        self.on_page = on_page
        self.writes: list[tuple[int, str, dict[str, list[int]]]] = []
        self.foreign: set[int] = set()


def _install(monkeypatch: pytest.MonkeyPatch, cfg: ClientConfig, site: _Site) -> None:
    class FakeWP:
        def __init__(self, config: WordPressConfig) -> None:
            pass

        def __enter__(self) -> FakeWP:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def term_id(self, taxonomy: str, term: str, language: str) -> int | None:
            return site.terms.get((term, language))

        def page_terms(self, post_type: str, page_id: int, gtin: str, taxonomy: str) -> list[int]:
            if page_id in site.foreign:
                raise GtinMismatchError(gtin, "08700000000000", page_id)
            return site.on_page.get(page_id, [])

        def set_page_terms(
            self, post_type: str, page_id: int, gtin: str, terms: dict[str, list[int]]
        ) -> None:
            site.writes.append((page_id, gtin, terms))
            site.on_page[page_id] = terms[_TAX]

    monkeypatch.setattr(set_categories, "get_client", lambda _cid: cfg)
    monkeypatch.setattr(set_categories, "WordPressClient", FakeWP)


_TERMS = {("keuken", "nl"): 5, ("keuken", "fr"): 22, ("outdoor_dier", "nl"): 7}


def test_a_dry_run_says_what_it_would_set_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)
    site = _Site(_TERMS, on_page={})
    _install(monkeypatch, _cfg(), site)

    assert set_categories.main([]) == 0

    assert site.writes == []
    out = capsys.readouterr().out
    assert f"would  {A}/nl" in out and "→ 'keuken' (5)" in out
    assert f"would  {A}/fr" in out and "→ 'keuken' (22)" in out  # the French term, not the Dutch
    assert f"skip   {C}/nl" in out  # unmapped brick: nothing to set
    assert "02987653000473" not in out  # taken down: not live, not touched


def test_apply_sets_each_page_once_and_a_second_run_finds_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)
    site = _Site(_TERMS, on_page={3: [7]})  # B is already in outdoor_dier
    _install(monkeypatch, _cfg(), site)

    assert set_categories.main(["--apply"]) == 0
    assert sorted(site.writes) == [(1, A, {_TAX: [5]}), (2, A, {_TAX: [22]})]

    capsys.readouterr()
    assert set_categories.main(["--apply"]) == 0
    assert len(site.writes) == 2  # idempotent
    assert "already in 'keuken'" in capsys.readouterr().out


def test_a_term_the_site_lacks_is_an_error_for_that_page_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)
    site = _Site({("keuken", "nl"): 5, ("keuken", "fr"): 22}, on_page={})  # no outdoor_dier
    _install(monkeypatch, _cfg(), site)

    assert set_categories.main(["--apply"]) == 1
    assert sorted(write[0] for write in site.writes) == [1, 2]  # A still done
    assert "no 'outdoor_dier' term in nl" in capsys.readouterr().out


def test_a_page_that_belongs_to_another_product_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _world(tmp_path, monkeypatch)
    site = _Site(_TERMS, on_page={})
    site.foreign = {1}  # a stale id in the ledger
    _install(monkeypatch, _cfg(), site)

    assert set_categories.main(["--apply"]) == 1
    assert 1 not in [write[0] for write in site.writes]
    assert f"ERROR  {A}/nl" in capsys.readouterr().out


def test_gtin_narrows_it_to_named_products(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(tmp_path, monkeypatch)
    site = _Site(_TERMS, on_page={})
    _install(monkeypatch, _cfg(), site)

    assert set_categories.main(["--apply", "--gtin", "2987653000961"]) == 0  # 13 digits is fine
    assert site.writes == [(3, B, {_TAX: [7]})]


def test_no_category_taxonomy_configured_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(tmp_path, monkeypatch)
    _install(monkeypatch, _cfg(taxonomies={}), _Site(_TERMS, on_page={}))

    assert set_categories.main([]) == 2
