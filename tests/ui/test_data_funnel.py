"""The Data screen's coverage funnel and the caption above Next — counted, not rendered.

Pure: rows in the shape :func:`ui.batch_grid.choose` builds them, an :class:`Eligibility`, numbers
out. The funnel is what the operator reads to know what a run will do, so each of its figures is
pinned to the split the plan makes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from lib.eligibility import CHECKING, Eligibility, LinkIssue  # noqa: E402
from lib.process_list import ProcessListSheet  # noqa: E402
from ui.batch_grid import (  # noqa: E402
    _GTIN,
    _ROW,
    CAN_RUN,
    CAN_RUN_NO_VIDEO,
    CHECKING_LINK,
    LINK_PROBLEM,
    NOT_ELIGIBLE,
    NOT_IN_EXPORT,
    RUNNABLE,
    Funnel,
    batch_of,
    funnel,
    row_status,
    runnable_keys,
    save_line,
    shared_barcode_notes,
    ticked_products,
    unticked_by,
)

_A, _B, _C, _D, _E = (f"0298765300000{n}" for n in range(1, 6))


def _rows(*gtins: str | None) -> list[dict[str, Any]]:
    return [{_ROW: n, _GTIN: gtin} for n, gtin in enumerate(gtins)]


#: A eligible with a video · B eligible, missing fr · C held for data · D not in the export · E ok
_VERDICT = Eligibility(
    not_eligible={_C: "missing data: x"}, missing_video={_B: "no confirmed video in fr"}
)
_EXPORTED = {_A, _B, _C, _E}


def test_coverage_counts_the_ticked_products_not_the_list() -> None:
    """Operator, 2026-10-09: the figures are about the batch, not the whole list."""
    rows = _rows(_A, _B, _C, _D, _E)

    counts = funnel(rows, ticked=[rows[1], rows[2], rows[3]], verdict=_VERDICT, exported=_EXPORTED)

    assert counts == Funnel(
        listed=5,
        selected=3,
        can_run=1,
        not_in_export=1,
        not_eligible=1,
        missing_video=1,
        bad_link=0,
    )


def test_an_unticked_problem_is_not_counted() -> None:
    rows = _rows(_A, _C, _D)

    counts = funnel(rows, ticked=[rows[0]], verdict=_VERDICT, exported=_EXPORTED)

    assert (counts.selected, counts.can_run, counts.not_in_export, counts.not_eligible) == (
        1,
        1,
        0,
        0,
    )


def test_products_are_counted_once_however_many_rows_name_them() -> None:
    """The pilot's own list carries one barcode on two rows; a run publishes it once."""
    rows = _rows(_A, _A, None)

    counts = funnel(rows, ticked=rows[:2], verdict=Eligibility(), exported={_A})

    assert (counts.listed, counts.selected, counts.can_run) == (1, 1, 1)


def test_a_ticked_row_that_cannot_run_is_selected_but_never_saved() -> None:
    rows = _rows(_C)

    counts = funnel(rows, ticked=rows, verdict=_VERDICT, exported=_EXPORTED)

    assert (counts.selected, counts.can_run) == (1, 0)


def test_an_undecidable_batch_has_nothing_that_can_run() -> None:
    """An unreadable mapping under the video rule holds everything; the funnel must agree."""
    rows = _rows(_A)

    counts = funnel(rows, ticked=rows, verdict=Eligibility(problem="x"), exported={_A})

    assert (counts.can_run, counts.not_eligible) == (0, 1)


def test_a_links_only_batch_counts_its_broken_links_not_its_data() -> None:
    rows = _rows(_A, _B)
    verdict = Eligibility(bad_link={_B: LinkIssue("https://x/y", "the page does not exist (404)")})

    counts = funnel(rows, ticked=rows, verdict=verdict, exported={_A, _B})

    assert (counts.can_run, counts.bad_link, counts.not_eligible) == (1, 1, 0)


def test_the_caption_says_what_next_will_save_before_it_is_pressed() -> None:
    assert save_line(3, 3) == "Next saves all 3 ticked product(s) and goes on to the copy."
    assert save_line(4, 3, onward="the preflight") == (
        "Next saves 3 of 4 ticked product(s) — 1 cannot run and is left out, see below — and "
        "goes on to the preflight."
    )
    assert save_line(5, 3).startswith("Next saves 3 of 5 ticked product(s) — 2 cannot run and are")
    assert save_line(0, 0) == "Tick at least one product — Next stays off until then."
    assert save_line(2, 0) == (
        "None of the ticked products can run — see the tables below. Next stays off."
    )


