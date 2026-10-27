"""Choosing the barcode column of an uploaded product list (:mod:`lib.list_columns`).

A list headed some other way used to be refused. Now it is read by shape, the operator picks the
column, and what is installed reads exactly like a list headed the configured way — which these
tests check with the run's own reader, not with this module's idea of it.
"""

from __future__ import annotations

import io
from pathlib import Path

import openpyxl

from lib.config import ProcessListConfig
from lib.list_columns import (
    NOT_USED_SUFFIX,
    ListColumns,
    barcode_problem,
    guess,
    is_as_configured,
    normalised_header,
    read_upload,
)
from lib.process_list import load_listed_targets, load_process_list
from ui.process_list_edit import archive, normalised_workbook

CONFIG = ProcessListConfig(path="unused.xlsx", gtin_column="Barcode", target_url_column="Link")
A, B = "02987653000527", "02987653000011"


def _workbook(*rows: list[object]) -> bytes:
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_a_list_headed_the_configured_way_is_found_and_needs_nothing() -> None:
    grid = read_upload(_workbook(["Naam", "Barcode"], ["Doek", A]), CONFIG)

    assert grid is not None
    barcode, link = guess(grid, CONFIG)
    assert (barcode, link) == (1, None)
    assert is_as_configured(grid, ListColumns(barcode, link), CONFIG)


def test_an_ean_column_under_a_title_row_is_found_and_guessed() -> None:
    grid = read_upload(_workbook(["Selectie oktober"], ["Naam", "EAN code"], ["Doek", A]), CONFIG)

    assert grid is not None and grid.header == ["Naam", "EAN code"]
    assert guess(grid, CONFIG) == (1, None)


def test_a_column_nobody_anticipated_is_offered_by_shape_with_no_guess() -> None:
    grid = read_upload(_workbook(["Artikel", "Nummer"], ["Doek", A]), CONFIG)

    assert grid is not None and grid.header == ["Artikel", "Nummer"]
    assert guess(grid, CONFIG) == (None, None)


def test_the_chosen_columns_take_the_configured_headings_and_a_rival_is_set_aside() -> None:
    grid = read_upload(_workbook(["Barcode", "Nummer", "URL"], ["oud", A, "https://x/p/"]), CONFIG)
    assert grid is not None

    header = normalised_header(grid, ListColumns(barcode=1, link=2), CONFIG)

    assert header == ["Barcode" + NOT_USED_SUFFIX, "Barcode", "Link"]


def test_what_is_installed_reads_with_the_runs_own_reader(tmp_path: Path) -> None:
    upload = _workbook(["Lijst"], ["Artikel", "Nummer", "URL"], ["Doek", A, ""], ["Bezem", B, ""])
    grid = read_upload(upload, CONFIG)
    assert grid is not None
    chosen = ListColumns(barcode=1, link=2)
    config = ProcessListConfig(
        path=str(tmp_path / "selection" / "selections.xlsx"),
        gtin_column="Barcode",
        target_url_column="Link",
    )

    archive(config, upload, normalised_workbook(normalised_header(grid, chosen, config), grid.rows))

    assert load_process_list(config) == frozenset({A, B})
    assert load_listed_targets(config, "https://x") == {}
    dated = sorted((tmp_path / "selection").parent.glob("uploads/product-list-*.xlsx"))
    assert dated and dated[-1].read_bytes() == upload, "the upload is kept exactly as sent"


def test_a_column_of_names_is_not_a_barcode_column_and_says_what_it_holds() -> None:
    grid = read_upload(_workbook(["Artikel", "Nummer"], ["Doek", "2987653 000527"]), CONFIG)
    assert grid is not None

    assert barcode_problem(grid, ListColumns(0)) == (
        "the column 'Artikel' holds no barcodes (it starts: Doek)"
    )
    assert barcode_problem(grid, ListColumns(1)) is None, "spaces inside a barcode are fine"


def test_a_file_with_no_table_has_no_grid() -> None:
    assert read_upload(_workbook(), CONFIG) is None
