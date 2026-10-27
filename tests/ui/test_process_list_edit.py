"""Tests for ui/process_list_edit.py — installing, pruning and restoring the control file.

The operator's recurring job, and the one step where the shell writes to a file they authored.
The properties worth asserting are the ones that make that safe: other columns survive, the
previous version survives, the file they *uploaded* survives every save after it — the per-run
result sheet reads it to name the rows they dropped — and an empty result is refused.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.config import ProcessListConfig
from lib.errors import ProcessListError
from lib.process_list import load_process_list
from ui.process_list_edit import (
    archive,
    archive_path,
    read_sheet,
    restore_from_upload,
    save_sheet,
)

GTIN_A = "2987653007359"
GTIN_B = "2987653007360"


def _write(tmp_path: Path, rows: list[list[object]], header: list[str] | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header or ["Artikelnr.", "Barcode", "Omschrijving"])
    for row in rows:
        sheet.append(row)
    path = tmp_path / "process-list.xlsx"
    workbook.save(path)
    return path


def _config(path: Path) -> ProcessListConfig:
    return ProcessListConfig(path=str(path), gtin_column="Barcode")


def test_reading_keeps_every_column(tmp_path: Path) -> None:
    """Only the GTIN column is configured; the rest are the operator's working notes."""
    path = _write(tmp_path, [["1079", GTIN_A, "Drain saver"]])

    sheet = read_sheet(_config(path))

    assert sheet.header == ["Artikelnr.", "Barcode", "Omschrijving"]
    assert sheet.rows == [["1079", GTIN_A, "Drain saver"]]
    assert sheet.gtin_index == 1


def test_an_integer_barcode_keeps_its_digits(tmp_path: Path) -> None:
    """openpyxl hands back a float for a numeric cell; ``8.7e+12`` is not a barcode."""
    path = _write(tmp_path, [["1079", int(GTIN_A), "Drain saver"]])

    sheet = read_sheet(_config(path))

    assert sheet.rows[0][1] == GTIN_A


def test_a_missing_gtin_column_says_so_the_way_the_cli_does(tmp_path: Path) -> None:
    path = _write(tmp_path, [["1079", GTIN_A]], header=["Artikelnr.", "EAN"])

    with pytest.raises(ProcessListError, match="Barcode"):
        read_sheet(_config(path))


def test_saving_keeps_the_other_columns_and_leaves_the_pruned_rows_recoverable(
    tmp_path: Path,
) -> None:
    """The dated copy holds what was **saved**; the pruned row survives in the archive.

    There is no undo in a web form, so something has to hold the rows that were just dropped. For
    an uploaded list that is ``uploaded.xlsx``, which is untouched by any save. This list was
    placed by hand and never uploaded, so the first save files it as the upload it effectively is
    — the list as it arrived, just not through the picker. Filing it as a *selection* would record
    a choice nobody made.
    """
    path = _write(tmp_path, [["1079", GTIN_A, "Drain saver"], ["1080", GTIN_B, "Airfryer basket"]])
    sheet = read_sheet(_config(path))

    dated = save_sheet(sheet.without({1}))

    assert read_sheet(_config(dated)).rows == [["1079", GTIN_A, "Drain saver"]], (
        "the dated copy is what was saved, not what it replaced"
    )
    assert read_sheet(_config(path)).rows == [["1079", GTIN_A, "Drain saver"]]
    assert read_sheet(_config(archive_path(path))).rows[1][1] == GTIN_B, (
        "the pruned row is not recoverable anywhere"
    )


def test_seeding_an_archive_never_overwrites_a_real_upload(tmp_path: Path) -> None:
    """The seeding is for hand-placed lists only, and must not touch an uploaded one.

    If it ran on every save it would replace the operator's original with a pruned version of
    itself — the archive would agree with the control file, and the rows they dropped would be
    unrecoverable while a file named ``uploaded.xlsx`` sat there claiming otherwise.
    """
    control = tmp_path / "process" / "selection" / "selections.xlsx"
    source = _write(tmp_path / "upload", [["1", GTIN_A, "a"], ["2", GTIN_B, "b"]])
    archive(_config(control), source.read_bytes())

    save_sheet(read_sheet(_config(control)).keeping({0}))

    assert read_sheet(_config(archive_path(control))).rows == [
        ["1", GTIN_A, "a"],
        ["2", GTIN_B, "b"],
    ], "the save overwrote the operator's own upload"


def test_the_pruned_file_is_what_the_pipeline_then_reads(tmp_path: Path) -> None:
    """The point of the whole screen: the CLI must agree with what the operator just saved."""
    path = _write(tmp_path, [["1079", GTIN_A, "keep"], ["1080", GTIN_B, "drop"]])
    sheet = read_sheet(_config(path))

    save_sheet(sheet.without({1}))

    assert load_process_list(_config(path)) == frozenset({f"0{GTIN_A}"})


