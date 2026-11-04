"""A source's ``fallback_attribute``: read a second attribute where the first is empty everywhere.

Noviplast, 2026-11-04: the page title moves from 3297 (*Korte productnaam*, the till name) to 3309
(*Commerciële benaming*, which the GS1 datamodel reserves for web shops). The client filled 3309
for the 49 published products only, and the export carries the whole catalogue — so a title read
from 3309 alone would leave every other product without one, and a blank ``product_name`` fails
the whole parse. ``fallback_attribute: "3297"`` keeps those on the name they had.

The fallback is decided **per language, after every market**: a 3309 value in the lowest-ranked
market still beats a 3297 value in the highest, because the question is which attribute the client
meant, not which market answered first. Each value taken from the fallback is reported, so the
data-quality report can say which products still need 3309 filled in MyGS1.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from pydantic import ValidationError

from lib.gdsn import BuildResult, GdsnSource, build_records, read_workbook, source_label

_KEY = [
    "Gtin",
    "TargetMarketCountryCode",
    "InformationProviderOfTradeItem",
    "TradeItemUnitDescriptorCode",
]
_HEADER = [
    [*_KEY, *["TradeItemDescriptionInformation"] * 10],
    [
        None,
        None,
        None,
        None,
        "DescriptionShort[0]",
        "DescriptionShort[0]",
        "DescriptionShort[1]",
        "DescriptionShort[1]",
        "LabelDescription[0]",
        "LabelDescription[0]",
        "LabelDescription[1]",
        "LabelDescription[1]",
        "BrandNameInformation",
        "BrandNameInformation",
    ],
    [None, None, None, None, *["LanguageCode", "Value"] * 4, "BrandName", "SubBrand"],
    [None] * 14,
    [None] * 14,
    [None] * 14,
    [
        "GTIN (3059)",
        "Country (3179)",
        "Provider (3088)",
        "Unit (3074)",
        *["Short product name (3297)"] * 4,
        *["Label Description (3309)"] * 4,
        "Brand Name (3336)",
        "Sub brand (3340)",
    ],
]

#: Each product's 3297 and 3309 values, by market and then language. ``None`` is a blank slot.
_PREFERRED = "02987653000001"  # 3309 filled in both languages — 3297 must not be read
_FALLBACK = "02987653000002"  # 3309 empty everywhere — 3297 in both languages
_LOWER_MARKET = "02987653000003"  # 3309 nl only, and only in 056; 3297 nl+fr in 528
_BOTH_BLANK = "02987653000004"  # fr empty in 3297 and 3309 — a blank, not a fallback


def _row(
    gtin: str,
    market: str,
    short: tuple[str | None, str | None],
    label: tuple[str | None, str | None],
    sub: str | None = None,
) -> list[object]:
    def pair(lang: str, value: str | None) -> list[object]:
        return [lang, value] if value is not None else [None, None]

    return [
        gtin,
        market,
        "GLN",
        "BASE_UNIT_OR_EACH",
        *pair("nl", short[0]),
        *pair("fr", short[1]),
        *pair("nl", label[0]),
        *pair("fr", label[1]),
        "Noviplast",
        sub,
    ]


_DATA = [
    _row(
        _PREFERRED,
        "528",
        ("Raamwisser kunststof grn", "Essuie-glace"),
        ("Eazy raamwisser", "Eazy raclette"),
    ),
    _row(
        _PREFERRED,
        "056",
        ("Raamwisser kunststof grn", "Essuie-glace"),
        ("Eazy raamwisser", "Eazy raclette"),
    ),
    _row(_FALLBACK, "528", ("Mini Juicer", "Mini Juicer FR"), (None, None)),
    _row(_FALLBACK, "056", ("Mini Juicer", "Mini Juicer FR"), (None, None)),
    _row(_LOWER_MARKET, "528", ("Vliegenverjager kunststof zw", "Chasse mouches"), (None, None)),
    _row(_LOWER_MARKET, "056", (None, None), ("Fly away", None)),
    _row(_BOTH_BLANK, "528", ("Dizzy Dog", None), (None, None)),
]

_MARKETS = ["528", "056"]
_LANGUAGES = ["nl", "fr"]


def _title(**overrides: object) -> GdsnSource:
    fields: dict[str, object] = {
        "sheet": "TradeItemDescription",
        "attribute": "3309",
        "fallback_attribute": "3297",
        "localised": True,
    }
    return GdsnSource.model_validate({**fields, **overrides})


#: ``brand`` is required on every record, so every map here carries it.
_BRAND = {"brand": GdsnSource(sheet="TradeItemDescription", attribute="3336")}


def _build(
    tmp_path: Path,
    gdsn_map: dict[str, GdsnSource],
    gdsn_extras: dict[str, GdsnSource] | None = None,
) -> BuildResult:
    gdsn_map = {**_BRAND, **gdsn_map}
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheet = wb.create_sheet("TradeItemDescription")
    for row in [*_HEADER, *_DATA]:
        sheet.append(row)
    path = tmp_path / "fallback.xlsx"
    wb.save(path)
    return build_records(
        read_workbook(str(path)), gdsn_map, _MARKETS, _LANGUAGES, "nl", gdsn_extras or {}
    )


def _record_names(result: BuildResult, gtin: str) -> dict[str, str]:
    record = next(r for r in result.records if r.gtin == gtin)
    return dict(record.product_name.values)


def _issues(result: BuildResult, gtin: str, kind: str) -> list[str]:
    return sorted(i.field for i in result.issues if i.gtin == gtin and i.issue == kind)


def test_preferred_attribute_wins_when_it_has_a_value(tmp_path: Path) -> None:
    result = _build(tmp_path, {"product_name": _title()})

    assert _record_names(result, _PREFERRED) == {"nl": "Eazy raamwisser", "fr": "Eazy raclette"}
    assert _issues(result, _PREFERRED, "value_from_fallback") == []


def test_fallback_is_used_per_language_and_reported(tmp_path: Path) -> None:
    result = _build(tmp_path, {"product_name": _title()})

    assert _record_names(result, _FALLBACK) == {"nl": "Mini Juicer", "fr": "Mini Juicer FR"}
    found = [i for i in result.issues if i.gtin == _FALLBACK and i.issue == "value_from_fallback"]
    assert sorted(i.field for i in found) == ["product_name.fr", "product_name.nl"]
    # The value reported is the one the page will show, and the source names both attributes —
    # the operator fills the first one in MyGS1, and needs to know the second is what is used now.
    assert {i.value for i in found} == {"Mini Juicer", "Mini Juicer FR"}
    assert all(i.source == "TradeItemDescription attr 3309, else 3297" for i in found)
    assert all("3309" in i.detail and "3297" in i.detail for i in found)


def test_preferred_value_in_a_lower_market_beats_the_fallback_in_a_higher_one(
    tmp_path: Path,
) -> None:
    # 3309 nl exists only in 056; 3297 nl exists in 528, the higher-ranked market. The client's
    # chosen attribute decides, not the market order — so nl is "Fly away". fr has no 3309 in any
    # market, so it falls back to 3297.
    result = _build(tmp_path, {"product_name": _title()})

    assert _record_names(result, _LOWER_MARKET) == {"nl": "Fly away", "fr": "Chasse mouches"}
    assert _issues(result, _LOWER_MARKET, "value_from_fallback") == ["product_name.fr"]


def test_a_language_blank_in_both_attributes_is_a_blank_naming_both(tmp_path: Path) -> None:
    result = _build(tmp_path, {"product_name": _title()})

    assert _record_names(result, _BOTH_BLANK) == {"nl": "Dizzy Dog"}
    assert _issues(result, _BOTH_BLANK, "value_from_fallback") == ["product_name.nl"]
    blanks = [i for i in result.issues if i.gtin == _BOTH_BLANK and i.issue == "value_blank"]
    assert [i.field for i in blanks] == ["product_name.fr"]
    assert blanks[0].source == "TradeItemDescription attr 3309, else 3297"


def test_report_issues_false_keeps_the_value_and_reports_nothing(tmp_path: Path) -> None:
    result = _build(tmp_path, {"product_name": _title(report_issues=False)})

    assert _record_names(result, _FALLBACK) == {"nl": "Mini Juicer", "fr": "Mini Juicer FR"}
    assert [i for i in result.issues if i.field.startswith("product_name")] == []


def test_a_language_agnostic_field_falls_back_too(tmp_path: Path) -> None:
    brand = GdsnSource(sheet="TradeItemDescription", attribute="3340", fallback_attribute="3336")
    result = _build(tmp_path, {"product_name": _title(), "brand": brand})

    record = next(r for r in result.records if r.gtin == _PREFERRED)
    assert record.brand == "Noviplast"
    assert _issues(result, _PREFERRED, "value_from_fallback") == ["brand"]


def test_a_pass_through_extra_falls_back_too(tmp_path: Path) -> None:
    # A flag must mean the same thing on every resolution path; an extra that silently ignored
    # its fallback would read blank here while the mapped field above read a value.
    extras = {"web_name": _title()}
    result = _build(tmp_path, {"product_name": _title()}, extras)

    record = next(r for r in result.records if r.gtin == _FALLBACK)
    assert record.extra("web_name", "nl") == "Mini Juicer"
    assert record.extra("web_name", "fr") == "Mini Juicer FR"


def test_a_missing_fallback_column_is_reported_like_a_missing_attribute(tmp_path: Path) -> None:
    result = _build(tmp_path, {"product_name": _title(fallback_attribute="9999")})

    assert any("9999" in e for e in result.errors)


def test_source_label_names_both_attributes() -> None:
    assert source_label(_title()) == "TradeItemDescription attr 3309, else 3297"
    assert source_label(GdsnSource(sheet="S", attribute="3297")) == "S attr 3297"


@pytest.mark.parametrize(
    "overrides",
    [
        {"fallback_attribute": "3309"},  # the same attribute twice is no fallback at all
        {"attribute": ""},  # nothing to fall back from
        {"primary_file": True, "localised": False},  # a file URI has no attribute to swap
    ],
)
def test_a_fallback_that_could_never_apply_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _title(**overrides)
