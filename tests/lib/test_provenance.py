"""The ledger: what a derived artefact records about where it came from.

Two properties carry the weight here, and both are about failing safely rather than about the happy
path. A record that could not be written must never fail the operator's save. And a record that is
*absent* must read as "not recorded", never as "there is no source" — those are different answers
and only one of them is safe to show somebody deciding whether to publish.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.input_layout import PROCESS_DIR
from lib.provenance import (
    HISTORY_NAME,
    VERSION,
    Recorded,
    SourceRef,
    append,
    describe,
    history_path,
    read,
    record_selection,
    record_upload,
    resolve,
    sha256_of,
)


@pytest.fixture
def client(tmp_path: Path) -> Path:
    root = tmp_path / "input" / "acme"
    (root / PROCESS_DIR / "selection").mkdir(parents=True)
    (root / PROCESS_DIR / "uploads" / "GS1 export").mkdir(parents=True)
    return root


def _export(client: Path) -> Path:
    return client / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx"


def _ledger(client: Path) -> Path:
    return client / PROCESS_DIR / HISTORY_NAME


def _entry(what: str, name: str, digest: str = "deadbeef") -> Recorded:
    return Recorded(
        what=what,  # type: ignore[arg-type]
        at=datetime(2026, 9, 27, 10, 15, tzinfo=UTC),
        of=SourceRef(name=name, sha256=digest, bytes=3),
    )


# --- Where it lives ----------------------------------------------------------


def test_the_ledger_sits_in_process_above_the_two_halves_it_records(client: Path) -> None:
    assert history_path(_export(client)) == client / PROCESS_DIR / HISTORY_NAME


def test_a_client_outside_the_layout_has_nowhere_to_record_and_that_is_not_an_error(
    tmp_path: Path,
) -> None:
    """One client is still on the pre-layout flat shape. Nothing about it may start raising."""
    assert history_path(tmp_path / "input" / "acme" / "products.xlsx") is None
    assert read(None) == ([], 0)
    assert append(None, _entry("export", "export-1.xlsx")) is False


# --- Reading tolerantly ------------------------------------------------------


def test_a_missing_ledger_is_an_empty_history_not_a_failure(client: Path) -> None:
    assert read(_ledger(client)) == ([], 0)


def test_a_truncated_final_line_is_counted_not_silently_dropped(client: Path) -> None:
    """What a process killed mid-append leaves. Dropping it reports one fewer save than happened."""
    # Arrange
    good = _entry("export", "export-20260927T101500.xlsx")
    append(_ledger(client), good)
    with _ledger(client).open("a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "what": "selec')

    # Act
    history = read(_ledger(client))

    # Assert
    assert [e.of.name for e in history.entries] == ["export-20260927T101500.xlsx"]
    assert history.unreadable == 1


def test_a_line_from_a_version_this_code_does_not_know_is_counted_not_half_read(
    client: Path,
) -> None:
    """The check that makes a schema change fail loudly.

    Pydantic ignores keys it did not expect, so a future line would validate, lose whatever moved,
    and come back looking complete. That is the failure mode a renamed persisted field produced once
    before, and it nearly put retracted products back on a live site.
    """
    # Arrange
    _ledger(client).write_text(
        json.dumps(
            {
                "v": VERSION + 1,
                "what": "selection",
                "at": "2026-09-27T10:16:00Z",
                "of": {"sha256": "abc", "bytes": 1},
                "chosen_from": {"export": {"sha256": "def", "bytes": 2}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Act
    history = read(_ledger(client))

    # Assert
    assert history.entries == []
    assert history.unreadable == 1


def test_a_line_that_is_not_an_object_at_all_is_counted(client: Path) -> None:
    _ledger(client).write_text('["not", "a", "record"]\nnot json either\n\n', encoding="utf-8")

    assert read(_ledger(client)) == ([], 2), "the blank line is not a failure; the other two are"


def test_the_newest_record_is_by_position_because_the_file_is_append_only(client: Path) -> None:
    """A clock that stepped backwards must not be able to reorder what happened."""
    # Arrange: the second line carries the *earlier* timestamp.
    append(_ledger(client), _entry("export", "first.xlsx"))
    append(
        _ledger(client),
        Recorded(
            what="export",
            at=datetime(2020, 1, 1, tzinfo=UTC),
            of=SourceRef(name="second.xlsx", sha256="cafe", bytes=3),
        ),
    )

    # Act / Assert
    newest = read(_ledger(client)).newest("export")
    assert newest is not None
    assert newest.of.name == "second.xlsx"


def test_newest_ignores_the_other_kinds_of_document(client: Path) -> None:
    append(_ledger(client), _entry("export", "export-1.xlsx"))
    append(_ledger(client), _entry("selection", "selection-1.xlsx"))

    history = read(_ledger(client))
    found = history.newest("export")
    assert found is not None and found.of.name == "export-1.xlsx"
    assert history.newest("product-list") is None


# --- Identifying a file ------------------------------------------------------


def test_a_file_is_described_by_its_bytes(client: Path) -> None:
    path = _export(client)
    path.write_bytes(b"exported")

    ref = describe(path, name=path.name, given_name="GDSN export.xlsx", rows=12)

    assert ref is not None
    assert ref.sha256 == sha256_of(path)
    assert (ref.name, ref.given_name, ref.bytes, ref.rows) == (
        "export.xlsx",
        "GDSN export.xlsx",
        8,
        12,
    )


def test_describing_a_file_that_is_not_there_is_none_not_a_crash(client: Path) -> None:
    assert describe(_export(client)) is None
    assert sha256_of(_export(client)) is None


def test_the_ledger_supplies_the_name_and_the_hash_only_checks_it(client: Path) -> None:
    """Name is the reference; the hash is the check. A hash used as the key makes renames silent."""
    # Arrange: the live export, and a record of the dated copy of those same bytes.
    live = _export(client)
    live.write_bytes(b"exported")
    dated = live.with_name("export-20260927T101500.xlsx")
    dated.write_bytes(b"exported")
    record_upload(
        _ledger(client), "export", kept=dated, given_name="GDSN_export_2026-09-27.xlsx", rows=12
    )

    # Act
    ref = resolve(live, read(_ledger(client)), "export")

    # Assert
    assert ref is not None
    assert ref.name == "export-20260927T101500.xlsx", "the archive a later reader can still open"
    assert ref.given_name == "GDSN_export_2026-09-27.xlsx"


def test_a_file_no_record_names_is_still_identified_by_its_hash(client: Path) -> None:
    """A hand-placed export.

    ``name=None`` is the fact, not an error — and emphatically not "there is no export".
    """
    live = _export(client)
    live.write_bytes(b"placed by hand")

    ref = resolve(live, read(_ledger(client)), "export")

    assert ref is not None, "collapsing 'nobody wrote this down' into None loses a real file"
    assert ref.name is None
    assert ref.sha256 == sha256_of(live)


def test_a_record_of_different_bytes_does_not_name_the_live_file(client: Path) -> None:
    """The check doing its job: the export was replaced outside the shell."""
    live = _export(client)
    live.write_bytes(b"this quarter")
    stale = live.with_name("export-20260620T090000.xlsx")
    stale.write_bytes(b"last quarter")
    record_upload(_ledger(client), "export", kept=stale, given_name="old.xlsx")

    ref = resolve(live, read(_ledger(client)), "export")

    assert ref is not None
    assert ref.name is None


def test_a_re_upload_of_bytes_already_seen_resolves_to_the_newest_copy(client: Path) -> None:
    live = _export(client)
    live.write_bytes(b"exported")
    for stamp in ("20260620T090000", "20260927T101500"):
        dated = live.with_name(f"export-{stamp}.xlsx")
        dated.write_bytes(b"exported")
        record_upload(_ledger(client), "export", kept=dated, given_name=f"sent-{stamp}.xlsx")

    ref = resolve(live, read(_ledger(client)), "export")

    assert ref is not None and ref.name == "export-20260927T101500.xlsx"


# --- Recording a save --------------------------------------------------------


def test_a_save_records_the_export_and_the_list_it_was_chosen_from(client: Path) -> None:
    # Arrange
    export = _export(client)
    export.write_bytes(b"exported")
    dated_export = export.with_name("export-20260927T101500.xlsx")
    dated_export.write_bytes(b"exported")
    record_upload(_ledger(client), "export", kept=dated_export, given_name="GDSN.xlsx", rows=12)

    sent = client / PROCESS_DIR / "uploads" / "product-list.xlsx"
    sent.write_bytes(b"the list as sent")
    dated_list = client / PROCESS_DIR / "uploads" / "product-list-20260927T101530.xlsx"
    dated_list.write_bytes(b"the list as sent")
    record_upload(
        _ledger(client), "product-list", kept=dated_list, given_name="week 39.xlsx", rows=13
    )

    saved = client / PROCESS_DIR / "selection" / "selection-20260927T101600.xlsx"
    saved.write_bytes(b"the ticked list")

    # Act
    assert record_selection(_ledger(client), saved, product_list=sent, export=export, rows=11)

    # Assert
    entry = read(_ledger(client)).newest("selection")
    assert entry is not None
    assert entry.of.name == "selection-20260927T101600.xlsx"
    assert entry.of.rows == 11
    assert entry.sources["export"].name == "export-20260927T101500.xlsx"
    assert entry.sources["export"].given_name == "GDSN.xlsx"
    assert entry.sources["product-list"].name == "product-list-20260927T101530.xlsx"


def test_a_save_against_a_hand_placed_export_records_it_unnamed_rather_than_not_at_all(
    client: Path,
) -> None:
    """The distinction the panel reads: an export is there, but nothing recorded which one."""
    export = _export(client)
    export.write_bytes(b"placed by hand")
    saved = client / PROCESS_DIR / "selection" / "selection-20260927T101600.xlsx"
    saved.write_bytes(b"the ticked list")

    record_selection(
        _ledger(client), saved, product_list=export.with_name("absent.xlsx"), export=export
    )

    entry = read(_ledger(client)).newest("selection")
    assert entry is not None
    assert entry.sources["export"].name is None
    assert "product-list" not in entry.sources, "a list that is not on disk is not a source"


# --- Nothing here may fail an operator's action -------------------------------


def test_a_ledger_that_cannot_be_written_reports_false_and_does_not_raise(client: Path) -> None:
    """A save that succeeded must not be reported as failed because a note about it could not
    be filed.
    """
    # Arrange: a directory where the ledger should be, so every append is an OSError.
    _ledger(client).mkdir()

    # Act / Assert
    assert append(_ledger(client), _entry("export", "export-1.xlsx")) is False
    assert read(_ledger(client)) == ([], 0)


def test_recording_an_upload_that_was_never_archived_is_false_not_an_exception(
    client: Path,
) -> None:
    """``_keep_upload`` returns None when the dated copy could not be written."""
    assert record_upload(_ledger(client), "export", kept=None, given_name="x.xlsx") is False
    assert (
        record_upload(_ledger(client), "export", kept=_export(client), given_name="x.xlsx") is False
    ), "a kept path that is not on disk records nothing"


def test_the_recorded_row_count_comes_back_with_the_name(client: Path) -> None:
    """So a caller that only wants to *name* a file need not parse a workbook to size it.

    Without this, ``inputs.json`` recorded ``rows: null`` for every document and the Runs screen
    could name the export a run used but not say how many rows its selection held.
    """
    live = _export(client)
    live.write_bytes(b"exported")
    dated = live.with_name("export-20260927T101500.xlsx")
    dated.write_bytes(b"exported")
    record_upload(_ledger(client), "export", kept=dated, given_name="GDSN.xlsx", rows=12)

    assert resolve(live, read(_ledger(client)), "export").rows == 12  # type: ignore[union-attr]


def test_a_count_the_caller_supplies_wins_over_the_recorded_one(client: Path) -> None:
    """The caller counted the file in front of it; the record describes bytes that matched."""
    live = _export(client)
    live.write_bytes(b"exported")
    dated = live.with_name("export-20260927T101500.xlsx")
    dated.write_bytes(b"exported")
    record_upload(_ledger(client), "export", kept=dated, given_name="GDSN.xlsx", rows=12)

    assert resolve(live, read(_ledger(client)), "export", rows=99).rows == 99  # type: ignore[union-attr]


def test_a_selection_records_the_mode_it_was_saved_with(client: Path) -> None:
    export = _export(client)
    saved = client / PROCESS_DIR / "selection" / "selection-20261009T101600.xlsx"
    saved.write_bytes(b"the ticked list")

    record_selection(
        _ledger(client),
        saved,
        product_list=export.with_name("absent.xlsx"),
        export=export,
        mode="links",
    )

    entry = read(_ledger(client)).newest("selection")
    assert entry is not None
    assert entry.mode == "links"


def test_a_version_1_line_still_reads_and_has_no_mode(client: Path) -> None:
    """Every ledger written before the mode moved to the Data screen is version 1."""
    _ledger(client).write_text(
        json.dumps(
            {
                "v": 1,
                "what": "selection",
                "at": "2026-09-27T10:16:00Z",
                "of": {"sha256": "abc", "bytes": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    history = read(_ledger(client))

    assert history.unreadable == 0
    assert history.entries[0].mode is None
