"""Where a run's own documents live, across both layouts.

This module had no tests either, and it carries two traps that a wrong answer hides rather than
raises: runs are ordered by **modification time and never by name**, and a legacy flat log has no
directory of its own, so one has to be named after it. Both were load-bearing before anything
asserted them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.run_files import (
    LOG_NAME,
    RESULT_NAME,
    SELECTION_NAME,
    UPLOAD_NAME,
    iter_logs,
    log_path,
    newest_log,
    run_dir,
    runs_dir,
    sibling,
    stamp_of,
)


@pytest.fixture(autouse=True)
def _in_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``runs_dir`` builds a bare ``Path("output")``, so these are all CWD-relative."""
    monkeypatch.chdir(tmp_path)


def _log(stamp: str, *, flat: bool = False, mtime: float | None = None) -> Path:
    path = runs_dir("acme") / (f"{stamp}.jsonl" if flat else f"{stamp}/{LOG_NAME}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# --- Naming ------------------------------------------------------------------


def test_a_new_run_writes_into_a_directory_of_its_own() -> None:
    assert log_path("acme", "20260927T101500Z") == Path(
        "output/acme/runs/20260927T101500Z/run.jsonl"
    )
    assert run_dir("acme", "20260927T101500Z") == Path("output/acme/runs/20260927T101500Z")


def test_the_four_documents_a_run_owns_have_fixed_names() -> None:
    """Fixed, so a directory is a run by *containing a log* rather than by its name looking
    like a timestamp.
    """
    assert (LOG_NAME, SELECTION_NAME, UPLOAD_NAME, RESULT_NAME) == (
        "run.jsonl",
        "selection-used.xlsx",
        "selection-uploaded.xlsx",
        "result.xlsx",
    )


# --- Both layouts ------------------------------------------------------------


def test_both_layouts_are_read_because_the_operator_has_weeks_of_the_old_one() -> None:
    """A reader that saw only the new shape would report their history as empty."""
    new, flat = _log("20260927T101500Z"), _log("20260819T234001Z", flat=True)

    assert set(iter_logs("acme")) == {new, flat}


def test_the_same_run_under_two_spellings_comes_out_as_one_name() -> None:
    assert stamp_of(Path("output/acme/runs/20260925T101500/run.jsonl")) == "20260925T101500"
    assert stamp_of(Path("output/acme/runs/20260925T101500.jsonl")) == "20260925T101500"


def test_a_clients_first_visit_has_no_runs_rather_than_an_error() -> None:
    assert list(iter_logs("acme")) == []
    assert newest_log("acme") is None


# --- Ordering ----------------------------------------------------------------


def test_the_newest_run_is_by_mtime_because_by_name_a_same_second_pair_inverts() -> None:
    """``-`` precedes ``.``, so ``{ts}-1.jsonl`` sorts *before* ``{ts}.jsonl``.

    Two runs a second apart is what a re-run after a failure looks like, and reporting on the wrong
    one of the pair is invisible until somebody opens a page URL.
    """
    # Arrange: the -1 log is the later write, and sorts first by name.
    first = _log("20260927T101500Z", flat=True, mtime=1_000_000)
    second = _log("20260927T101500Z-1", flat=True, mtime=2_000_000)
    assert sorted([first.name, second.name])[0] == second.name, "the trap itself"

    # Act / Assert
    assert newest_log("acme") == second


def test_the_newest_run_compares_across_both_layouts() -> None:
    _log("20260819T234001Z", flat=True, mtime=1_000_000)
    newer = _log("20260927T101500Z", mtime=2_000_000)

    assert newest_log("acme") == newer


# --- A run's other documents -------------------------------------------------


def test_a_runs_documents_sit_beside_its_log() -> None:
    log = _log("20260927T101500Z")

    assert sibling(log, RESULT_NAME) == log.parent / RESULT_NAME


def test_a_legacy_flat_log_gets_a_directory_named_after_it() -> None:
    """So a sheet asked for today about last month's run belongs to *that* run, not the newest."""
    log = _log("20260819T234001Z", flat=True)

    assert sibling(log, RESULT_NAME) == runs_dir("acme") / "20260819T234001Z" / RESULT_NAME
    assert sibling(log, SELECTION_NAME).parent == sibling(log, RESULT_NAME).parent


def test_where_runs_live_is_under_output_never_under_input() -> None:
    """The invariant the whole layout rests on: a run reads ``input/`` and writes ``output/``."""
    assert runs_dir("acme") == Path("output/acme/runs")
