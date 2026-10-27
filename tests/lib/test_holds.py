"""Unit tests for lib/holds.py — which products the plan will hold, asked before there is a plan.

The point of this module is that a caller running *before* the plan can stop paying for work the
plan will throw away, so the tests that matter are the two directions it can be wrong in. Holding
too little wastes what it was written to save. Holding too much is worse and quieter: a unit
nobody generates copy for is a unit E21 then drops from the plan, so a product that would have
published simply does not, and no surface says why.

That second direction is what the translation cases are about. The plan checks E23 on the
*post-generation* records, so a value the feed carries in one language and the client has marked
``translate: true`` is not a gap by the time the plan asks. This must reach the same answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from lib.config import (
    ClientConfig,
    ExportConfig,
    GeneratorConfig,
    GS1Config,
    MediaConfig,
    WordPressConfig,
)
from lib.errors import VideoMapError
from lib.gdsn import GdsnSource
from lib.holds import held_units, video_gate_for
from lib.records import LocalisedText, ProductRecord, SkipReason

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"
LANGS = ["nl", "fr"]


# --- Builders ----------------------------------------------------------------


def _config(**overrides: Any) -> ClientConfig:
    params: dict[str, Any] = {
        "client_id": "acme",
        "display_name": "Acme BV",
        "gs1": GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
        ),
        "export": ExportConfig(path="input/acme.xlsx"),
        "wordpress": WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=LANGS,
            slug_pattern="p-{gtin}",
            target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
        ),
        "generator": GeneratorConfig(enabled=True),
    }
    params.update(overrides)
    return ClientConfig(**params)


def _export(**sources: GdsnSource) -> ExportConfig:
    return ExportConfig(path="input/acme.xlsx", gdsn_map=dict(sources))


def _product(gtin: str = GTIN_A, **fields: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": gtin,
        "brand": "Acme",
        "product_name": LocalisedText(values={"nl": "haak", "fr": "crochet"}),
        "image_url": "https://cdn.test/a.jpg",
    }
    return ProductRecord.model_validate({**base, **fields})


def _video_map(tmp_path: Path, confirmed: dict[str, list[str]]) -> MediaConfig:
    """A video map confirming ``{language: [gtin, …]}``, wired into a restricting MediaConfig."""
    entries = {
        language: [{"file": f"{g}_{language}.mp4", "gtin": g, "confirmed": True} for g in gtins]
        for language, gtins in confirmed.items()
    }
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return MediaConfig(video_map_path=str(path), restrict_to_mapped_gtins=True)


# --- video_gate_for ----------------------------------------------------------


def test_no_media_config_disables_the_video_hold_rather_than_holding_everything() -> None:
    """``None``, not a closed gate — the difference between "unrestricted" and "nothing passes"."""
    assert video_gate_for(_config()) is None


def test_a_media_block_that_does_not_restrict_disables_the_video_hold(tmp_path: Path) -> None:
    media = _video_map(tmp_path, {"nl": [GTIN_A], "fr": [GTIN_A]}).model_copy(
        update={"restrict_to_mapped_gtins": False}
    )

    gate = video_gate_for(_config(media=media))

    assert gate is not None, "unenforced, it still answers which file a page gets"
    assert not gate.enforced
    assert gate.admits(GTIN_B), "holds nothing"
    assert held_units(_config(media=media), []) == {}


def test_an_unreadable_map_on_an_unrestricted_client_is_no_gate_not_an_error(
    tmp_path: Path,
) -> None:
    """It decides nothing about what may publish, so it must not start failing runs."""
    broken = tmp_path / "broken.yml"
    broken.write_text("nl:\n\t- stray tab\n", encoding="utf-8")
    media = MediaConfig(video_map_path=str(broken), restrict_to_mapped_gtins=False)

    assert video_gate_for(_config(media=media)) is None


def test_a_video_is_needed_in_every_language(tmp_path: Path) -> None:
    """ "Fully mapped" means every configured language, which is what holds the nl-only GTINs."""
    media = _video_map(tmp_path, {"nl": [GTIN_A, GTIN_B], "fr": [GTIN_A]})

    gate = video_gate_for(_config(media=media))

    assert gate is not None
    assert gate.confirmed == frozenset({GTIN_A})
    assert gate.admits(GTIN_A)
    assert not gate.admits(GTIN_B)


def test_publish_without_video_admits_the_nl_only_gtin(tmp_path: Path) -> None:
    media = _video_map(tmp_path, {"nl": [GTIN_A, GTIN_B], "fr": [GTIN_A]}).model_copy(
        update={"publish_without_video": True}
    )

    gate = video_gate_for(_config(media=media))

    assert gate is not None
    assert gate.admits(GTIN_B)


# --- E24: no confirmed video --------------------------------------------------


def test_a_gtin_without_a_confirmed_video_is_held_in_every_language(tmp_path: Path) -> None:
    cfg = _config(media=_video_map(tmp_path, {"nl": [GTIN_A], "fr": [GTIN_A]}))

    held = held_units(cfg, [_product(GTIN_A), _product(GTIN_B)])

    assert held == {
        (GTIN_B, "nl"): SkipReason.NO_CONFIRMED_VIDEO,
        (GTIN_B, "fr"): SkipReason.NO_CONFIRMED_VIDEO,
    }


def test_an_unreadable_video_map_raises_rather_than_holding_nothing(tmp_path: Path) -> None:
    """Silently returning "nothing is held" would generate for products the plan then drops.

    The doctor catches this and gives up wide; ``run_plan`` lets it stop the run. Either beats a
    hold that quietly stops applying because a file moved.
    """
    path = tmp_path / "mapping.yml"
    path.write_text("nl: [", encoding="utf-8")
    cfg = _config(media=MediaConfig(video_map_path=str(path), restrict_to_mapped_gtins=True))

    with pytest.raises(VideoMapError):
        held_units(cfg, [_product()])


# --- E23: missing mandatory source data ---------------------------------------


def test_a_missing_mandatory_value_holds_the_product() -> None:
    cfg = _config(
        export=_export(net_content=GdsnSource(sheet="S", attribute="3510", required=True))
    )

    held = held_units(cfg, [_product(net_content=None)])

    assert set(held.values()) == {SkipReason.MISSING_MANDATORY_FIELD}
    assert set(held) == {(GTIN_A, "nl"), (GTIN_A, "fr")}


def test_a_language_gap_the_producer_will_translate_is_not_a_hold() -> None:
    """The case that must never regress: this runs before generation, the plan runs after it.

    ``product_name`` is marked ``translate: true`` and the feed carries nl, so by the time the plan
    asks E23 the producer has filled fr and the product publishes. Holding it here would mean no
    copy is written for it, E21 then drops it, and a publishable SKU silently does not publish.
    """
    cfg = _config(
        export=_export(
            product_name=GdsnSource(
                sheet="S", attribute="3301", localised=True, required=True, translate=True
            )
        )
    )
    product = _product(product_name=LocalisedText(values={"nl": "haak"}))

    assert held_units(cfg, [product]) == {}


def test_a_value_blank_in_every_language_is_a_hold_even_when_translatable() -> None:
    """Translation renders a value the feed holds; it never invents one it does not.

    Same source, same flag as the test above — only the data differs. That is the whole line
    ``translation_gaps`` draws, and E23 is on the far side of it.
    """
    cfg = _config(
        export=_export(
            product_name=GdsnSource(
                sheet="S", attribute="3301", localised=True, required=True, translate=True
            )
        )
    )
    product = _product(product_name=LocalisedText(values={}))

    assert set(held_units(cfg, [product]).values()) == {SkipReason.MISSING_MANDATORY_FIELD}


def test_a_translatable_field_not_marked_translate_is_still_a_hold() -> None:
    """``translate`` is the client's decision about its own page, so an unset flag means no fill."""
    cfg = _config(
        export=_export(
            product_name=GdsnSource(sheet="S", attribute="3301", localised=True, required=True)
        )
    )
    product = _product(product_name=LocalisedText(values={"nl": "haak"}))

    assert set(held_units(cfg, [product]).values()) == {SkipReason.MISSING_MANDATORY_FIELD}


