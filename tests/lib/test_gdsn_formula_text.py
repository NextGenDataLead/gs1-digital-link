"""A value that is spreadsheet formula text stops the parse instead of reaching a page.

Noviplast, 2026-11-04: the 3309 upload into MyGS1 was built with an INDEX/MATCH formula, and the
import stored the formula's *text* in front of each name — every one of 262 cells read
``INDEX([1]TradeItemDescription!$F:$F, MATCH(1, …, 0))Fun Grill``. The export carried it as plain
text, it fit every length limit, and with 3309 as the title it would have been the headline of
49 live pages. Nothing in the parse noticed; a person reading the export did.

It is an error, not a warning, because it never means the client chose that value: it means an
import went wrong, and the fix is a re-upload in MyGS1, not anything downstream.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.gdsn import BuildResult, GdsnSource, build_records, looks_like_formula_text, read_workbook

_HEADER = [
    [
        "Gtin",
        "TargetMarketCountryCode",
        "InformationProviderOfTradeItem",
        "TradeItemUnitDescriptorCode",
        *["TradeItemDescriptionInformation"] * 6,
    ],
    [None] * 4
    + ["DescriptionShort[0]"] * 2
    + ["LabelDescription[0]"] * 2
    + ["BrandNameInformation"] * 2,
    [None] * 4 + ["LanguageCode", "Value", "LanguageCode", "Value", "BrandName", "SubBrand"],
    [None] * 10,
    [None] * 10,
    [None] * 10,
    [
        "GTIN (3059)",
        "Country (3179)",
        "Provider (3088)",
        "Unit (3074)",
        *["Short product name (3297)"] * 2,
        *["Label Description (3309)"] * 2,
        "Brand Name (3336)",
        "Sub brand (3340)",
    ],
]
_GTIN = "02987653000001"
_IMPORTED = (
    "INDEX([1]TradeItemDescription!$F:$F, MATCH(1, ([1]TradeItemDescription!$E:$E=F7)"
    "*([1]TradeItemDescription!$A:$A=A7), 0))Fun Grill"
)


def _build(
    tmp_path: Path, label: str, sub: str | None = None, *, extra: bool = False
) -> BuildResult:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheet = wb.create_sheet("TradeItemDescription")
    for row in [
        *_HEADER,
        [
            _GTIN,
            "528",
            "GLN",
            "BASE_UNIT_OR_EACH",
            "nl",
            "Fun Grill",
            "nl",
            label,
            "Noviplast",
            sub,
        ],
    ]:
        sheet.append(row)
    path = tmp_path / "formula.xlsx"
    wb.save(path)
    title = GdsnSource(
        sheet="TradeItemDescription", attribute="3309", fallback_attribute="3297", localised=True
    )
    gdsn_map = {
        "brand": GdsnSource(sheet="TradeItemDescription", attribute="3336"),
        "product_name": GdsnSource(sheet="TradeItemDescription", attribute="3297", localised=True)
        if extra
        else title,
    }
    extras = {"sub_brand": GdsnSource(sheet="TradeItemDescription", attribute="3340")}
    if extra:
        extras["web_name"] = title
    return build_records(read_workbook(str(path)), gdsn_map, ["528"], ["nl"], "nl", extras)


def test_formula_text_in_a_mapped_field_stops_the_parse_and_names_where(tmp_path: Path) -> None:
    result = _build(tmp_path, _IMPORTED)

    assert result.records == []
    [error] = result.errors
    assert _GTIN in error
    assert "product_name.nl" in error
    assert "attr 3309" in error
    assert "Paste Special" in error  # the remedy, in the words of the tool the client used


def test_formula_text_in_a_pass_through_extra_stops_the_parse_too(tmp_path: Path) -> None:
    # The same check on every path: an extra is printed on the page as surely as the title.
    result = _build(tmp_path, _IMPORTED, extra=True)

    assert result.records == []
    assert any("web_name.nl" in e for e in result.errors)


def test_formula_text_in_a_language_agnostic_extra_stops_the_parse(tmp_path: Path) -> None:
    result = _build(tmp_path, "Fun Grill", sub="VLOOKUP(A2,Sheet2!$A:$B,2,FALSE)")

    assert result.records == []
    assert any("sub_brand" in e for e in result.errors)


def test_a_clean_export_is_untouched(tmp_path: Path) -> None:
    result = _build(tmp_path, "Fun Grill", sub="Garden & Co!")

    assert result.errors == []
    assert result.records[0].product_name.values == {"nl": "Fun Grill"}


@pytest.mark.parametrize(
    "value",
    [
        _IMPORTED,
        "=INDEX(B:B, 3)",
        "=A1&B1",
        "Sheet1!$F:$F",
        "[1]TradeItemDescription!A7",
        "#REF!",
        "#N/A",
        "#VALUE!",
        "#NAME?",
        "VLOOKUP(A2, B:C, 2, FALSE)",
    ],
)
def test_spreadsheet_formula_text_is_recognised(value: str) -> None:
    assert looks_like_formula_text(value)


@pytest.mark.parametrize(
    "value",
    [
        "Fun Grill",
        "Push & fix",
        "Super 5",
        "Wow! Een handige tuinslang",
        "Set van 3 krijtstiften (20 cm)",
        "Lampe = lumière",
        "Index card holder",
        "Match the colour: 3 varianten",
        "#1 in tuinonderhoud",
        "Leuke kerstverlichting - 3 varianten",
        "50 x 30 cm",
    ],
)
def test_ordinary_product_text_is_not_mistaken_for_a_formula(value: str) -> None:
    assert not looks_like_formula_text(value)
