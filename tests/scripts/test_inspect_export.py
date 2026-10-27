"""Tests for scripts/inspect_export.py (IMPLEMENTATION_SPEC §8.5).

The fixture carries **both** 3297 and 3318 on one sheet, because that is the pairing the
script exists to get right and previously got wrong. 3297 is labelled *"Short product
name"* and holds an internal logistics string; 3318 is labelled *"Trade item description"*
and holds the marketing name the page titles. Suggesting the former as ``product_name`` is
the bug fixed in c76492b, which this script then went on re-proposing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml

from scripts import inspect_export

# What each attribute really holds, per the tuned clients.yml.
_LOGISTICS_3297 = "Schroefverwijderaar metaal grs"
_MARKETING_3318 = "Democlient Rugsteun"


def _write_gdsn_xlsx(tmp_path: Path) -> str:
    header = [
        [
            "Gtin",
            "TargetMarketCountryCode",
            "InformationProviderOfTradeItem",
            "TradeItemUnitDescriptorCode",
            "TradeItemDescriptionInformation",
            "TradeItemDescriptionInformation",
            "TradeItemDescriptionInformation",
            "TradeItemDescriptionInformation",
            "TradeItemDescriptionInformation",
        ],
        [
            None,
            None,
            None,
            None,
            "DescriptionShort[0]",
            "DescriptionShort[0]",
            "TradeItemDescription[0]",
            "TradeItemDescription[0]",
            "BrandNameInformation",
        ],
        [
            None,
            None,
            None,
            None,
            "LanguageCode",
            "Value",
            "LanguageCode",
            "Value",
            "BrandName",
        ],
        [None] * 9,
        [None] * 9,
        [None] * 9,
        [
            "GTIN (3059)",
            "Country (3179)",
            "Provider (3088)",
            "Unit (3074)",
            # The label that caused the bug: it reads like the page title and is not.
            "Short product name (3297)",
            "Short product name (3297)",
            "Trade item description (3318)",
            "Trade item description (3318)",
            "Brand Name (3336)",
        ],
    ]
    data = [
        [
            "02987653007359",
            "528",
            "GLN",
            "BASE_UNIT_OR_EACH",
            "nl",
            _LOGISTICS_3297,
            "nl",
            _MARKETING_3318,
            "Democlient",
        ]
    ]
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    sheet = workbook.create_sheet("TradeItemDescription")
    for row in [*header, *data]:
        sheet.append(row)
    path = tmp_path / "gdsn.xlsx"
    workbook.save(path)
    return str(path)


def _suggested_map(out: str) -> dict[str, Any]:
    """Parse the suggested ``gdsn_map`` back out of stdout.

    Parsed rather than string-matched: what matters is the mapping the operator would
    paste into clients.yml, and a substring assertion cannot tell ``product_name: 3318``
    from ``description_long: 3318``. Everything from the header on is valid YAML — the
    surrounding prose is all ``#`` comments.
    """
    block = yaml.safe_load(out[out.index("# Suggested clients.yml export block") :])
    return dict(block["export"]["gdsn_map"])


def test_reports_sheets_and_suggests_map(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = inspect_export.main([_write_gdsn_xlsx(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "### TradeItemDescription" in out
    assert "Short product name" in out
    assert "gdsn_map:" in out
    assert "product_name:" in out
    assert "brand:" in out


def test_suggests_3318_as_product_name_not_3297(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression. 3297 is DescriptionShort; suggesting it titles every page wrong."""
    inspect_export.main([_write_gdsn_xlsx(tmp_path)])

    gdsn_map = _suggested_map(capsys.readouterr().out)
    assert gdsn_map["product_name"]["attribute"] == "3318"
    # 3297 belongs in gdsn_extras as a pass-through, not mapped to any page field.
    assert "3297" not in {entry["attribute"] for entry in gdsn_map.values()}


def test_does_not_suggest_3318_as_description_long(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """3318 is the name. The other half of the same swap."""
    inspect_export.main([_write_gdsn_xlsx(tmp_path)])

    gdsn_map = _suggested_map(capsys.readouterr().out)
    assert "description_long" not in gdsn_map  # 1067 is absent from this fixture


def test_names_the_tuning_it_cannot_infer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Omitting these silently is how a suggestion gets pasted in as though complete.
    inspect_export.main([_write_gdsn_xlsx(tmp_path)])

    printed = capsys.readouterr().out
    assert "strip_prefix" in printed
    assert "max_length" in printed
    assert "gdsn_extras" in printed


def test_missing_file_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = inspect_export.main(["/no/such/file.xlsx"])

    assert code == 1
    assert "cannot read export" in capsys.readouterr().err


def test_wrong_arg_count_returns_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert inspect_export.main([]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag_prints_usage_and_succeeds(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    # docs/setup.md tells operators every script answers --help. This script takes a bare
    # path, so without an explicit branch the flag was read as a filename and crashed.
    assert inspect_export.main([flag]) == 0
    assert "usage:" in capsys.readouterr().out


def test_non_xlsx_file_reports_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # openpyxl raises InvalidFileException, which is NOT an OSError — before it was
    # caught, a typo'd path or a .xls escaped as an unhandled traceback.
    not_a_workbook = tmp_path / "products.xls"
    not_a_workbook.write_text("this is not a spreadsheet")

    code = inspect_export.main([str(not_a_workbook)])

    assert code == 1
    assert "cannot read export" in capsys.readouterr().err
