"""Every product picture the feed holds, across all markets, each once (operator, 2026-11-18).

The shapes here are the real export's: the Netherlands and Belgium list different sets for one
product, the German market drops the ``.tif`` from the same file names, a primary flag set in one
market only, and a referenced file that is not a picture.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from lib.gdsn import BuildResult, GdsnSource, build_records, read_workbook
from lib.records import ProductImage

_KEY = [
    "Gtin",
    "TargetMarketCountryCode",
    "InformationProviderOfTradeItem",
    "TradeItemUnitDescriptorCode",
]
_SLOTS = 3
#: One file group's leaves: (sub-path, leaf, label).
_LEAVES = [
    ("ReferencedFileTypeCode", "Value", "Referenced file type code (2469)"),
    (None, "FileName", "File name (2481)"),
    (None, "UniformResourceIdentifier", "Uniform resource identifier (2485)"),
    (None, "IsPrimaryFile", "Is Primary File (4277)"),
]

GTIN = "02987653000001"
OTHER = "02987653000002"  # no file sheet row at all — no pictures, and no error


def _file_sheet(sheet: openpyxl.worksheet.worksheet.Worksheet, data: list[list[object]]) -> None:
    groups = [f"ReferencedFileHeader[{n}]" for n in range(_SLOTS) for _ in _LEAVES]
    subs = [sub for _ in range(_SLOTS) for sub, _, _ in _LEAVES]
    leaves = [leaf for _ in range(_SLOTS) for _, leaf, _ in _LEAVES]
    labels = [label for _ in range(_SLOTS) for _, _, label in _LEAVES]
    sheet.append([*_KEY, *groups])
    sheet.append([None] * 4 + subs)
    sheet.append([None] * 4 + leaves)
    sheet.append(["GTIN (3059)", "Country (3179)", "Provider (3088)", "Unit (3074)", *labels])
    for row in data:
        sheet.append(row)


def _files(gtin: str, market: str, *files: tuple[str, str, str, str]) -> list[object]:
    cells: list[object] = []
    for slot in range(_SLOTS):
        cells += list(files[slot]) if slot < len(files) else [None] * len(_LEAVES)
    return [gtin, market, "GLN", "BASE_UNIT_OR_EACH", *cells]


def _pic(name: str, primary: bool = False, kind: str = "PRODUCT_IMAGE") -> tuple[str, ...]:
    return (kind, name, f"https://cdn/{name}", "TRUE" if primary else "FALSE")


def _build(tmp_path: Path, rows: list[list[object]], *, image_source: bool = True) -> BuildResult:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    names = wb.create_sheet("TradeItemDescription")
    names.append(
        [
            *_KEY,
            "TradeItemDescriptionInformation",
            "TradeItemDescriptionInformation",
            "BrandNameInformation",
        ]
    )
    names.append([None] * 4 + ["DescriptionShort[0]", "DescriptionShort[0]", None])
    names.append([None] * 4 + ["LanguageCode", "Value", "BrandName"])
    names.append(
        [
            "GTIN (3059)",
            "Country (3179)",
            "Provider (3088)",
            "Unit (3074)",
            "Short product name (3297)",
            "Short product name (3297)",
            "Brand Name (3336)",
        ]
    )
    for gtin in (GTIN, OTHER):
        names.append([gtin, "528", "GLN", "BASE_UNIT_OR_EACH", "nl", "Naam", "Noviplast"])
    _file_sheet(wb.create_sheet("ReferencedFileDetailInformation"), rows)
    path = tmp_path / "images.xlsx"
    wb.save(path)
    gdsn_map = {
        "brand": GdsnSource(sheet="TradeItemDescription", attribute="3336"),
        "product_name": GdsnSource(sheet="TradeItemDescription", attribute="3297", localised=True),
    }
    if image_source:
        gdsn_map["image_url"] = GdsnSource(
            sheet="ReferencedFileDetailInformation", primary_file=True
        )
    return build_records(read_workbook(str(path)), gdsn_map, ["528", "056", "276"], ["nl"], "nl")


def _images(result: BuildResult, gtin: str = GTIN) -> list[ProductImage]:
    assert not result.errors, result.errors
    return next(r for r in result.records if r.gtin == gtin).images


def test_every_market_is_combined_in_priority_then_slot_order(tmp_path: Path) -> None:
    result = _build(
        tmp_path,
        [
            _files(GTIN, "056", _pic(f"{GTIN}_C1C1.tif"), _pic(f"{GTIN}_C1L1.tif")),
            _files(GTIN, "528", _pic(f"{GTIN}_C1C0.tif"), _pic(f"{GTIN}_C1C1.tif")),
        ],
    )
    assert [i.name for i in _images(result)] == [
        f"{GTIN}_C1C0.tif",
        f"{GTIN}_C1C1.tif",
        f"{GTIN}_C1L1.tif",
    ]


def test_a_name_without_its_extension_is_the_same_picture(tmp_path: Path) -> None:
    """The German market lists ``…_C1C0`` for the ``…_C1C0.tif`` the others list."""
    result = _build(
        tmp_path,
        [
            _files(GTIN, "528", _pic(f"{GTIN}_C1C0.tif")),
            _files(GTIN, "276", _pic(f"{GTIN}_C1C0")),
        ],
    )
    (only,) = _images(result)
    assert only.name == f"{GTIN}_C1C0.tif"
    assert only.uri == f"https://cdn/{GTIN}_C1C0.tif"


def test_a_picture_is_primary_when_any_market_says_so(tmp_path: Path) -> None:
    result = _build(
        tmp_path,
        [
            _files(GTIN, "528", _pic(f"{GTIN}_C1C1_s01.tif")),
            _files(GTIN, "056", _pic(f"{GTIN}_C1C1_s01.tif", primary=True)),
        ],
    )
    (only,) = _images(result)
    assert only.primary


def test_a_file_that_is_not_a_picture_is_left_out(tmp_path: Path) -> None:
    result = _build(
        tmp_path,
        [_files(GTIN, "528", _pic(f"{GTIN}_C1C0.tif"), _pic("leaflet.pdf", kind="DOCUMENT"))],
    )
    assert [i.name for i in _images(result)] == [f"{GTIN}_C1C0.tif"]


def test_a_product_with_no_file_row_has_no_pictures(tmp_path: Path) -> None:
    result = _build(tmp_path, [_files(GTIN, "528", _pic(f"{GTIN}_C1C0.tif"))])
    assert _images(result, OTHER) == []


def test_a_config_with_no_picture_source_collects_none(tmp_path: Path) -> None:
    result = _build(tmp_path, [_files(GTIN, "528", _pic(f"{GTIN}_C1C0.tif"))], image_source=False)
    assert _images(result) == []