def test_saving_an_empty_list_is_refused(tmp_path: Path) -> None:
    """An empty control file yields an empty plan and a run that reports success publishing nothing.

    Refused here rather than at ``load_process_list``, because by then the operator's pruning is
    already lost and they have to redo it to find out.
    """
    path = _write(tmp_path, [["1079", GTIN_A, "Drain saver"]])
    sheet = read_sheet(_config(path))

    with pytest.raises(ProcessListError, match="report success"):
        save_sheet(sheet.without({0}))

    assert read_sheet(_config(path)).rows  # the file on disk is untouched


def test_without_does_not_mutate_the_original(tmp_path: Path) -> None:
    path = _write(tmp_path, [["1079", GTIN_A, "a"], ["1080", GTIN_B, "b"]])
    sheet = read_sheet(_config(path))

    pruned = sheet.without({0})

    assert len(sheet.rows) == 2
    assert len(pruned.rows) == 1


# --- Pruning in more than one pass --------------------------------------------
#
# The Data screen's grid keys each row by its position when the grid was built, and that key never
# changes. A sheet renumbers on every edit. Feeding fixed keys into a renumbered sheet is correct
# once and wrong from the second removal onward — and wrong in the way that matters least visibly:
# the grid shows one set of rows, the file receives another, and the save reports success. That is
# a live page and a permanent GS1 record for a product the operator did not choose.


def _rows(n: int) -> list[list[object]]:
    return [[f"art-{i}", f"298765300{i:04d}", f"name-{i}"] for i in range(n)]


def test_keeping_selects_by_original_position_and_holds_the_order(tmp_path: Path) -> None:
    sheet = read_sheet(_config(_write(tmp_path, _rows(5))))

    kept = sheet.keeping({0, 2, 4})

    assert [row[1] for row in kept.rows] == [sheet.rows[i][1] for i in (0, 2, 4)]
    assert len(sheet.rows) == 5, "the original is untouched"


def test_two_removals_leave_the_file_agreeing_with_the_grid(tmp_path: Path) -> None:
    """The regression. Remove one row, then another, exactly as the screen does it."""
    sheet = read_sheet(_config(_write(tmp_path, _rows(5))))
    grid = [{"_row": n, "gtin": row[1]} for n, row in enumerate(sheet.rows)]

    for selection in ({0}, {3}):  # two passes, keys taken from the original grid both times
        grid = [row for row in grid if row["_row"] not in selection]
        pruned = sheet.keeping({int(row["_row"]) for row in grid})

    assert [row[1] for row in pruned.rows] == [row["gtin"] for row in grid], (
        "the file would receive rows other than the ones left on screen"
    )


def test_the_incremental_form_is_the_one_that_drifts(tmp_path: Path) -> None:
    """Why ``keeping`` exists, asserted rather than described — ``without`` renumbers."""
    sheet = read_sheet(_config(_write(tmp_path, _rows(5))))
    grid = [{"_row": n, "gtin": row[1]} for n, row in enumerate(sheet.rows)]

    drifting = sheet
    for selection in ({0}, {3}):
        grid = [row for row in grid if row["_row"] not in selection]
        drifting = drifting.without(selection)

    assert [row[1] for row in drifting.rows] != [row["gtin"] for row in grid]


# --- The reader is the run's reader -------------------------------------------


def test_a_header_below_row_one_reads_on_screen_too(tmp_path: Path) -> None:
    """The bug this screen had: openpyxl, header fixed at row 1, against Strict-OOXML files.

    A real operator list has a report title above the table. It loaded in a run and failed here,
    which is the worst place for the two to disagree — the operator is looking at the screen.
    """
    # Arrange
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(["Voorraadlijst Q3"])
    worksheet.append([])
    worksheet.append(["Artikelnr.", "Barcode", "Omschrijving"])
    worksheet.append(["1079", GTIN_A, "Drain saver"])
    path = tmp_path / "process-list.xlsx"
    workbook.save(path)

    # Act
    sheet = read_sheet(_config(path))

    # Assert
    assert sheet.header == ["Artikelnr.", "Barcode", "Omschrijving"]
    assert sheet.rows == [["1079", GTIN_A, "Drain saver"]]
    assert sheet.gtin_index == 1


# --- The upload, and putting it back ------------------------------------------


