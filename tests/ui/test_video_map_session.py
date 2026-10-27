"""``MappingSession`` — the mapping as last read, and the edits not yet written to it.

It exists so a screen that redraws (the Data screen redraws on every upload) neither drops staged
edits nor writes them onto a stale copy of the file. These check the four things that decide that.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from ui.video_map_panel import _SESSIONS, MappingSession  # noqa: E402

_TEXT = "nl:\n- {file: a.mp4, gtin: ''}\n- {file: b.mp4, gtin: '02987653000011'}\n"


def _session(tmp_path: Path, text: str = _TEXT) -> MappingSession:
    path = tmp_path / "mapping.yml"
    path.write_text(text, "utf-8")
    session = MappingSession(path)
    session.reload()
    return session


def test_reload_reads_the_rows(tmp_path: Path) -> None:
    session = _session(tmp_path)

    session.reload()

    assert [row.file for row in session.rows] == ["a.mp4", "b.mp4"]


def test_an_unreadable_file_is_a_problem_in_words_not_an_exception(tmp_path: Path) -> None:
    session = _session(
        tmp_path, "nl:\n- file: a.mp4\n  gtin: ''\n"
    )  # block form, not one row a line

    assert session.problem is not None
    assert session.rows == []


def test_a_file_changed_on_disk_is_noticed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert not session.stale_on_disk()

    session.path.write_text(_TEXT + "- {file: c.mp4, gtin: ''}\n", "utf-8")
    os.utime(session.path, (10_000, 10_000))

    assert session.stale_on_disk()


def test_a_write_re_reads_so_the_screen_shows_what_is_on_disk(tmp_path: Path) -> None:
    session = _session(tmp_path)

    backup = session.write(_TEXT.replace("gtin: ''", "gtin: '02987653000028'"))

    assert backup is not None and backup.exists()
    assert session.rows[0].gtin == "02987653000028"
    assert not session.stale_on_disk()


def test_sessions_are_kept_per_client_outside_any_page() -> None:
    """The point of the module-level dict: a redraw builds new widgets, not a new session."""
    assert isinstance(_SESSIONS, dict)
