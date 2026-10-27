"""Unit tests for lib/video_signoff_archive.py — keeping the sheets and the column choice."""

from __future__ import annotations

import os
from pathlib import Path

from lib.input_layout import signoff_archives, signoff_dir, signoff_index, signoff_path
from lib.video_signoff_archive import (
    FILE_GONE,
    NO_RECORD,
    NO_SHEET,
    UNREADABLE_INDEX,
    Absent,
    Signoff,
    archive,
    describe,
    newest,
    record,
)

_COLUMNS = {"language": 0, "file": 1, "gtin": 2}
_HEADERS = {"language": "language", "file": "file", "gtin": "current_gtin"}


def _map(tmp_path: Path) -> Path:
    path = tmp_path / "videos" / "mapping.yml"
    path.parent.mkdir(parents=True)
    path.write_text("nl: []\n", "utf-8")
    return path


def _record(video_map: Path, sheet: Path) -> str | None:
    return record(
        video_map,
        sheet=sheet,
        given_name="Videos v3.xlsx",
        at="2026-10-06T10:15:00",
        rows=173,
        columns=_COLUMNS,
        headers=_HEADERS,
    )


def _age(path: Path, seconds: int) -> None:
    os.utime(path, (seconds, seconds))


def test_the_sheets_live_beside_the_mapping_not_under_process(tmp_path: Path) -> None:
    video_map = tmp_path / "input" / "c" / "videos" / "mapping.yml"

    assert signoff_dir(video_map) == tmp_path / "input" / "c" / "videos" / "signoff"
    assert signoff_index(video_map).name == "signoff.json"
    assert signoff_path(video_map, "20261006-101500").name == "signoff-20261006-101500.xlsx"
    assert "process" not in signoff_dir(video_map).parts


def test_a_path_helper_creates_nothing(tmp_path: Path) -> None:
    video_map = tmp_path / "videos" / "mapping.yml"

    signoff_path(video_map, "x")
    signoff_archives(video_map)

    assert not (tmp_path / "videos").exists()


def test_two_uploads_in_one_second_are_both_kept(tmp_path: Path) -> None:
    video_map = _map(tmp_path)

    first = archive(video_map, b"one", stamp="20261006-101500")
    second = archive(video_map, b"two", stamp="20261006-101500")

    assert first != second
    assert {first.read_bytes(), second.read_bytes()} == {b"one", b"two"}


def test_nothing_uploaded_is_no_sheet(tmp_path: Path) -> None:
    assert newest(_map(tmp_path)) == Absent(NO_SHEET)


def test_an_upload_whose_columns_were_never_chosen_is_no_record(tmp_path: Path) -> None:
    video_map = _map(tmp_path)
    sheet = archive(video_map, b"x", stamp="a")

    assert newest(video_map) == Absent(NO_RECORD, sheet)


def test_a_recorded_choice_comes_back_with_its_sheet(tmp_path: Path) -> None:
    video_map = _map(tmp_path)
    sheet = archive(video_map, b"x", stamp="a")

    assert _record(video_map, sheet) is None
    found = newest(video_map)

    assert isinstance(found, Signoff)
    assert found.sheet == sheet
    assert found.record.columns == _COLUMNS
    assert found.record.headers["gtin"] == "current_gtin"
    assert found.record.given_name == "Videos v3.xlsx"


def test_a_newer_sheet_without_a_choice_is_not_answered_with_the_older_one(
    tmp_path: Path,
) -> None:
    """Re-planning the older sheet while calling it the client's latest word is the quiet wrong
    answer. Ordered by modification time, never by name."""
    video_map = _map(tmp_path)
    older = archive(video_map, b"x", stamp="b")
    _record(video_map, older)
    newer = archive(video_map, b"y", stamp="a")  # sorts *before* the older one by name
    _age(older, 1_000)
    _age(newer, 2_000)

    assert newest(video_map) == Absent(NO_RECORD, newer)


def test_a_choice_for_a_sheet_that_is_gone_says_so(tmp_path: Path) -> None:
    video_map = _map(tmp_path)
    sheet = archive(video_map, b"x", stamp="a")
    _record(video_map, sheet)
    sheet.unlink()

    assert newest(video_map).reason == FILE_GONE  # type: ignore[union-attr]


def test_an_unreadable_note_says_so(tmp_path: Path) -> None:
    video_map = _map(tmp_path)
    sheet = archive(video_map, b"x", stamp="a")
    signoff_index(video_map).write_text("{not json", "utf-8")

    assert newest(video_map) == Absent(UNREADABLE_INDEX, sheet)


def test_recording_never_raises(tmp_path: Path) -> None:
    """The sheet is already archived; a note that cannot be filed is said, not thrown."""
    video_map = tmp_path / "nowhere" / "mapping.yml"  # no signoff/ folder to write into

    problem = _record(video_map, tmp_path / "signoff-a.xlsx")

    assert problem is not None and "could not be written" in problem


def test_each_absence_is_its_own_sentence_and_none_is_a_warning() -> None:
    sentences = {
        describe(Absent(reason, Path("signoff-a.xlsx")))
        for reason in (NO_SHEET, NO_RECORD, FILE_GONE, UNREADABLE_INDEX)
    }

    assert len(sentences) == 4
    assert not any("⚠" in s or "warning" in s.lower() for s in sentences)