# --- a row's status ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("gtin", "status", "detail"),
    [
        (_A, CAN_RUN, ""),
        (_B, CAN_RUN_NO_VIDEO, "no confirmed video in fr"),
        (_C, NOT_ELIGIBLE, "missing data: x"),
        (_D, NOT_IN_EXPORT, ""),
        (None, NOT_IN_EXPORT, ""),
    ],
)
def test_each_row_says_what_a_run_would_do_with_it(
    gtin: str | None, status: str, detail: str
) -> None:
    assert row_status(gtin, _EXPORTED, _VERDICT) == (status, detail)


def test_a_link_still_being_checked_reads_as_checking_not_broken() -> None:
    verdict = Eligibility(
        bad_link={
            _A: LinkIssue("https://x/a", CHECKING),
            _B: LinkIssue("https://x/b", "the page does not exist (404)"),
        }
    )

    assert row_status(_A, {_A, _B}, verdict) == (CHECKING_LINK, CHECKING)
    assert row_status(_B, {_A, _B}, verdict)[0] == LINK_PROBLEM
    assert {CHECKING_LINK, LINK_PROBLEM}.isdisjoint(RUNNABLE)


# --- what a restart ticks, and what Next writes ---------------------------------


def test_a_restart_ticks_the_saved_batch_and_nothing_else() -> None:
    """The grid is the whole upload; only the barcodes the saved batch names arrive ticked."""
    rows = _rows(_A, _B, _D)

    assert unticked_by(rows, saved=frozenset({_B})) == {0, 2}


def test_a_barcode_on_two_rows_is_ticked_on_both() -> None:
    eligible = _rows(_A, _A, _E)

    assert unticked_by(eligible, saved=frozenset({_A})) == {2}


def test_next_writes_the_ticked_rows_only_and_to_the_control_file(tmp_path: Path) -> None:
    """Not the held rows, not the rows the export lacks: the batch file is the batch.

    It used to keep every not-eligible row too, and the screens after Data read "24 of 118
    ticked" for a batch of 3.
    """
    upload = ProcessListSheet(
        path=tmp_path / "uploads" / "product-list.xlsx",
        header=["Artikelnr.", "Barcode"],
        rows=[["1", _A], ["2", _C], ["3", _D], ["4", _E]],
        gtin_index=1,
    )
    control = tmp_path / "selection" / "selections.xlsx"

    saved = batch_of(upload, {0, 3}, control)

    assert saved.path == control
    assert saved.rows == [["1", _A], ["4", _E]]
    assert upload.rows[1] == ["2", _C], "the upload itself is never pruned"


# --- one barcode on several rows ------------------------------------------------


def _list(*rows: tuple[str, str, str]) -> ProcessListSheet:
    return ProcessListSheet(
        path=Path("list.xlsx"),
        header=["Artikelnr.", "Omschrijving", "Barcode"],
        rows=[list(row) for row in rows],
        gtin_index=2,
    )


def test_two_different_products_on_one_barcode_are_a_warning_naming_both() -> None:
    """7 Days and Joy Grill on 2987653008486 — the export says it is the grill."""
    sheet = _list(("4214", "7 Days", "2987653008486"), ("5003", "Joy Grill", "2987653008486"))

    notes = shared_barcode_notes(sheet, {"02987653008486": "Grillen"})

    assert len(notes) == 1
    gtin, kind, sentence = notes[0]
    assert gtin == "02987653008486"
    assert kind == "warn"
    assert "4214 7 Days" in sentence and "5003 Joy Grill" in sentence
    assert "The export says it is Grillen." in sentence


def test_the_same_product_twice_is_a_quiet_note() -> None:
    sheet = _list(("4156", "Desk lamp", "2987653004488"), ("4156", "Desk lamp", "2987653004488"))

    assert [kind for _, kind, _ in shared_barcode_notes(sheet, {})] == ["quiet"]


def test_a_list_without_shared_barcodes_says_nothing() -> None:
    sheet = _list(("4156", "Desk lamp", "2987653004488"), ("2078", "Multi Wiper", "2987653007151"))

    assert shared_barcode_notes(sheet, {}) == []


def test_next_writes_only_the_ticked_rows_that_can_run() -> None:
    """A ticked row the run would drop is named in the caption, never written: the file is the
    batch, and a row in it the run then drops would read as chosen on every later screen."""
    ticked = [
        {_ROW: 0, _GTIN: _A, "_status": CAN_RUN},
        {_ROW: 1, _GTIN: _B, "_status": CAN_RUN_NO_VIDEO},
        {_ROW: 2, _GTIN: _C, "_status": NOT_ELIGIBLE},
        {_ROW: 3, _GTIN: _D, "_status": NOT_IN_EXPORT},
        {_ROW: 4, _GTIN: _E, "_status": LINK_PROBLEM},
    ]

    assert runnable_keys(ticked) == {0, 1}


def test_the_issue_report_counts_each_ticked_product_once_in_list_order() -> None:
    rows = _rows(_B, None, _A, _B)

    assert ticked_products(rows, {_A: "Vergiet"}) == [(_B, ""), (_A, "Vergiet")]