def test_the_upload_is_filed_with_the_uploads_not_beside_the_live_file(tmp_path: Path) -> None:
    """An upload is what the operator sent; the live file is what they chose from it.

    Filing the original inside the folder named after the thing derived from it reads backwards
    the moment anybody looks — and the result sheet reads this archive to name the rows they
    dropped, so getting it confused with a pruned save would have it name the wrong ones,
    silently, in a file that goes to the client.
    """
    # Arrange
    control = tmp_path / "process" / "selection" / "selections.xlsx"

    # Act
    kept = archive_path(control)

    # Assert
    assert kept == tmp_path / "process" / "uploads" / "product-list.xlsx"
    assert kept != control.with_suffix(".bak.xlsx")


def test_an_upload_is_archived_byte_for_byte_and_becomes_the_control_file(tmp_path: Path) -> None:
    # Arrange
    source = _write(tmp_path / "upload", [["1079", GTIN_A, "Drain saver"]])
    control = tmp_path / "input" / "process" / "selection" / "selections.xlsx"
    data = source.read_bytes()

    # Act
    kept = archive(_config(control), data)

    # Assert
    assert kept.read_bytes() == data
    assert control.read_bytes() == data
    assert load_process_list(_config(control)) == frozenset({f"0{GTIN_A}"})


def test_an_upload_that_will_not_read_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """Refused while the operator is still looking at the upload button, not on Preflight."""
    # Arrange — a realistic control path: the archives are siblings of ``in-use/``, so a control
    # file sitting at the root of the client folder would scatter them a level too high.
    control = _write(
        tmp_path / "process" / "selection" / "selections", [["1079", GTIN_A, "Drain saver"]]
    )
    before = control.read_bytes()

    # Act / Assert
    with pytest.raises(ProcessListError):
        archive(_config(control), b"this is not a workbook")

    assert control.read_bytes() == before, "the list they were working from is untouched"
    assert not archive_path(control).exists()


def test_an_upload_with_no_gtins_is_refused(tmp_path: Path) -> None:
    """An empty list plans nothing and reports success. Caught at the door."""
    # Arrange
    empty = _write(tmp_path / "upload", [["1079", None, "Drain saver"]])
    control = tmp_path / "input" / "process" / "selection" / "selections.xlsx"

    # Act / Assert
    with pytest.raises(ProcessListError, match="report success"):
        archive(_config(control), empty.read_bytes())

    assert not control.exists()


def test_every_save_is_kept_dated_and_the_upload_is_never_filed_as_one(tmp_path: Path) -> None:
    """One dated file per save, none per non-save — and the count is how you see it.

    Archiving the file being *replaced* produced two wrong records at once: the first save filed
    the untouched upload under selections, as a choice nobody made, and the selection actually in
    force had no dated copy until a later save displaced it. After one upload and two saves there
    must be exactly two selection files, holding what was chosen.
    """
    # Arrange
    source = _write(
        tmp_path / "upload", [["1", GTIN_A, "a"], ["2", GTIN_B, "b"], ["3", "2987653007361", "c"]]
    )
    control = tmp_path / "input" / "process" / "selection" / "selections.xlsx"
    archive(_config(control), source.read_bytes())

    # Act: two prunes, keys taken from the original grid both times, as the screen does it.
    original = read_sheet(_config(control))
    save_sheet(original.keeping({0, 1}))
    save_sheet(original.keeping({0}))

    # Assert
    # By modification time, not by name: a same-second pair is `{stamp}` and `{stamp}-1`, and
    # `-` sorts before `.`, so by name the *second* copy comes first.
    saved = sorted(
        (control.parent / "selection-*.xlsx").parent.glob("selection-*.xlsx"),
        key=lambda path: path.stat().st_mtime,
    )
    assert len(saved) == 2, "one dated file per save — and none for the upload nobody chose"
    assert read_sheet(_config(saved[-1])).rows == [["1", GTIN_A, "a"]], (
        "the newest dated selection is what was just saved, not what it replaced"
    )
    assert read_sheet(_config(saved[0])).rows == [["1", GTIN_A, "a"], ["2", GTIN_B, "b"]]
    assert read_sheet(_config(archive_path(control))).rows == [
        ["1", GTIN_A, "a"],
        ["2", GTIN_B, "b"],
        ["3", "2987653007361", "c"],
    ], "the upload is untouched by any save"
    assert len(read_sheet(_config(archive_path(control))).rows) == 3, (
        "the upload is still all three"
    )


# --- What the saved file is like to open --------------------------------------


def test_the_saved_header_is_frozen_and_filterable(tmp_path: Path) -> None:
    """The file goes back to a spreadsheet; the operator's next act is to sort or filter it."""
    # Arrange
    path = _write(tmp_path, [["1079", GTIN_A, "Drain saver"], ["1080", GTIN_B, "Airfryer basket"]])

    # Act
    save_sheet(read_sheet(_config(path)).keeping({0, 1}))

    # Assert
    worksheet = openpyxl.load_workbook(path).active
    assert worksheet.freeze_panes == "A2"
    assert worksheet.auto_filter.ref == "A1:C3"