def test_one_translatable_member_satisfies_a_required_group() -> None:
    """A group gap is named for the group, so it has to be resolved back to its members.

    Ask the group name of ``translation_gaps`` and nothing ever matches: it answers in field names.
    The product would then be held for a gap the producer was about to close.
    """
    cfg = _config(
        export=_export(
            description_short=GdsnSource(
                sheet="S",
                attribute="1083",
                localised=True,
                required_group="marketing_copy",
                translate=True,
            ),
            description_long=GdsnSource(
                sheet="S",
                attribute="1067",
                localised=True,
                required_group="marketing_copy",
                translate=True,
            ),
        )
    )
    carried = {"description_short": LocalisedText(values={"nl": "sterk"})}
    product = _product(extras_localised=carried)

    assert held_units(cfg, [product]) == {}


def test_a_required_group_blank_in_every_language_holds_the_product() -> None:
    """Democlient's real E23: attr 1083 and 1067 both empty, which only MyGS1 can fix."""
    cfg = _config(
        export=_export(
            description_short=GdsnSource(
                sheet="S",
                attribute="1083",
                localised=True,
                required_group="marketing_copy",
                translate=True,
            ),
            description_long=GdsnSource(
                sheet="S",
                attribute="1067",
                localised=True,
                required_group="marketing_copy",
                translate=True,
            ),
        )
    )

    assert set(held_units(cfg, [_product()]).values()) == {SkipReason.MISSING_MANDATORY_FIELD}


