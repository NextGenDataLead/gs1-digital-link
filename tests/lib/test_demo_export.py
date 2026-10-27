"""Tests for lib/demo_export.py — the synthetic export, checked against the config it serves.

``clients.example.yml``'s ``democlient`` block is read here rather than restated. The fixture and
that config are authored independently, so this file is what holds them together: a mapped
attribute the workbook stops carrying, or a language the config gains, fails here rather than in
somebody's rehearsal.

Nothing in this module writes outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lib import demo_export, mandatory
from lib.config import ClientConfig, ExportConfig, load_clients
from lib.gdsn import BuildResult, GdsnSheet, build_records, read_workbook
from lib.gdsn_layout import MODULES
from lib.records import ProductRecord
from scripts.make_demo_export import _write_workbook

#: The example config, by absolute path — these tests must not care what the cwd is.
_EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "clients.example.yml"

#: GS1 prefix 029 is restricted distribution: reserved for internal use, never issued to a
#: company. Every demo barcode must sit on it, or a fixture GTIN could name a real product.
_RESTRICTED_PREFIX = "0029"


@pytest.fixture(scope="module")
def client() -> ClientConfig:
    return load_clients(_EXAMPLE_CONFIG)["democlient"]


@pytest.fixture(scope="module")
def workbook(tmp_path_factory: pytest.TempPathFactory) -> dict[str, GdsnSheet]:
    """The synthetic export, written to disk and read back by the real reader."""
    path = tmp_path_factory.mktemp("demo") / "products.xlsx"
    _write_workbook(path, demo_export.sheet_grids())
    return read_workbook(str(path))


@pytest.fixture(scope="module")
def parsed(workbook: dict[str, GdsnSheet], client: ClientConfig) -> BuildResult:
    export = client.export
    return build_records(
        workbook,
        export.gdsn_map,
        export.market_priority,
        list(client.wordpress.languages),
        client.wordpress.default_language,
        export.gdsn_extras,
    )


def _by_gtin(parsed: BuildResult) -> dict[str, ProductRecord]:
    return {record.gtin: record for record in parsed.records}


def _value(record: ProductRecord, field: str, export: ExportConfig) -> Any:
    """The value a declared source resolved to, wherever that source's field lives."""
    if field in export.gdsn_map:
        return getattr(record, field, None)
    return record.extras.get(field) or record.extras_localised.get(field)


# --- The shape ----------------------------------------------------------------


def test_every_module_sheet_is_emitted_and_read_back(workbook: dict[str, GdsnSheet]) -> None:
    """An export is mostly sheets the config ignores; the reader has to cope with all of them."""
    assert set(workbook) == set(MODULES)


def test_every_declared_source_is_actually_carried(
    workbook: dict[str, GdsnSheet], client: ClientConfig
) -> None:
    """Both maps, not just gdsn_map — ``required`` on an extra was ignored for a whole release."""
    missing = [
        f"{field}: {source.sheet}/{source.attribute}"
        for field, source in client.export.all_sources.items()
        if source.attribute and not workbook[source.sheet].has_attribute(source.attribute)
    ]
    assert missing == []


def test_regenerating_produces_the_same_rows() -> None:
    assert demo_export.sheet_grids() == demo_export.sheet_grids()


# --- The catalogue ------------------------------------------------------------


def test_the_catalogue_languages_are_the_ones_the_config_resolves(client: ClientConfig) -> None:
    assert tuple(client.wordpress.languages) == demo_export.LANGUAGES
    assert client.wordpress.default_language in demo_export.LANGUAGES


@pytest.mark.parametrize(
    "gtin",
    [
        *(product.gtin for product in demo_export.CATALOGUE),
        demo_export.CASE_GTIN,
        demo_export.UNKNOWN_BARCODE,
    ],
)
def test_every_demo_barcode_is_valid_and_unallocatable(gtin: str) -> None:
    assert len(gtin) == 14
    assert gtin.isdigit()
    assert gtin.startswith(_RESTRICTED_PREFIX)
    assert demo_export.check_digit(gtin[:-1]) == gtin[-1]


def test_demo_barcodes_are_distinct() -> None:
    barcodes = [product.gtin for product in demo_export.CATALOGUE]
    barcodes += [demo_export.CASE_GTIN, demo_export.UNKNOWN_BARCODE]
    assert len(set(barcodes)) == len(barcodes)


# --- What the reader makes of it ----------------------------------------------


def test_it_parses_cleanly_into_one_record_per_product(parsed: BuildResult) -> None:
    assert parsed.errors == []
    assert len(parsed.records) == len(demo_export.CATALOGUE)


def test_the_only_warnings_are_the_source_findings_it_is_built_to_show(
    parsed: BuildResult,
) -> None:
    """Any structural complaint — a source not found, a sheet absent — would land here too."""
    assert len(parsed.warnings) == len(parsed.issues)


