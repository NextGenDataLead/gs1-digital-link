"""Tests for lib/scope_report.py — the operator's list with what the run did to each row.

The report joins two documents that do not share a vocabulary: a list of barcodes the operator
wrote, and a log keyed by ``(gtin, language)`` the tool wrote. Everything worth asserting here is
about the seams between them.

Above all: **a hold is not a failure.** ``lib.preflight.check_video_coverage`` puts the reason
best — calling a handled condition a failure is how a report earns the right to be ignored — and
this file is forwarded to the client, who will act on the word in the cell.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.process_list import ProcessListSheet
from lib.records import RunOutcome, SkippedUnit, SkipReason
from lib.scope_report import (
    HELD,
    IN_SCOPE,
    NOT_ELIGIBLE,
    NOT_IN_EXPORT,
    NOT_RUN,
    NOT_SELECTED,
    build_rows,
    legend_grid,
    scope_grid,
    units_grid,
)

LANGUAGES = ["nl", "fr"]
HEADER = ["Artikelnr.", "Omschrijving NL", "Barcode", "Categorie"]

A = "02987653000001"
B = "02987653000002"
C = "02987653000003"


def _sheet(rows: list[list[str]]) -> ProcessListSheet:
    return ProcessListSheet(Path("process-list.xlsx"), HEADER, rows, gtin_index=2)


def _row(article: str, name: str, gtin: str, category: str = "webpage + QR") -> list[str]:
    return [article, name, gtin, category]


def _outcome(gtin: str, language: str, status: str, **kwargs: object) -> RunOutcome:
    return RunOutcome(
        gtin=gtin, language=language, ts=datetime(2026, 8, 27, tzinfo=UTC), status=status, **kwargs
    )


def _skip(gtin: str, language: str, reason: SkipReason, detail: str) -> SkippedUnit:
    return SkippedUnit(gtin=gtin, language=language, reason=reason, detail=detail)


def _build(
    sheet: ProcessListSheet,
    *,
    selected: set[str] | None = None,
    exported: set[str] | None = None,
    outcomes: list[RunOutcome] | None = None,
    skipped: list[SkippedUnit] | None = None,
) -> list:
    listed = sheet.listed_gtins()
    return build_rows(
        sheet,
        selected=listed if selected is None else selected,
        exported=listed if exported is None else exported,
        outcomes=outcomes or [],
        skipped=skipped or [],
        languages=LANGUAGES,
    )


def test_one_language_published_and_the_other_failed_shows_both() -> None:
    """The case a single status column cannot hold, and that has actually happened here."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])
    outcomes = [
        _outcome(A, "nl", "ok", wp_url="https://example.test/nl/p-a"),
        _outcome(A, "fr", "error", error="500 Internal Server Error", failed_call="POST /pages"),
    ]

    # Act
    rows = _build(sheet, outcomes=outcomes)

    # Assert
    assert rows[0].units["nl"].status == "ok"
    assert rows[0].units["nl"].page == "https://example.test/nl/p-a"
    assert rows[0].units["fr"].status == "error"
    assert "POST /pages" in rows[0].units["fr"].detail
    assert rows[0].result == "error", "the worst language decides, so one column is filterable"


def test_a_held_unit_is_never_reported_as_a_failure() -> None:
    """Calling a handled condition a failure is how a report earns the right to be ignored."""
    # Arrange
    sheet = _sheet([_row("2078", "Multi Wiper", A)])
    skipped = [
        _skip(A, language, SkipReason.NO_CONFIRMED_VIDEO, "no client-confirmed video")
        for language in LANGUAGES
    ]

    # Act
    rows = _build(sheet, skipped=skipped)

    # Assert
    assert rows[0].result == HELD
    assert [unit.status for unit in rows[0].units.values()] == [HELD, HELD]
    assert "no_confirmed_video" in rows[0].units["nl"].detail, "the reason travels with the status"


