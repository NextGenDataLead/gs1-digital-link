"""Tests for lib/gdsn_layout.py — the header grid, checked against the reader that reads it.

Every test here goes through :func:`lib.gdsn.read_workbook` rather than the layout module's own
idea of what it wrote. The two are mirrors of one shape, and a fixture written by one and
verified by the other is the only version of this test that could fail: comparing the writer to
itself would pass however wrong the shape was.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.gdsn import read_workbook
from lib.gdsn_layout import (
    HEADER_DEPTH,
    KEY_COLUMNS,
    Column,
    header_rows,
    localised_cells,
    localised_columns,
    measurement_cells,
    measurement_columns,
    multislot_cells,
)

_GTIN = "00290000000012"
_MARKET = "528"


def _read_back(tmp_path: Path, columns: list[Column], cells: list[object]) -> object:
    """Write one sheet with one data row, and hand it back as the reader sees it."""
    rows = header_rows(columns)
    rows.append([_GTIN, _MARKET, "GLN", "BASE_UNIT_OR_EACH", *cells])
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    sheet = workbook.create_sheet("TradeItemDescription")
    for row in rows:
        sheet.append(row)
    path = tmp_path / "layout.xlsx"
    workbook.save(path)
    return read_workbook(str(path))["TradeItemDescription"]


def test_every_column_path_and_label_survives_the_round_trip(tmp_path: Path) -> None:
    columns = [
        *KEY_COLUMNS,
        *localised_columns(("Module",), "Group", 2, "Value label (1234)", "Language (1234)"),
        *measurement_columns(("Module", "Nested"), "Measure", "Measure label (5678)"),
        Column(("Module", "Deep", "Deeper", "Leaf"), "Deep label (9012)"),
    ]
    sheet = _read_back(tmp_path, columns, [None] * (len(columns) - len(KEY_COLUMNS)))

    assert [column.path for column in sheet.columns] == [column.path for column in columns]
    assert [column.label for column in sheet.columns] == [column.label for column in columns]


@pytest.mark.parametrize(
    ("path", "expected_rows"),
    [
        # The alignment is to the leaf, not the root: the first segment stays on row 0 and the
        # rest pack upward from the last header row. Mirrors what the real export does, and is
        # why sheets of different depth still put their leaf on the same row.
        (("One",), [0]),
        (("One", "Two"), [0, 5]),
        (("One", "Two", "Three"), [0, 4, 5]),
        (("One", "Two", "Three", "Four"), [0, 3, 4, 5]),
    ],
)
def test_path_segments_are_bottom_aligned_under_the_first(
    path: tuple[str, ...], expected_rows: list[int]
) -> None:
    grid = header_rows([Column(path, "Label (1)")])
    occupied = [row for row in range(HEADER_DEPTH - 1) if grid[row][0] is not None]

    assert occupied == expected_rows
    assert [grid[row][0] for row in occupied] == list(path)
    assert grid[HEADER_DEPTH - 1][0] == "Label (1)"


def test_a_localised_value_is_found_by_its_language_sibling(tmp_path: Path) -> None:
    columns = [*KEY_COLUMNS, *localised_columns(("M",), "G", 2, "Naam (3301)", "Naam (3301)")]
    cells = localised_cells({"nl": "Voegstrijker", "fr": "Lisseur"}, ("nl", "fr"), 2, ("nl", "fr"))
    sheet = _read_back(tmp_path, columns, cells)

    assert sheet.pick_localised(_GTIN, _MARKET, "3301", "nl") == "Voegstrijker"
    assert sheet.pick_localised(_GTIN, _MARKET, "3301", "fr") == "Lisseur"


def test_a_language_the_market_does_not_carry_leaves_no_dangling_language_code(
    tmp_path: Path,
) -> None:
    """Both cells of the slot stay empty, so nothing can pair a language onto a blank value."""
    columns = [*KEY_COLUMNS, *localised_columns(("M",), "G", 2, "Naam (3301)", "Naam (3301)")]
    cells = localised_cells({"nl": "Voegstrijker", "fr": "Lisseur"}, ("nl", "fr"), 2, ("nl",))
    sheet = _read_back(tmp_path, columns, cells)

    assert sheet.pick_localised(_GTIN, _MARKET, "3301", "fr") is None
    assert cells[2:4] == [None, None]


def test_one_language_spread_over_several_slots_is_read_back_whole(tmp_path: Path) -> None:
    columns = [*KEY_COLUMNS, *localised_columns(("M",), "G", 6, "USP (1067)", "Language (1067)")]
    benefits = {"nl": ("Eerst", "Tweede", "Derde"), "fr": ("Premier",)}
    cells = multislot_cells(benefits, ("nl", "fr"), 6, ("nl", "fr"))
    sheet = _read_back(tmp_path, columns, cells)

    assert sheet.pick_localised_all(_GTIN, _MARKET, "1067", "nl") == ["Eerst", "Tweede", "Derde"]
    assert sheet.pick_localised_all(_GTIN, _MARKET, "1067", "fr") == ["Premier"]


def test_a_measurement_carries_its_unit_and_a_blank_one_carries_nothing(tmp_path: Path) -> None:
    columns = [
        *KEY_COLUMNS,
        *measurement_columns(("M",), "Height", "Height (3498)"),
        *measurement_columns(("M",), "Width", "Width (3520)"),
    ]
    cells = [*measurement_cells("210", "MMT"), *measurement_cells(None, "MMT")]
    sheet = _read_back(tmp_path, columns, cells)

    assert sheet.pick_scalar(_GTIN, _MARKET, "3498", with_unit=True) == "210 MMT"
    assert sheet.pick_scalar(_GTIN, _MARKET, "3520", with_unit=True) is None