def test_the_case_level_row_never_becomes_a_product(parsed: BuildResult) -> None:
    """Were the unit filter to stop working, this GTIN would surface as a nameless product."""
    assert demo_export.CASE_GTIN not in _by_gtin(parsed)


def test_every_declared_source_resolves_for_at_least_one_product(
    parsed: BuildResult, client: ClientConfig
) -> None:
    """A field nothing carries is a field the fixture silently stopped exercising."""
    unresolved = [
        field
        for field in client.export.all_sources
        if not any(_value(record, field, client.export) for record in parsed.records)
    ]
    assert unresolved == []


# --- The edges it exists to exercise ------------------------------------------


def test_a_language_missing_from_one_market_is_taken_from_the_next(
    parsed: BuildResult, client: ClientConfig
) -> None:
    """Neither market carries both languages; the record is whole only because ranking works."""
    record = _by_gtin(parsed)[demo_export.CATALOGUE[4].gtin]
    assert record.product_name.values == {"nl": "Plantenspuit", "fr": "Pulvérisateur"}


def test_markets_that_disagree_resolve_to_the_highest_ranked(parsed: BuildResult) -> None:
    record = _by_gtin(parsed)[demo_export.CATALOGUE[2].gtin]
    assert record.product_name.values["fr"] == "Support dorsal"
    conflict = next(
        issue
        for issue in parsed.issues
        if issue.gtin == record.gtin and issue.issue == "value_inconsistent_across_markets"
    )
    assert [market for market, _value in conflict.market_values] == ["528", "056"]


def test_a_repeated_localised_group_is_read_whole_and_newline_joined(
    parsed: BuildResult,
) -> None:
    """The generator splits 1067 back into ranked USPs on exactly this character."""
    record = _by_gtin(parsed)[demo_export.CATALOGUE[11].gtin]
    assert record.description_long is not None
    assert record.description_long.values["nl"].split("\n") == [
        "LED met 200 lumen",
        "Aluminium behuizing",
        "Spatwaterdicht",
    ]


def test_a_repeated_scalar_group_joins_every_slot_with_a_comma(parsed: BuildResult) -> None:
    """Not a newline: this renders verbatim in Technische details, where one would collapse."""
    record = _by_gtin(parsed)[demo_export.CATALOGUE[8].gtin]
    assert record.extras["material"] == "latex, zzzanders, katoen"


def test_the_either_or_marketing_group_is_satisfied_by_one_member(parsed: BuildResult) -> None:
    record = _by_gtin(parsed)[demo_export.CATALOGUE[11].gtin]
    assert record.description_short is None
    assert record.description_long is not None


def test_the_hero_falls_back_to_the_product_image_when_none_is_flagged_primary(
    parsed: BuildResult,
) -> None:
    record = _by_gtin(parsed)[demo_export.CATALOGUE[6].gtin]
    assert record.image_url is not None
    assert record.gtin in record.image_url


def test_the_flagged_primary_wins_over_the_other_files(parsed: BuildResult) -> None:
    record = _by_gtin(parsed)[demo_export.CATALOGUE[0].gtin]
    assert record.image_url is not None
    assert "1200x1200" in record.image_url


# --- What a run would hold ----------------------------------------------------


def test_one_product_is_held_by_each_mandatory_rule(
    parsed: BuildResult, client: ClientConfig
) -> None:
    """Nine publishable and three held. A demo in which nothing is ever held shows a screen the
    operator will not recognise the first time a real export disappoints them — and the three
    holds are deliberately one per rule, so each is exercised rather than just the loudest."""
    languages = list(client.wordpress.languages)
    held = {
        record.gtin: [
            gap.label
            for gap in mandatory.missing_mandatory(record, client.export.all_sources, languages)
        ]
        for record in parsed.records
        if mandatory.missing_mandatory(record, client.export.all_sources, languages)
    }

    assert len(parsed.records) - len(held) == 9
    assert list(held) == [demo_export.CATALOGUE[index].gtin for index in (7, 9, 10)]
    # A localised required field, a required pass-through extra, and a required scalar.
    assert any("product_name.fr" in label for label in held[demo_export.CATALOGUE[7].gtin])
    assert any("dim_height" in label for label in held[demo_export.CATALOGUE[9].gtin])
    assert any("image_url" in label for label in held[demo_export.CATALOGUE[10].gtin])


# --- The scope list -----------------------------------------------------------


def test_the_scope_list_carries_the_catalogue_and_one_barcode_the_export_lacks(
    parsed: BuildResult,
) -> None:
    """The Data screen has a table whose only job is to show barcodes that join to nothing."""
    header, *rows = demo_export.process_list_rows()
    listed = [row[0] for row in rows]

    assert header[0] == "Barcode"
    assert set(listed) == {product.gtin for product in demo_export.CATALOGUE} | {
        demo_export.UNKNOWN_BARCODE
    }
    assert demo_export.UNKNOWN_BARCODE not in _by_gtin(parsed)