# --- keeping every original ---------------------------------------------------


def test_every_upload_is_kept_dated_the_moment_it_lands(tmp_path: Path) -> None:
    """Archived on the way *in*, not when the next upload displaces it.

    Archiving on replacement only ever keeps "the one before the current one" — so a list uploaded
    once and never replaced, which is the ordinary case, would have no dated copy at all.
    """
    control = tmp_path / "process" / "selection" / "selections.xlsx"
    first = _write(tmp_path / "first", [["1", GTIN_A, "a"]])
    second = _write(tmp_path / "second", [["1", GTIN_A, "a"], ["2", GTIN_B, "b"]])

    archive(_config(control), first.read_bytes())
    archive(_config(control), second.read_bytes())

    kept = sorted(
        (control.parent.parent / "uploads").glob("product-list-*.xlsx"),
        key=lambda p: p.stat().st_mtime,
    )
    assert len(kept) == 2, "each upload is kept, not just the one before the current"
    assert read_sheet(_config(kept[0])).rows == [["1", GTIN_A, "a"]]


def test_the_current_upload_keeps_a_stable_name(tmp_path: Path) -> None:
    """Two things find it without guessing: Restore, and the per-run result sheet.

    Resolving it as "newest file in uploads/" would make any stray copy dropped into that folder
    silently become the operator's original — the failure mode the fixed names exist to avoid.
    """
    control = tmp_path / "process" / "selection" / "selections.xlsx"
    source = _write(tmp_path / "upload", [["1", GTIN_A, "a"]])

    kept = archive(_config(control), source.read_bytes())

    assert kept == control.parent.parent / "uploads" / "product-list.xlsx"
    assert kept.read_bytes() == source.read_bytes()


def test_restore_puts_the_upload_back_and_keeps_what_it_replaced(tmp_path: Path) -> None:
    """The way back from a selection gone wrong, without needing the original file to hand.

    It is the same act as re-uploading — the archive *is* the upload, byte for byte — minus
    finding it in somebody's sent items. Nothing is archived on the way out because there is
    nothing left to archive: every selection the operator saved is already dated.
    """
    control = tmp_path / "process" / "selection" / "selections.xlsx"
    source = _write(tmp_path / "upload", [["1", GTIN_A, "a"], ["2", GTIN_B, "b"]])
    archive(_config(control), source.read_bytes())
    save_sheet(read_sheet(_config(control)).keeping({0}))
    assert len(read_sheet(_config(control)).rows) == 1

    restored = restore_from_upload(_config(control))

    assert restored == 2
    assert read_sheet(_config(control)).rows == [["1", GTIN_A, "a"], ["2", GTIN_B, "b"]]
    assert list((control.parent / "selection-*.xlsx").parent.glob("selection-*.xlsx")), (
        "the selection that was saved before restoring is no longer dated anywhere"
    )


def test_restore_without_an_archive_refuses_rather_than_emptying_the_list(tmp_path: Path) -> None:
    """A client whose list was placed by hand has no upload to go back to.

    Writing *something* anyway — an empty file, or the control file over itself — would be the
    shape this project keeps designing against: a control that looks restored and plans nothing.
    """
    control = _write(tmp_path / "process" / "selection" / "selections", [["1", GTIN_A, "a"]])

    with pytest.raises(ProcessListError, match="no archived upload"):
        restore_from_upload(_config(control))

    assert read_sheet(_config(control)).rows == [["1", GTIN_A, "a"]], "the list is untouched"


def test_the_dated_copy_is_byte_identical_to_the_file_a_run_will_read(tmp_path: Path) -> None:
    """The contract the whole provenance mechanism rests on.

    A record of a save identifies the dated copy by hashing the **live** file, which only works
    because the copy is written *from those bytes* rather than saved a second time.

    openpyxl stamps the wall clock into each zip member, so two saves of one workbook agree only
    while they land inside the same second — measured, not assumed: a second apart they differ. A
    `workbook.save(kept)` here would therefore miss the lookup **intermittently**, which is worse
    than missing it always, and the symptom would be a selection that occasionally reads as one
    nobody saved. This assertion is why the copy is a copy; note that it can only catch a re-save
    when the two happen to straddle a second boundary, so treat it as pinning the invariant rather
    than as a trap for that one edit.
    """
    # Arrange
    control = _write(tmp_path / "process" / "selection", [["1079", GTIN_A], ["3086", GTIN_B]])

    # Act
    kept = save_sheet(read_sheet(_config(control)))

    # Assert
    assert kept.read_bytes() == control.read_bytes()
    assert kept != control