def test_a_sku_missing_from_the_export_is_named_as_such_not_as_a_failure() -> None:
    """The fact nothing else in the tool reports: listed, and the export has no row for it."""
    # Arrange
    sheet = _sheet([_row("3086", "Contour King Small", A), _row("1079", "Drain saver", B)])

    # Act
    rows = _build(sheet, exported={B})

    # Assert
    assert rows[0].in_scope == NOT_IN_EXPORT
    assert rows[0].result == NOT_RUN, "nothing ran, and that is not the same as something failing"
    assert rows[1].in_scope == IN_SCOPE


def test_a_deselected_row_is_named_as_the_operators_own_decision() -> None:
    """It is on their uploaded list, so it must appear — and it must not read as a fault."""
    # Arrange
    sheet = _sheet([_row("1079", "keep", A), _row("1080", "drop", B)])

    # Act
    rows = _build(sheet, selected={A})

    # Assert
    assert rows[0].in_scope == IN_SCOPE
    assert rows[1].in_scope == NOT_SELECTED


def test_a_row_the_export_lacks_is_named_so_even_off_the_batch() -> None:
    """Data offers no tick box on it, so leaving it out was never the operator's decision."""
    # Arrange
    sheet = _sheet([_row("1080", "gone", A)])

    # Act
    rows = _build(sheet, selected=set(), exported=set())

    # Assert
    assert rows[0].in_scope == NOT_IN_EXPORT


def test_a_held_row_off_the_batch_says_not_eligible_and_why() -> None:
    """The batch file holds only the ticks now, so the plan never sees a held row to explain it.

    The reason comes from the Data screen's verdict instead, and reads as a hold, not a failure.
    """
    # Arrange
    sheet = _sheet([_row("1079", "keep", A), _row("1080", "held", B), _row("1081", "drop", C)])

    # Act
    rows = build_rows(
        sheet,
        selected={A},
        exported={A, B, C},
        outcomes=[],
        skipped=[],
        languages=LANGUAGES,
        not_eligible={B: "missing data: dim_height"},
    )

    # Assert
    assert [row.in_scope for row in rows] == [IN_SCOPE, NOT_ELIGIBLE, NOT_SELECTED]
    assert rows[1].result == HELD
    assert rows[1].units["nl"].detail == "missing data: dim_height"
    assert rows[2].result == NOT_RUN


def test_a_gtin_with_no_outcome_reads_not_run_and_never_blank() -> None:
    """A blank cell in a status column reads as "fine". This one is a question."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])

    # Act
    rows = _build(sheet)

    # Assert
    assert rows[0].result == NOT_RUN
    assert all(unit.status == NOT_RUN for unit in rows[0].units.values())


def test_a_dry_run_is_not_reported_as_published() -> None:
    """``dry-run`` is 167 of the 261 rows this project has actually logged. It wrote nothing."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])
    outcomes = [_outcome(A, language, "dry-run") for language in LANGUAGES]

    # Act
    rows = _build(sheet, outcomes=outcomes)

    # Assert
    assert rows[0].result == "dry-run"


def test_an_unknown_status_is_passed_through_rather_than_guessed_at() -> None:
    """A log that starts carrying a new status should show it, not fall back on a plausible lie."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])

    # Act
    rows = _build(sheet, outcomes=[_outcome(A, "nl", "quarantined")])

    # Assert
    assert rows[0].units["nl"].status == "quarantined"


def test_the_run_log_wins_over_a_stale_hold() -> None:
    """A unit that ran is a unit that was not held, whatever an older plan says."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])
    skipped = [_skip(A, "nl", SkipReason.NO_CONFIRMED_VIDEO, "no video")]

    # Act
    rows = _build(sheet, outcomes=[_outcome(A, "nl", "ok")], skipped=skipped)

    # Assert
    assert rows[0].units["nl"].status == "ok"


def test_a_thirteen_digit_run_log_still_joins_to_a_fourteen_digit_list() -> None:
    """``ProductRecord.gtin`` is the feed's own form, and some feeds carry 13 digits."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", "2987653000001")])

    # Act
    rows = _build(sheet, outcomes=[_outcome("2987653000001", "nl", "ok")])

    # Assert
    assert rows[0].gtin == A
    assert rows[0].units["nl"].status == "ok"


# --- The grids ----------------------------------------------------------------


def test_the_operators_columns_come_through_verbatim() -> None:
    """The sheet's identity is that it is *their* list. Re-ordering it would end that."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A, "webpage + QR")])
    rows = _build(sheet)

    # Act
    columns, grid = scope_grid(sheet, rows, LANGUAGES)

    # Assert
    assert columns[:4] == HEADER
    assert grid[0][:4] == ["1079", "Drain saver", A, "webpage + QR"]
    assert columns[4:6] == ["in_scope", "result"]
    assert columns[6:] == [
        "status_nl",
        "page_nl",
        "detail_nl",
        "status_fr",
        "page_fr",
        "detail_fr",
    ]


