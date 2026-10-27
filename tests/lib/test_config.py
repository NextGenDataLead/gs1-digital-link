"""Unit tests for the client config loader (IMPLEMENTATION_SPEC §2.4, §4.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from lib.config import GS1Config, get_client, load_clients, resolve_client_id
from lib.errors import ConfigError, ExportParseError
from lib.gs1_dl_client import GS1Config as ResolvedGS1Config


def _base_client() -> dict[str, Any]:
    return {
        "display_name": "Test Co",
        "gs1": {
            "account_number_test": "8712345000010",
            "client_id_env_test": "TEST_GS1_ID",
            "client_secret_env_test": "TEST_GS1_SECRET",
        },
        "export": {"path": "./input/test/products.xlsx"},
        "wordpress": {
            "site_url": "https://example.test",
            "username": "bot",
            "app_password_env": "TEST_WP_PASS",
        },
    }


def _write_config(
    tmp_path: Path, client: dict[str, Any], defaults: dict[str, Any] | None = None
) -> str:
    data: dict[str, Any] = {"version": 1, "clients": {"acme": client}}
    if defaults is not None:
        data["defaults"] = defaults
    path = tmp_path / "clients.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


# --- Loading & defaults ------------------------------------------------------


def test_loads_example_config_with_defaults_applied() -> None:
    clients = load_clients("clients.example.yml")

    democlient = clients["democlient"]
    assert democlient.display_name == "Democlient B.V."
    assert democlient.gs1.batch_size == 50  # inherited from defaults
    assert democlient.wordpress.post_status == "publish"  # inherited from defaults
    assert democlient.wordpress.multilingual_plugin == "wpml"  # client override
    assert democlient.wordpress.wpml_helper_path == "/wp-json/democlient/v1/translations"


def test_get_client_returns_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _base_client())
    assert get_client("acme", path).display_name == "Test Co"


def test_get_client_unknown_id_raises(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _base_client())
    with pytest.raises(ConfigError, match="unknown client_id"):
        get_client("nope", path)


# --- Single-client default (one client per repository) -----------------------


def test_get_client_defaults_to_the_only_client(tmp_path: Path) -> None:
    """One client per repo is the normal case, so naming it on every command is ceremony."""
    path = _write_config(tmp_path, _base_client())
    assert get_client(None, path).client_id == "acme"


def test_resolve_client_id_returns_the_only_client(tmp_path: Path) -> None:
    clients = load_clients(_write_config(tmp_path, _base_client()))
    assert resolve_client_id(None, clients) == "acme"


def test_resolve_client_id_passes_an_explicit_id_through(tmp_path: Path) -> None:
    """An explicit id is never second-guessed, even against a single-client config."""
    clients = load_clients(_write_config(tmp_path, _base_client()))
    assert resolve_client_id("whatever", clients) == "whatever"


def test_resolve_client_id_refuses_to_guess_between_several(tmp_path: Path) -> None:
    """Silently picking one of several would publish the wrong catalogue to the wrong site."""
    data = {"version": 1, "clients": {"acme": _base_client(), "beta": _base_client()}}
    path = tmp_path / "clients.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clients = load_clients(str(path))

    with pytest.raises(ConfigError, match="defines 2 clients"):
        resolve_client_id(None, clients)


def test_get_client_omitted_id_with_several_clients_raises(tmp_path: Path) -> None:
    data = {"version": 1, "clients": {"acme": _base_client(), "beta": _base_client()}}
    path = tmp_path / "clients.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="name the one to act on explicitly"):
        get_client(None, str(path))


def test_resolve_client_id_with_no_clients_raises() -> None:
    with pytest.raises(ConfigError, match="defines no clients"):
        resolve_client_id(None, {})


def test_schema_invalid_config_raises_config_error(tmp_path: Path) -> None:
    data = {"version": 2, "clients": {"acme": _base_client()}}  # version const is 1
    path = tmp_path / "clients.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid"):
        load_clients(path)


# --- Edge case E6 ------------------------------------------------------------


def test_e6_flat_invalid_column_target_raises(tmp_path: Path) -> None:
    client = _base_client()
    client["export"] = {"path": "x.xlsx", "column_map": {"Col": "not_a_field"}}
    path = _write_config(tmp_path, client)
    with pytest.raises(ExportParseError, match="not_a_field"):
        load_clients(path)


def test_e6_gdsn_invalid_field_raises(tmp_path: Path) -> None:
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {"bogus_field": {"sheet": "S", "attribute": "1"}},
    }
    path = _write_config(tmp_path, client)
    with pytest.raises(ExportParseError, match="bogus_field"):
        load_clients(path)


def test_e6_accepts_valid_targets(tmp_path: Path) -> None:
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {"product_name": {"sheet": "TradeItemDescription", "attribute": "3297"}},
    }
    path = _write_config(tmp_path, client)
    source = load_clients(path)["acme"].export.gdsn_map["product_name"]
    assert source.sheet == "TradeItemDescription"


def test_translate_is_opt_in_per_field(tmp_path: Path) -> None:
    """Filling a language gap costs producer tokens, so it is never on by accident."""
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {
            "product_name": {"sheet": "S", "attribute": "3301", "localised": True},
            "description_short": {
                "sheet": "S",
                "attribute": "1083",
                "localised": True,
                "translate": True,
            },
        },
    }
    path = _write_config(tmp_path, client)

    gdsn_map = load_clients(path)["acme"].export.gdsn_map
    assert gdsn_map["product_name"].translate is False
    assert gdsn_map["description_short"].translate is True


def test_the_example_config_opts_the_published_fields_into_translation(tmp_path: Path) -> None:
    """The example is the documentation of which fields are worth filling.

    `logistics_name` and `marketing_name` are deliberately out: both are `in_matrix: false`
    because nothing consumes them, so filling a gap in one is tokens spent on a value no page
    reads.

    `functional_name` is absent for a different reason: it is gone from the config entirely
    rather than merely un-translated. It was a second declaration of attr 3301, which
    `product_name` already carries — see the duplicate-attribute test below.
    """
    export = load_clients("clients.example.yml")["democlient"].export

    assert [n for n, s in export.gdsn_map.items() if s.translate] == [
        "product_name",
        "description_short",
        "description_long",
    ]
    assert [n for n, s in export.gdsn_extras.items() if s.translate] == [
        "product_variation",
        "material",
    ]


def test_the_example_config_reads_every_repeated_slot_of_a_multi_value_source() -> None:
    """`multivalue` is what stops a repeated `[n]` column being truncated to its first slot.

    Attr 1067 spreads USPs across `TradeItemFeatureBenefit[n]`; attr 4.012 spreads a product's
    materials across `Material[n]`. Both need the flag, and the flag is per-source opt-in, so the
    example is the only thing in git that records which sources actually repeat.
    """
    export = load_clients("clients.example.yml")["democlient"].export

    assert [n for n, s in export.gdsn_map.items() if s.multivalue] == ["description_long"]
    assert [n for n, s in export.gdsn_extras.items() if s.multivalue] == ["material"]


def test_two_sources_reading_one_attribute_are_refused_at_load(tmp_path: Path) -> None:
    """One attribute, one field — checked where the operator's own config is read.

    Attr 3301 was declared twice for months: mapped as `product_name` and again as
    `extras.functional_name`. The same value arrived under two names, so no reader could tell
    them apart, and the duplication surfaced as two §4 rows for one MyGS1 cell, two identical
    coverage-matrix columns, and a client template printing the product name under itself.

    Refused at load rather than pinned in a test over `clients.example.yml`, because the
    duplicate lived in the gitignored `clients.yml` that no test in git can see.
    """
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {"product_name": {"sheet": "TradeItemDescription", "attribute": "3301"}},
        "gdsn_extras": {"functional_name": {"sheet": "TradeItemDescription", "attribute": "3301"}},
    }
    path = _write_config(tmp_path, client)

    with pytest.raises(ExportParseError, match="product_name.*functional_name.*3301"):
        load_clients(path)


def test_all_sources_carries_both_maps_so_a_required_extra_is_never_dropped() -> None:
    """The single mapping E23 and the coverage matrix both read.

    Returning `gdsn_map` alone here is the bug this property was added to remove — `required` on
    a `gdsn_extras` entry would be silently ignored again, and by *both* consumers at once, so
    nothing downstream could notice the disagreement. A mutation doing exactly that survived the
    suite until this test existed.
    """
    export = load_clients("clients.example.yml")["democlient"].export
    sources = export.all_sources

    assert set(sources) == set(export.gdsn_map) | set(export.gdsn_extras)
    # Mapped fields first, so gap order stays stable between runs.
    assert list(sources)[: len(export.gdsn_map)] == list(export.gdsn_map)
    # The shipped example demonstrates the capability: dimensions are mandatory extras.
    assert [n for n, s in sources.items() if s.required and n in export.gdsn_extras] == [
        "dim_height",
        "dim_width",
        "dim_depth",
    ]


def test_one_field_name_declared_in_both_maps_is_refused_at_load(tmp_path: Path) -> None:
    """`all_sources` merges the two maps, so a shared name would silently shadow.

    Every consumer that asks "is this field mandatory?" now reads one merged mapping — E23 and
    the coverage matrix both. A name in `gdsn_map` and `gdsn_extras` would let the second
    quietly win, which is the failure the duplicate-attribute check above exists to stop, one
    level up: the value would arrive from the wrong sheet and nothing would say so.
    """
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {"net_content": {"sheet": "TradeItemMeasurements", "attribute": "3510"}},
        "gdsn_extras": {"net_content": {"sheet": "MarketingInformation", "attribute": "9999"}},
    }
    path = _write_config(tmp_path, client)

    with pytest.raises(ExportParseError, match="net_content"):
        load_clients(path)


def test_one_attribute_number_on_two_sheets_is_not_a_duplicate(tmp_path: Path) -> None:
    """GDSN attribute numbers are only unique within a sheet, so the sheet is half the identity.

    Comparing numbers alone would refuse a legitimate config and push a client back towards the
    duplicate declaration this check exists to stop.
    """
    client = _base_client()
    client["export"] = {
        "format": "gdsn",
        "path": "x.xlsx",
        "gdsn_map": {"product_name": {"sheet": "TradeItemDescription", "attribute": "3301"}},
        "gdsn_extras": {"other": {"sheet": "MarketingInformation", "attribute": "3301"}},
    }
    path = _write_config(tmp_path, client)

    assert load_clients(path)["acme"].export.gdsn_extras["other"].sheet == "MarketingInformation"


def test_the_example_config_reads_each_attribute_once(tmp_path: Path) -> None:
    """The shipped example has to demonstrate the rule it is validated against."""
    export = load_clients("clients.example.yml")["democlient"].export

    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []
    for name, src in [*export.gdsn_map.items(), *export.gdsn_extras.items()]:
        key = (src.sheet, src.attribute)
        if key in seen:
            duplicates.append(f"{seen[key]} and {name} both read {src.sheet} attr {src.attribute}")
        seen[key] = name

    assert not duplicates, "; ".join(duplicates)


# --- GS1Config.resolve bridge ------------------------------------------------


def test_resolve_returns_phase2_shape() -> None:
    cfg = GS1Config(
        account_number_test="8712345000010",
        client_id_env_test="ID_ENV",
        client_secret_env_test="SECRET_ENV",
    )
    resolved = cfg.resolve("test")

    assert isinstance(resolved, ResolvedGS1Config)
    assert resolved.account_number == "8712345000010"
    assert resolved.environment == "test"
    assert resolved.client_id_env == "ID_ENV"


def test_resolve_production_without_credentials_raises() -> None:
    cfg = GS1Config(
        account_number_test="8712345000010",
        client_id_env_test="ID_ENV",
        client_secret_env_test="SECRET_ENV",
    )
    with pytest.raises(ConfigError, match="production"):
        cfg.resolve("production")


# --- Categories block (Phase 7.5) --------------------------------------------


def _client_with_categories(categories: dict[str, Any]) -> dict[str, Any]:
    client = _base_client()
    client["categories"] = categories
    return client


def test_categories_absent_is_none(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _base_client())
    assert get_client("acme", path).categories is None


def test_categories_loads(tmp_path: Path) -> None:
    client = _client_with_categories(
        {
            "terms": ["tuin", "keuken"],
            "brick_category_map": {"10003865": "tuin"},
            "overrides": {"02987653000123": "keuken"},
        }
    )
    cfg = get_client("acme", _write_config(tmp_path, client))
    assert cfg.categories is not None
    assert cfg.categories.terms == ["tuin", "keuken"]
    assert cfg.categories.brick_category_map == {"10003865": "tuin"}
    assert cfg.categories.overrides == {"02987653000123": "keuken"}
    assert cfg.categories.on_unmapped == "warn"
    assert cfg.categories.require_terms_exist is True


def test_categories_brick_map_value_outside_terms_raises(tmp_path: Path) -> None:
    client = _client_with_categories(
        {"terms": ["tuin"], "brick_category_map": {"10003865": "keuken"}}
    )
    path = _write_config(tmp_path, client)
    with pytest.raises(ConfigError, match="keuken"):
        load_clients(path)


def test_categories_override_value_outside_terms_raises(tmp_path: Path) -> None:
    client = _client_with_categories({"terms": ["tuin"], "overrides": {"02987653000123": "keuken"}})
    path = _write_config(tmp_path, client)
    with pytest.raises(ConfigError, match="keuken"):
        load_clients(path)


def test_categories_empty_terms_raises(tmp_path: Path) -> None:
    # minItems in the schema rejects an empty terms list before the loader runs.
    path = _write_config(tmp_path, _client_with_categories({"terms": []}))
    with pytest.raises(ConfigError):
        load_clients(path)


def test_categories_duplicate_terms_raises(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _client_with_categories({"terms": ["tuin", "tuin"]}))
    with pytest.raises(ConfigError, match="unique"):
        load_clients(path)


def test_categories_unknown_key_rejected_by_schema(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _client_with_categories({"terms": ["tuin"], "bogus": 1}))
    with pytest.raises(ConfigError, match="invalid"):
        load_clients(path)


def test_example_config_categories_block_loads() -> None:
    cfg = load_clients("clients.example.yml")["democlient"]
    assert cfg.categories is not None
    # The site's own term set: it has no `tuin`, garden goes under outdoor_dier (2026-10-08).
    assert "outdoor_dier" in cfg.categories.terms
    assert "tuin" not in cfg.categories.terms
    assert cfg.categories.brick_category_map["10003865"] == "outdoor_dier"


# --- Media (Phase 9.5) -------------------------------------------------------


def _client_with_media(media: dict[str, Any]) -> dict[str, Any]:
    client = _base_client()
    client["media"] = media
    return client


def test_media_absent_is_none(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _base_client())
    assert get_client("acme", path).media is None


def test_media_defaults_applied(tmp_path: Path) -> None:
    cfg = get_client("acme", _write_config(tmp_path, _client_with_media({})))
    assert cfg.media is not None
    assert cfg.media.image_max_dim == 1600
    assert cfg.media.image_quality == 85
    assert cfg.media.header_image_field == "product_header_image"
    assert cfg.media.video_file_field == "product_header_video_file"
    assert cfg.media.image_write_shape == "id"
    assert cfg.media.video_transcode is False


def test_media_loads_full_block(tmp_path: Path) -> None:
    cfg = get_client(
        "acme",
        _write_config(
            tmp_path,
            _client_with_media(
                {
                    "image_max_dim": 1200,
                    "image_quality": 90,
                    "header_image_field": "hero",
                    "regular_image_field": "main",
                    "video_file_field": "vid",
                    "image_write_shape": "url",
                    "video_folders": {"nl": "in/nl", "fr": "in/fr"},
                    "video_map_path": "in/mapping.yml",
                    "video_transcode": True,
                    "ffmpeg_bin": "/opt/homebrew/bin/ffmpeg",
                }
            ),
        ),
    )
    assert cfg.media is not None
    assert cfg.media.image_max_dim == 1200
    assert cfg.media.image_write_shape == "url"
    assert cfg.media.video_folders == {"nl": "in/nl", "fr": "in/fr"}
    assert cfg.media.video_transcode is True


def test_media_restrict_flag_defaults_false_and_parses(tmp_path: Path) -> None:
    default = get_client("acme", _write_config(tmp_path, _client_with_media({})))
    assert default.media is not None
    assert default.media.restrict_to_mapped_gtins is False
    on = get_client(
        "acme", _write_config(tmp_path, _client_with_media({"restrict_to_mapped_gtins": True}))
    )
    assert on.media is not None
    assert on.media.restrict_to_mapped_gtins is True


def test_media_invalid_write_shape_rejected_by_schema(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _client_with_media({"image_write_shape": "bogus"}))
    with pytest.raises(ConfigError, match="invalid"):
        load_clients(path)


def test_media_quality_out_of_range_rejected_by_schema(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _client_with_media({"image_quality": 200}))
    with pytest.raises(ConfigError, match="invalid"):
        load_clients(path)


def test_media_unknown_key_rejected_by_schema(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _client_with_media({"bogus": 1}))
    with pytest.raises(ConfigError, match="invalid"):
        load_clients(path)


def test_example_config_media_block_loads() -> None:
    cfg = load_clients("clients.example.yml")["democlient"]
    assert cfg.media is not None
    assert cfg.media.video_transcode is True
    assert set(cfg.media.video_folders) == {"nl", "fr"}


# --- Lazy secrets ------------------------------------------------------------


def test_load_does_not_read_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear every candidate secret env var; loading must still succeed and only
    # carry the env-var *names*, never resolved values.
    for name in ("TEST_GS1_ID", "TEST_GS1_SECRET", "TEST_WP_PASS"):
        monkeypatch.delenv(name, raising=False)
    assert "TEST_GS1_ID" not in os.environ

    client = get_client("acme", _write_config(tmp_path, _base_client()))

    assert client.gs1.client_id_env_test == "TEST_GS1_ID"
    assert client.wordpress.app_password_env == "TEST_WP_PASS"
