"""Unit tests for lib/eligibility.py — the Data screen's split of a batch, and lib/batch_reset.py.

Eligibility is a join of :func:`lib.holds.held_products` and the video status, so the tests pin it
to those: a product is eligible exactly when the plan would not hold it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from lib.batch_reset import NOTE_NAME, clear_batch
from lib.config import (
    ClientConfig,
    ExportConfig,
    GeneratorConfig,
    GS1Config,
    MediaConfig,
    WordPressConfig,
)
from lib.eligibility import (
    CHECKING,
    NO_IMAGE,
    NO_TARGET,
    LinkIssue,
    eligibility,
    eligibility_for,
    link_targets,
)
from lib.gates import Mode
from lib.gdsn import GdsnSource
from lib.holds import held_units
from lib.records import LocalisedText, ProductRecord

_A = "02987653007359"  # nl + fr video
_B = "02987653007360"  # nl video only
_C = "02987653007361"  # two nl videos, one fr
_D = "02987653007362"  # no video, and missing net content
_LANGS = ["nl", "fr"]


def _config(media: MediaConfig, **overrides: Any) -> ClientConfig:
    params: dict[str, Any] = {
        "client_id": "acme",
        "display_name": "Acme BV",
        "gs1": GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
        ),
        "export": ExportConfig(
            path="input/acme.xlsx",
            gdsn_map={"net_content": GdsnSource(sheet="S", attribute="3510", required=True)},
        ),
        "wordpress": WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=_LANGS,
            slug_pattern="p-{gtin}",
            target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
        ),
        "generator": GeneratorConfig(enabled=True),
        "media": media,
    }
    params.update(overrides)
    return ClientConfig(**params)


def _product(gtin: str, **fields: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": gtin,
        "brand": "Acme",
        "product_name": LocalisedText(values={"nl": "haak", "fr": "crochet"}),
        "image_url": "https://cdn.test/a.jpg",
        "net_content": "1 H87",
    }
    return ProductRecord.model_validate({**base, **fields})


def _media(tmp_path: Path, *, publish_without_video: bool) -> MediaConfig:
    entries = {
        "nl": [
            {"file": "a_nl.mp4", "gtin": _A},
            {"file": "b_nl.mp4", "gtin": _B},
            {"file": "c1.mp4", "gtin": _C},
            {"file": "c2.mp4", "gtin": _C},
        ],
        "fr": [{"file": "a_fr.mp4", "gtin": _A}, {"file": "c_fr.mp4", "gtin": _C}],
    }
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return MediaConfig(
        video_map_path=str(path),
        restrict_to_mapped_gtins=True,
        publish_without_video=publish_without_video,
        require_hero_image=True,
    )


def _products() -> list[ProductRecord]:
    return [_product(_A), _product(_B), _product(_C), _product(_D, net_content=None)]


@pytest.mark.parametrize("setting", [False, True])
def test_eligible_is_exactly_what_the_plan_does_not_hold(tmp_path: Path, setting: bool) -> None:
    """The anti-drift pin: a tick box is offered on a product iff the plan would publish it."""
    cfg = _config(_media(tmp_path, publish_without_video=setting))
    products = _products()

    verdict = eligibility(cfg, products)

    held = {gtin for gtin, _ in held_units(cfg, products)}
    assert set(verdict.not_eligible) == held
    assert {p.gtin for p in products if verdict.is_eligible(p.gtin14)} == {
        p.gtin for p in products
    } - held


def test_publishing_without_video_marks_rather_than_holds(tmp_path: Path) -> None:
    verdict = eligibility(_config(_media(tmp_path, publish_without_video=True)), _products())

    assert verdict.missing_video == {_B: "no confirmed video in fr"}
    assert verdict.not_eligible[_C] == "two videos in nl — the client must keep one"
    assert verdict.not_eligible[_D].startswith("missing data: net_content")


def test_with_the_pilot_rule_a_missing_video_is_the_reason(tmp_path: Path) -> None:
    verdict = eligibility(_config(_media(tmp_path, publish_without_video=False)), _products())

    assert verdict.not_eligible[_B] == "no confirmed video in fr"
    assert verdict.missing_video == {}


def test_missing_data_outranks_the_video_as_the_plan_attributes_it(tmp_path: Path) -> None:
    """``_D`` has no video either; the plan names E23 first, so the screen does too."""
    verdict = eligibility(_config(_media(tmp_path, publish_without_video=True)), _products())

    assert "missing data" in verdict.not_eligible[_D]
    assert _D not in verdict.missing_video


def test_a_product_held_for_data_also_names_its_two_videos(tmp_path: Path) -> None:
    """Insectenval, on the pilot: image_url blank *and* two nl videos. Both need fixing."""
    cfg = _config(_media(tmp_path, publish_without_video=True))

    verdict = eligibility(cfg, [_product(_C, net_content=None)])

    assert verdict.not_eligible[_C] == (
        "missing data: net_content (attr 3510); two videos in nl — the client must keep one"
    )


def test_without_the_video_rule_no_video_words_are_a_reason(tmp_path: Path) -> None:
    media = _media(tmp_path, publish_without_video=False).model_copy(
        update={"restrict_to_mapped_gtins": False}
    )

    verdict = eligibility(_config(media), [_product(_D, net_content=None)])

    assert verdict.not_eligible[_D] == "missing data: net_content (attr 3510)"


def test_a_blank_source_image_is_named(tmp_path: Path) -> None:
    cfg = _config(_media(tmp_path, publish_without_video=True))

    verdict = eligibility(cfg, [_product(_A, image_url="")])

    assert verdict.not_eligible == {_A: NO_IMAGE}


def test_an_unreadable_mapping_makes_nothing_eligible(tmp_path: Path) -> None:
    """Fail closed, as a run with the rule on would: it holds everything."""
    broken = tmp_path / "broken.yml"
    broken.write_text("nl:\n\t- stray tab\n", encoding="utf-8")
    media = MediaConfig(video_map_path=str(broken), restrict_to_mapped_gtins=True)

    verdict = eligibility(_config(media), [_product(_A)])

    assert verdict.problem is not None
    assert not verdict.is_eligible(_A)


# --- lib/batch_reset ---------------------------------------------------------------------------


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    process = tmp_path / "input" / "acme" / "process"
    export = process / "uploads" / "GS1 export" / "export.xlsx"
    selection = process / "selection" / "selections.xlsx"
    upload = process / "uploads" / "product-list.xlsx"
    products = tmp_path / "output" / "acme" / "data" / "products.json"
    for path in (export, selection, upload, products):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    return export, selection, upload, products


def test_clearing_moves_the_four_live_files_aside_and_deletes_nothing(tmp_path: Path) -> None:
    export, selection, upload, products = _layout(tmp_path)
    archive = upload.with_name("product-list-20261001T000000.xlsx")
    archive.write_text("dated", encoding="utf-8")

    moved = clear_batch(export=export, selection=selection, products=products, stamp="S")

    folder = tmp_path / "input" / "acme" / "superseded" / "cleared-S"
    assert {p.name for p in moved} == {
        "export.xlsx",
        "selections.xlsx",
        "product-list.xlsx",
        "products.json",
    }
    assert all(p.parent == folder for p in moved)
    assert not any(p.exists() for p in (export, selection, upload, products))
    assert archive.read_text(encoding="utf-8") == "dated"  # dated copies stay where they are
    assert (folder / NOTE_NAME).is_file()
    assert (folder / "export.xlsx").read_text(encoding="utf-8") == "export.xlsx"


def test_clearing_nothing_makes_no_folder(tmp_path: Path) -> None:
    export, selection, upload, products = _layout(tmp_path)
    for path in (export, selection, upload, products):
        path.unlink()

    assert clear_batch(export=export, selection=selection, products=products, stamp="S") == []
    assert not (tmp_path / "input" / "acme" / "superseded").exists()


def test_clearing_twice_in_one_second_keeps_both(tmp_path: Path) -> None:
    export, selection, _, products = _layout(tmp_path)
    clear_batch(export=export, selection=selection, products=products, stamp="S")
    _layout(tmp_path)

    clear_batch(export=export, selection=selection, products=products, stamp="S")

    assert len(list((tmp_path / "input" / "acme" / "superseded").iterdir())) == 2


def test_a_path_outside_the_layout_is_refused_before_anything_moves(tmp_path: Path) -> None:
    stray = tmp_path / "export.xlsx"
    stray.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="process/"):
        clear_batch(export=stray, selection=stray, products=stray, stamp="S")
    assert stray.exists()


# --- a links-only batch is judged on its link ---------------------------------------


def test_a_links_only_batch_ignores_video_and_missing_data(tmp_path: Path) -> None:
    """Operator, 2026-10-09: a links run writes no page, so only a bad link may stop a product."""
    cfg = _config(_media(tmp_path, publish_without_video=False))
    targets = {gtin: f"https://wp.test/product/{gtin}/" for gtin in (_A, _B, _C, _D)}
    checked = dict.fromkeys(targets.values())

    verdict = eligibility_for(cfg, _products(), Mode.LINKS, targets=targets, checked=checked)

    assert verdict.not_eligible == {}
    assert verdict.missing_video == {}
    assert verdict.bad_link == {}
    assert all(verdict.is_eligible(gtin) for gtin in (_A, _B, _C, _D))


def test_a_links_only_product_is_stopped_only_by_its_link(tmp_path: Path) -> None:
    cfg = _config(_media(tmp_path, publish_without_video=False))
    targets = {
        _A: "https://wp.test/product/a/",
        _B: "https://elsewhere.test/b/",
        _C: "https://wp.test/product/gone/",
    }
    checked = {"https://wp.test/product/a/": None, "https://wp.test/product/gone/": "404"}

    verdict = eligibility_for(cfg, _products(), Mode.LINKS, targets=targets, checked=checked)

    assert verdict.is_eligible(_A)
    assert verdict.bad_link[_B] == LinkIssue(
        "https://elsewhere.test/b/", "not on the client's site (wp.test)"
    )
    assert verdict.bad_link[_C] == LinkIssue("https://wp.test/product/gone/", "404")
    assert verdict.bad_link[_D] == LinkIssue(None, NO_TARGET)


def test_an_unchecked_link_cannot_run_yet(tmp_path: Path) -> None:
    cfg = _config(_media(tmp_path, publish_without_video=False))

    verdict = eligibility_for(
        cfg, [_product(_A)], Mode.LINKS, targets={_A: "https://wp.test/a/"}, checked={}
    )

    assert verdict.bad_link[_A].problem == CHECKING
    assert not verdict.is_eligible(_A)


@pytest.mark.parametrize("mode", [Mode.PAGES, Mode.BOTH, None])
def test_a_batch_that_writes_pages_is_judged_as_before(tmp_path: Path, mode: Mode | None) -> None:
    cfg = _config(_media(tmp_path, publish_without_video=True))

    assert eligibility_for(cfg, _products(), mode, targets={}, checked={}) == eligibility(
        cfg, _products()
    )


def test_the_listed_address_wins_over_our_own_page() -> None:
    from lib.records import State  # noqa: PLC0415

    state = State.model_validate(
        {
            "client_id": "acme",
            "entries": {
                _A: {
                    lang: {
                        "content_hash": "h",
                        "wp_page_id": 1,
                        "wp_url": f"https://wp.test/{lang}/ours/",
                        "wp_status": "publish",
                        "last_run": "2026-10-08T00:00:00Z",
                        "wp_featured_media_id": None,
                        "gs1_link_set_hash": "",
                    }
                    for lang in _LANGS
                },
                _B: {
                    "nl": {
                        "content_hash": "h",
                        "wp_page_id": 2,
                        "wp_url": "https://wp.test/nl/b/",
                        "wp_status": "publish",
                        "last_run": "2026-10-08T00:00:00Z",
                        "wp_featured_media_id": None,
                        "gs1_link_set_hash": "",
                    }
                },
            },
        }
    )
    cfg = _config(MediaConfig())

    targets = link_targets(cfg, {_B: "https://wp.test/listed-b/"}, state)

    assert targets == {_A: "https://wp.test/nl/ours/", _B: "https://wp.test/listed-b/"}