def test_a_blank_header_column_is_labelled_by_position_not_dropped() -> None:
    """Two columns named ``""`` would be one column to anything that reads by name."""
    # Arrange
    sheet = ProcessListSheet(Path("p.xlsx"), ["Artikelnr.", "", "Barcode"], [["1", "x", A]], 2)

    # Act
    columns, grid = scope_grid(sheet, _build(sheet), LANGUAGES)

    # Assert
    assert columns[:3] == ["Artikelnr.", "column 2", "Barcode"]
    assert grid[0][:3] == ["1", "x", A]


def test_a_short_row_is_padded_rather_than_shifting_the_appended_columns() -> None:
    """A row with fewer cells than the header must not slide ``in_scope`` under ``Categorie``."""
    # Arrange
    sheet = ProcessListSheet(Path("p.xlsx"), HEADER, [["1079", "Drain saver", A]], 2)

    # Act
    columns, grid = scope_grid(sheet, _build(sheet), LANGUAGES)

    # Assert
    assert len(grid[0]) == len(columns)
    assert grid[0][4] == IN_SCOPE


def test_the_units_sheet_keeps_what_the_worst_of_reduction_drops() -> None:
    """One row per (GTIN, language), uninterpreted — where "nl ok, fr failed" survives."""
    # Arrange
    outcomes = [_outcome(A, "nl", "ok", wp_page_id=1637), _outcome(A, "fr", "error", error="500")]
    skipped = [_skip(B, "nl", SkipReason.MISSING_MANDATORY_FIELD, "dim_height is blank")]

    # Act
    columns, grid = units_grid(outcomes, skipped)

    # Assert
    assert columns[:4] == ["gtin", "language", "source", "status"]
    assert [row[3] for row in grid] == ["ok", "error", HELD]
    assert [row[2] for row in grid] == ["run log", "run log", "plan"]
    assert "dim_height is blank" in grid[2][-1]


def test_the_legend_explains_every_value_the_report_can_emit() -> None:
    """The file is forwarded on its own; a value that needs a covering email arrives without one."""
    # Arrange
    emitted = {
        IN_SCOPE,
        NOT_SELECTED,
        NOT_ELIGIBLE,
        NOT_IN_EXPORT,
        HELD,
        NOT_RUN,
        "ok",
        "error",
        "dry-run",
    }

    # Act
    _, grid = legend_grid()

    # Assert
    assert {row[1] for row in grid} == emitted
    assert all(row[2].strip().endswith(".") for row in grid), "each is a sentence, not a label"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["ok", "error"], "error"),
        (["ok", NOT_RUN], NOT_RUN),
        ([HELD, NOT_RUN], NOT_RUN),
        (["ok", HELD], HELD),
        (["ok", "dry-run"], "dry-run"),
        (["ok", "ok"], "ok"),
    ],
)
def test_the_worst_language_decides_the_result(statuses: list[str], expected: str) -> None:
    """``not run`` outranks ``held`` because a hold is explained and a silence is not."""
    # Arrange
    sheet = _sheet([_row("1079", "Drain saver", A)])
    outcomes = [
        _outcome(A, language, status)
        for language, status in zip(LANGUAGES, statuses, strict=True)
        if status not in {HELD, NOT_RUN}
    ]
    skipped = [
        _skip(A, language, SkipReason.NO_CONFIRMED_VIDEO, "no video")
        for language, status in zip(LANGUAGES, statuses, strict=True)
        if status == HELD
    ]

    # Act
    rows = _build(sheet, outcomes=outcomes, skipped=skipped)

    # Assert
    assert rows[0].result == expected
