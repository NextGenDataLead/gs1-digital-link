"""What batch is in force, and whether the two files belong together.

The property under most of this is that **three answers are not two**: the ticks were chosen against
this export, against an earlier one, or nobody recorded it. Every batch saved before the ledger
existed is in the third state, so collapsing it into either of the other two would make the common
case the wrong answer.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.batch import Chosen, in_force
from lib.gates import Mode
from lib.input_layout import PROCESS_DIR, archive_path
from lib.provenance import (
    History,
    history_path,
    read,
    record_selection,
    record_upload,
)

_COLUMN = "Barcode"
GTIN_A = "02987653007359"
GTIN_B = "02987653007360"


@pytest.fixture
def client(tmp_path: Path) -> Path:
    root = tmp_path / "input" / "acme"
    (root / PROCESS_DIR / "selection").mkdir(parents=True)
    (root / PROCESS_DIR / "uploads" / "GS1 export").mkdir(parents=True)
    return root


def _list(path: Path, gtins: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Artikelnr.", _COLUMN])
    for index, gtin in enumerate(gtins):
        sheet.append([str(1000 + index), gtin])
    workbook.save(path)
    return path


def _export(client: Path, content: bytes = b"exported") -> Path:
    path = client / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx"
    path.write_bytes(content)
    return path


def _describe(client: Path, *, history: History | None = None, products: int | None = None):
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    export = client / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx"
    return in_force(
        export=export,
        selection=selection,
        product_list=archive_path(selection),
        history=history if history is not None else read(history_path(export)),
        gtin_column=_COLUMN,
        products=products,
    )


# --- Counting ----------------------------------------------------------------


def test_the_ticked_count_is_reported_against_the_count_that_was_sent(client: Path) -> None:
    """ "11 of 13" and "11" are different facts, and the second hides the two that were dropped."""
    # Arrange
    _export(client)
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    _list(selection, [GTIN_A])
    _list(archive_path(selection), [GTIN_A, GTIN_B])

    # Act
    batch = _describe(client, products=12)

    # Assert
    assert (batch.selection.rows, batch.listed) == (1, 2)
    assert batch.export.rows == 12
    assert batch.ready


def test_no_upload_to_compare_with_is_none_and_not_the_same_number_twice(client: Path) -> None:
    """Printing the ticked count as the listed count too would read as "nothing was dropped"."""
    _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])

    assert _describe(client).listed is None


# --- Readiness ---------------------------------------------------------------


def test_both_files_on_disk_is_enough_no_upload_this_session_required(client: Path) -> None:
    """The reversal. The grid used to appear only if both files arrived *this visit*, while a run
    consumed the selection either way — invisible and live at once.
    """
    _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])

    assert _describe(client).ready


@pytest.mark.parametrize("missing", ["export", "selection"])
def test_one_file_missing_is_not_a_batch(client: Path, missing: str) -> None:
    if missing != "export":
        _export(client)
    if missing != "selection":
        _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])

    batch = _describe(client)
    assert not batch.ready
    assert getattr(batch, missing).exists is False


def test_a_selection_that_will_not_read_is_present_but_not_ready(client: Path) -> None:
    """ "There is a file and it will not read" is a different problem from "there is no file"."""
    _export(client)
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    selection.write_bytes(b"not a workbook at all")

    batch = _describe(client)
    assert batch.selection.exists, "the file is there; the screen must not say it is missing"
    assert batch.selection.rows is None
    assert not batch.ready


# --- Which export the ticks were chosen against ------------------------------


def _save_against(client: Path, export: Path) -> Path:
    """Record a save of the live selection, chosen against ``export`` as it stands now."""
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    saved = client / PROCESS_DIR / "selection" / "selection-20260927T101600.xlsx"
    saved.write_bytes(selection.read_bytes())
    record_selection(
        history_path(export),
        saved,
        product_list=archive_path(selection),
        export=export,
    )
    return saved


def test_a_selection_saved_against_this_export_reads_as_agreeing(client: Path) -> None:
    export = _export(client)
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    _list(selection, [GTIN_A])
    dated = export.with_name("export-20260927T101500.xlsx")
    dated.write_bytes(export.read_bytes())
    record_upload(history_path(export), "export", kept=dated, given_name="GDSN.xlsx")
    _save_against(client, export)

    batch = _describe(client)

    assert batch.chosen_against is Chosen.THIS_EXPORT
    assert not batch.stale
    assert batch.export.given_name == "GDSN.xlsx"


def test_replacing_the_export_after_choosing_makes_the_batch_stale(client: Path) -> None:
    """The silent failure: a barcode the new export has no row for produces no page and no error."""
    # Arrange: choose against last quarter's export, then upload this quarter's over it.
    export = _export(client, b"last quarter")
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    _list(selection, [GTIN_A])
    old = export.with_name("export-20260620T090000.xlsx")
    old.write_bytes(export.read_bytes())
    record_upload(history_path(export), "export", kept=old, given_name="Q2.xlsx")
    _save_against(client, export)
    export.write_bytes(b"this quarter")

    # Act
    batch = _describe(client)

    # Assert
    assert batch.chosen_against is Chosen.EARLIER_EXPORT
    assert batch.stale
    assert batch.chosen_export == "export-20260620T090000.xlsx", (
        "the warning has to name which export, or the operator cannot tell what changed"
    )


def test_a_selection_nobody_recorded_makes_no_claim_either_way(client: Path) -> None:
    """Every batch saved before the ledger existed. Not agreement, and not staleness."""
    _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])

    batch = _describe(client)

    assert batch.chosen_against is Chosen.NOT_RECORDED
    assert batch.chosen_export is None
    assert not batch.stale, "an unrecorded batch must not be reported as a stale one"


def test_the_record_matched_is_this_selection_s_not_merely_the_latest_save(client: Path) -> None:
    """A selection restored from an older copy must not inherit a later save's answer.

    Matching on "the newest selection record" instead of on the live file's own hash would have this
    read as agreeing, because the *other* selection was chosen against the export on disk.
    """
    # Arrange: save A against an old export, then save B against the current one, then put A back.
    export = _export(client, b"last quarter")
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    _list(selection, [GTIN_A])
    old = export.with_name("export-20260620T090000.xlsx")
    old.write_bytes(export.read_bytes())
    record_upload(history_path(export), "export", kept=old, given_name="Q2.xlsx")
    _save_against(client, export)
    first = selection.read_bytes()

    export.write_bytes(b"this quarter")
    _list(selection, [GTIN_A, GTIN_B])
    _save_against(client, export)

    selection.write_bytes(first)  # restored from the earlier dated copy

    # Act / Assert
    assert _describe(client).chosen_against is Chosen.EARLIER_EXPORT


def test_unreadable_ledger_lines_are_surfaced_not_swallowed(client: Path) -> None:
    """A screen saying "not recorded" should be able to say a record may have been lost."""
    export = _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])
    path = history_path(export)
    assert path is not None
    path.write_text('{"v": 1, "what": "selec\n', encoding="utf-8")

    assert _describe(client).unreadable_records == 1


def test_a_client_outside_the_layout_still_describes_its_files(tmp_path: Path) -> None:
    """The pre-layout flat shape has no ledger.

    That is a missing record, not a missing batch.
    """
    export = tmp_path / "input" / "acme" / "products.xlsx"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"exported")
    selection = tmp_path / "input" / "acme" / "process-list.xlsx"
    _list(selection, [GTIN_A])

    batch = in_force(
        export=export,
        selection=selection,
        product_list=tmp_path / "nowhere.xlsx",
        history=read(history_path(export)),
        gtin_column=_COLUMN,
    )

    assert batch.ready
    assert batch.chosen_against is Chosen.NOT_RECORDED


# --- What the batch publishes ------------------------------------------------


def _save_with_mode(client: Path, export: Path, mode: str | None, stamp: str) -> None:
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    saved = client / PROCESS_DIR / "selection" / f"selection-{stamp}.xlsx"
    saved.write_bytes(selection.read_bytes())
    record_selection(
        history_path(export),
        saved,
        product_list=archive_path(selection),
        export=export,
        mode=mode,  # type: ignore[arg-type]
    )


def test_the_mode_saved_with_this_selection_is_the_batchs_mode(client: Path) -> None:
    export = _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])
    _save_with_mode(client, export, "links", "20261009T100000")

    assert _describe(client).mode is Mode.LINKS


def test_saving_the_same_ticks_again_with_another_mode_changes_it(client: Path) -> None:
    """The operator changes only the run type and presses Next: the newest save wins."""
    export = _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])
    _save_with_mode(client, export, "links", "20261009T100000")
    _save_with_mode(client, export, "both", "20261009T100500")

    assert _describe(client).mode is Mode.BOTH


def test_a_selection_saved_without_a_mode_has_none_never_a_default(client: Path) -> None:
    export = _export(client)
    _list(client / PROCESS_DIR / "selection" / "selections.xlsx", [GTIN_A])
    _save_with_mode(client, export, None, "20261009T100000")

    assert _describe(client).mode is None


def test_a_selection_replaced_outside_the_shell_does_not_inherit_a_mode(client: Path) -> None:
    """Matched on the live file's bytes, like the export agreement: a later file is another."""
    export = _export(client)
    selection = client / PROCESS_DIR / "selection" / "selections.xlsx"
    _list(selection, [GTIN_A])
    _save_with_mode(client, export, "links", "20261009T100000")
    _list(selection, [GTIN_A, GTIN_B])

    assert _describe(client).mode is None