def test_required_on_a_gdsn_extra_holds_the_product_too() -> None:
    """``all_sources``, not ``gdsn_map`` — the #101 bug, which E23 is the worst place to have."""
    export = ExportConfig(
        path="input/acme.xlsx",
        gdsn_extras={"dim_height": GdsnSource(sheet="S", attribute="3498", required=True)},
    )

    held = held_units(_config(export=export), [_product()])

    assert set(held.values()) == {SkipReason.MISSING_MANDATORY_FIELD}


# --- E22: blank hero image ----------------------------------------------------


def test_a_blank_hero_image_holds_the_product_when_required(tmp_path: Path) -> None:
    media = _video_map(tmp_path, {"nl": [GTIN_A], "fr": [GTIN_A]}).model_copy(
        update={"require_hero_image": True}
    )

    held = held_units(_config(media=media), [_product(image_url="  ")])

    assert set(held.values()) == {SkipReason.BLANK_HERO_IMAGE}


def test_a_blank_hero_image_is_not_a_hold_when_not_required() -> None:
    assert held_units(_config(), [_product(image_url=None)]) == {}


# --- Attribution --------------------------------------------------------------


def test_the_first_rule_that_fires_is_the_one_reported(tmp_path: Path) -> None:
    """E23 before E24 before E22 — ``diff_against_state``'s order, so the plan says the same."""
    cfg = _config(
        export=_export(net_content=GdsnSource(sheet="S", attribute="3510", required=True)),
        media=_video_map(tmp_path, {"nl": [], "fr": []}),
    )

    held = held_units(cfg, [_product(net_content=None)])

    assert set(held.values()) == {SkipReason.MISSING_MANDATORY_FIELD}


def test_a_client_with_no_generator_still_gets_the_holds() -> None:
    """Nothing is translatable without a generator, so every mandatory gap is unfillable."""
    cfg = _config(
        generator=None,
        export=_export(
            product_name=GdsnSource(
                sheet="S", attribute="3301", localised=True, required=True, translate=True
            )
        ),
    )
    product = _product(product_name=LocalisedText(values={"nl": "haak"}))

    assert set(held_units(cfg, [product]).values()) == {SkipReason.MISSING_MANDATORY_FIELD}


def test_nothing_is_held_when_no_rule_is_configured() -> None:
    assert held_units(_config(), [_product(GTIN_A), _product(GTIN_B)]) == {}
