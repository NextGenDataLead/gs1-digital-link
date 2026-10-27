"""Unit tests for GPC brick → category resolution (Phase 7.5)."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.categories import (
    assign_categories,
    coverage_report,
    distinct_bricks,
    draft_brick_map,
    load_diy_datamodel,
    resolve_category,
)
from lib.config import CategoryConfig
from lib.errors import ExportParseError
from lib.records import LocalisedText, ProductRecord

_TERMS = frozenset({"tuin", "keuken", "specials"})


def _product(gtin: str, brick: str | None) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values={"nl": "test"}),
        gpc_brick_code=brick,
    )


# --- resolve_category --------------------------------------------------------


def test_brick_map_resolves_when_no_override() -> None:
    res = resolve_category(
        _product("02987653000123", "10003865"),
        brick_category_map={"10003865": "tuin"},
        overrides={},
        allowed_terms=_TERMS,
    )
    assert res.term == "tuin"
    assert res.issue is None


def test_override_wins_over_brick_map() -> None:
    # Same brick maps to tuin, but this GTIN is the nutcracker → keuken.
    res = resolve_category(
        _product("02987653000123", "10003865"),
        brick_category_map={"10003865": "tuin"},
        overrides={"02987653000123": "keuken"},
        allowed_terms=_TERMS,
    )
    assert res.term == "keuken"
    assert res.issue is None


def test_unmapped_brick_warns_and_does_not_guess() -> None:
    res = resolve_category(
        _product("02987653000123", "99999999"),
        brick_category_map={"10003865": "tuin"},
        overrides={},
        allowed_terms=_TERMS,
    )
    assert res.term is None
    assert res.issue is not None
    assert res.issue.issue == "category_unmapped"
    assert res.issue.field == "category"
    assert res.issue.value == "99999999"  # the brick, verbatim — not a guessed term


def test_missing_brick_reports_brick_missing() -> None:
    res = resolve_category(
        _product("02987653000123", None),
        brick_category_map={"10003865": "tuin"},
        overrides={},
        allowed_terms=_TERMS,
    )
    assert res.term is None
    assert res.issue is not None
    assert res.issue.issue == "category_brick_missing"
    assert res.issue.value == ""


def test_override_matches_on_gtin14_normalisation() -> None:
    # A 13-digit override key must resolve a product whose GTIN-14 is the zero-padded form.
    res = resolve_category(
        _product("2987653000123", "10003865"),  # 13 digits → gtin14 = 02987653000123
        brick_category_map={},
        overrides={"02987653000123": "keuken"},
        allowed_terms=_TERMS,
    )
    assert res.term == "keuken"


def test_term_outside_allowed_treated_as_unresolved() -> None:
    # Defensive: a brick_category_map value not in allowed_terms does not resolve.
    res = resolve_category(
        _product("02987653000123", "10003865"),
        brick_category_map={"10003865": "not_a_term"},
        overrides={},
        allowed_terms=_TERMS,
    )
    assert res.term is None
    assert res.issue is not None
    assert res.issue.issue == "category_unmapped"


def test_override_outside_allowed_treated_as_unresolved() -> None:
    res = resolve_category(
        _product("02987653000123", "10003865"),
        brick_category_map={"10003865": "tuin"},
        overrides={"02987653000123": "not_a_term"},
        allowed_terms=_TERMS,
    )
    assert res.term is None
    assert res.issue is not None
    assert res.issue.issue == "category_unmapped"


# --- distinct_bricks ---------------------------------------------------------


def test_distinct_bricks_groups_gtins_and_skips_missing() -> None:
    products = [
        _product("02987653000001", "10003865"),
        _product("02987653000002", "10003865"),
        _product("02987653000003", "10006459"),
        _product("02987653000004", None),
    ]
    bricks = distinct_bricks(products)
    assert bricks == {
        "10003865": ["02987653000001", "02987653000002"],
        "10006459": ["02987653000003"],
    }


# --- coverage_report ---------------------------------------------------------


def _categories(brick_map: dict[str, str], overrides: dict[str, str]) -> CategoryConfig:
    return CategoryConfig(
        terms=["tuin", "keuken", "specials"],
        brick_category_map=brick_map,
        overrides=overrides,
    )


def test_coverage_report_buckets_mapped_override_only_and_unmapped() -> None:
    products = [
        _product("02987653000001", "10003865"),  # mapped
        _product("02987653000002", "10006459"),  # override-only (whole brick overridden)
        _product("02987653000003", "77777777"),  # unmapped
    ]
    categories = _categories(
        brick_map={"10003865": "tuin"},
        overrides={"02987653000002": "specials"},
    )
    report = coverage_report(products, categories)
    assert report.total_bricks == 3
    assert report.mapped == ["10003865"]
    assert report.override_only == ["10006459"]
    assert report.unmapped == {"77777777": ["02987653000003"]}
    assert report.is_complete is False


def test_coverage_report_complete_when_every_brick_resolves() -> None:
    products = [
        _product("02987653000001", "10003865"),
        _product("02987653000002", "10006459"),
    ]
    categories = _categories(
        brick_map={"10003865": "tuin", "10006459": "specials"},
        overrides={},
    )
    report = coverage_report(products, categories)
    assert report.unmapped == {}
    assert report.is_complete is True


def test_coverage_partial_override_leaves_brick_unmapped() -> None:
    # A brick where only *some* products have an override is still unmapped — for the rest.
    products = [
        _product("02987653000001", "10003865"),
        _product("02987653000002", "10003865"),
    ]
    categories = _categories(brick_map={}, overrides={"02987653000001": "tuin"})
    report = coverage_report(products, categories)
    assert report.unmapped == {"10003865": ["02987653000002"]}
    assert report.is_complete is False


# --- load_diy_datamodel ------------------------------------------------------


def _write_datamodel(path: Path, header: list[str], rows: list[list[str]]) -> str:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return str(path)


def test_load_diy_datamodel_reads_parameterized_columns(tmp_path: Path) -> None:
    path = _write_datamodel(
        tmp_path / "diy.xlsx",
        header=["Brick", "Sector", "Ignored"],
        rows=[["10003865", "Garden", "x"], ["10006459", "Lighting", "y"]],
    )
    mapping = load_diy_datamodel(path, code_column="Brick", category_column="Sector")
    assert mapping == {"10003865": "Garden", "10006459": "Lighting"}


def test_load_diy_datamodel_blank_label_kept_as_empty(tmp_path: Path) -> None:
    path = _write_datamodel(
        tmp_path / "diy.xlsx", header=["Brick", "Sector"], rows=[["10003865", None]]
    )
    assert load_diy_datamodel(path, code_column="Brick", category_column="Sector") == {
        "10003865": ""
    }


def test_load_diy_datamodel_missing_column_raises(tmp_path: Path) -> None:
    path = _write_datamodel(tmp_path / "diy.xlsx", header=["Brick"], rows=[["10003865"]])
    with pytest.raises(ExportParseError, match="no worksheet has a header row"):
        load_diy_datamodel(path, code_column="Brick", category_column="Sector")


def test_load_diy_datamodel_finds_header_below_preamble(tmp_path: Path) -> None:
    # Real datamodels carry a banner/numbering preamble above the header row.
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["GS1 data pool - banner"])
    sheet.append([None, None])
    sheet.append(["1", "3"])  # a numbering row
    sheet.append(["Brick Code", "NL Brick Title"])
    sheet.append(["10003865", "Tuingereedschap"])
    path = tmp_path / "diy.xlsx"
    workbook.save(path)

    mapping = load_diy_datamodel(
        str(path), code_column="Brick Code", category_column="NL Brick Title"
    )
    assert mapping == {"10003865": "Tuingereedschap"}


def test_load_diy_datamodel_scans_to_the_matching_sheet(tmp_path: Path) -> None:
    workbook = openpyxl.Workbook()
    workbook.active.title = "Explanation"
    workbook.active.append(["irrelevant"])
    bricks = workbook.create_sheet("Bricks")
    bricks.append(["Brick Code", "NL Brick Title"])
    bricks.append(["10006459", "Verlichting"])
    path = tmp_path / "diy.xlsx"
    workbook.save(path)

    mapping = load_diy_datamodel(
        str(path), code_column="Brick Code", category_column="NL Brick Title"
    )
    assert mapping == {"10006459": "Verlichting"}


def test_load_diy_datamodel_unknown_sheet_raises(tmp_path: Path) -> None:
    path = _write_datamodel(
        tmp_path / "diy.xlsx", header=["Brick", "Sector"], rows=[["10003865", "Garden"]]
    )
    with pytest.raises(ExportParseError, match="no worksheet named"):
        load_diy_datamodel(path, code_column="Brick", category_column="Sector", sheet="Nope")


# --- draft_brick_map ---------------------------------------------------------


def test_draft_lists_every_brick_unset_and_annotates() -> None:
    products = [
        ProductRecord(
            gtin="02987653000001",
            brand="Democlient",
            product_name=LocalisedText(values={"nl": "Snoeischaar"}),
            gpc_brick_code="10003865",
        ),
        ProductRecord(
            gtin="02987653000002",
            brand="Democlient",
            product_name=LocalisedText(values={"nl": "Lamp"}),
            gpc_brick_code="10006459",
        ),
    ]
    bricks = distinct_bricks(products)
    draft = draft_brick_map(bricks, products, datamodel={"10003865": "Garden"})

    assert draft.entries == {"10003865": "", "10006459": ""}  # every brick UNSET
    assert "Garden" in draft.annotations["10003865"]
    assert "Snoeischaar" in draft.annotations["10003865"]
    assert draft.unannotated == ["10006459"]  # not covered by the datamodel


def test_draft_without_datamodel_marks_all_unannotated() -> None:
    products = [_product("02987653000001", "10003865")]
    draft = draft_brick_map(distinct_bricks(products), products, datamodel=None)
    assert draft.unannotated == ["10003865"]
    assert "1 product(s)" in draft.annotations["10003865"]


# --- assign_categories --------------------------------------------------------
#
# Lifted out of ``run_plan`` when ``run_generate`` had to reproduce the same records: the content
# hash covers ``category``, so a caller that classifies without assigning categories first gets
# different hashes from the plan's and generates copy for the wrong units.


def test_assign_categories_sets_the_term_and_reports_nothing() -> None:
    product = _product("02987653000123", "10003865")

    assigned, issues = assign_categories(
        CategoryConfig(terms=["tuin"], brick_category_map={"10003865": "tuin"}, overrides={}),
        [product],
    )

    assert [p.category for p in assigned] == ["tuin"]
    assert issues == []
    assert product.category is None  # the input is not mutated


def test_assign_categories_leaves_an_unmapped_brick_unset_and_reports_it() -> None:
    assigned, issues = assign_categories(
        CategoryConfig(terms=["tuin"], brick_category_map={"99999999": "tuin"}, overrides={}),
        [_product("02987653000123", "10003865")],
    )

    assert [p.category for p in assigned] == [None]  # never guessed
    assert [i.issue for i in issues] == ["category_unmapped"]


def test_assign_categories_without_a_config_is_a_no_op() -> None:
    products = [_product("02987653000123", "10003865")]

    assigned, issues = assign_categories(None, products)

    assert assigned == products
    assert issues == []
