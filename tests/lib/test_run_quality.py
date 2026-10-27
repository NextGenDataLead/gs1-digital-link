"""Unit tests for lib/run_quality.py — the note a live run leaves about pages without a video."""

from __future__ import annotations

from datetime import UTC, datetime

from lib.records import RunOutcome
from lib.run_quality import render, without_video

_TS = datetime(2026, 10, 6, tzinfo=UTC)


def _outcome(language: str, status: str, video_file: str | None) -> RunOutcome:
    return RunOutcome(
        gtin="02987653000011",
        language=language,
        ts=_TS,
        status=status,
        wp_url=f"https://wp.test/{language}/p/",
        video_file=video_file,
    )


def test_only_written_pages_with_a_recorded_empty_video_count() -> None:
    """A failed row published nothing; ``None`` is a client that attaches no videos at all."""
    outcomes = [
        _outcome("nl", "ok", ""),
        _outcome("fr", "ok", "a.mp4"),
        _outcome("de", "error", ""),
        _outcome("en", "ok", None),
    ]

    assert [o.language for o in without_video(outcomes)] == ["nl"]


def test_the_note_names_each_bare_page_and_counts_against_pages_written() -> None:
    text = render("20261006T120000Z", [_outcome("nl", "ok", ""), _outcome("fr", "ok", "a.mp4")])

    assert text.startswith("# Data quality — run 20261006T120000Z")
    assert "1 of the 2 page(s) this run wrote went live with no video" in text
    assert "| 02987653000011 | nl | https://wp.test/nl/p/ |" in text
    assert "| fr |" not in text


def test_a_run_with_every_video_says_so_rather_than_writing_an_empty_table() -> None:
    text = render("s", [_outcome("nl", "ok", "a.mp4")])

    assert "Every page this run wrote carries a video (1 page(s))." in text
    assert "|" not in text


def test_a_file_that_would_not_prepare_is_listed_as_the_operators_job() -> None:
    failed = _outcome("nl", "ok", "a.mp4").model_copy(update={"video_failed": "b.mp4"})

    text = render("s", [failed])

    assert "## A confirmed video could not be prepared" in text
    assert "| 02987653000011 | nl | b.mp4 |" in text
