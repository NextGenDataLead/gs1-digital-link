"""Where a run's own files live, and how to find them again.

A run produces three documents that describe it: the log of what it did to each row, the selection
it consumed, and the per-row result sheet. They used to live in three different shapes — a flat
``runs/{ts}.jsonl``, a file in ``input/`` that the run merely read, and a workbook written by hand
from another screen days later — and the consequence was that **nothing on disk could distinguish
an intention from a record**. The pair of files in ``input/`` sat there after the operator quit
mid-batch, looking exactly like a run that had happened.

So each run owns a directory, and the things that describe it are written *by it, into that
directory*. ``input/`` then means one thing only: what the **next** run will use. Quitting
mid-batch leaves an intention in ``input/`` and nothing in ``runs/``, which is precisely true.

**Both layouts are read.** The flat ``runs/{ts}.jsonl`` predates this and the operator has weeks of
them; a reader that saw only the new shape would report their history as empty, which is the
loudest possible way to look broken. Writing only ever uses the new shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The log's name inside a run directory. Fixed, so a directory is recognisable as a run by
#: containing one rather than by its name matching a timestamp format.
LOG_NAME: Final = "run.jsonl"

#: The selection the run actually consumed, copied in at the start. Not a reference to the file in
#: ``input/`` — that one is overwritten by the next batch, and a report that read it afterwards
#: would describe the wrong rows.
SELECTION_NAME: Final = "selection-used.xlsx"

#: The upload that selection was derived from, copied in beside it. Both, because the two answer
#: different questions: the selection says which rows ran, and only the upload can name the rows
#: the operator **dropped** — and reading that out of ``input/`` after the fact worked exactly
#: until the next batch replaced it.
UPLOAD_NAME: Final = "selection-uploaded.xlsx"

#: The per-row outcome workbook, written when the run finishes.
RESULT_NAME: Final = "result.xlsx"
#: What the operator ticked on the Data screen and why each could or could not run — copied from
#: beside the selection when it was saved with this selection (``lib.ticked_snapshot``).
TICKED_NAME: Final = "selection-ticked.json"

#: The data-quality note a live run leaves — the pages it published incomplete. See
#: :mod:`lib.run_quality`.
QUALITY_NAME: Final = "data-quality.md"

#: Where each record a links run wrote sends a scan, per language, against where it should — see
#: :mod:`lib.resolver_check`.
SCAN_CHECK_NAME: Final = "scan-check.json"

#: What this run **read**, against ``run.jsonl``'s what it did: the selection it consumed and the
#: export that selection was chosen against, each named and hashed. Written at the start, before
#: anything live, so a run that dies has still recorded what it touched.
#:
#: Not ``source.json``: that word belongs to ``.source.xlsx``, a spelling of "the uploaded list"
#: that nothing ever wrote and two scripts spent a week looking for.
SOURCES_NAME: Final = "inputs.json"


def runs_dir(client_id: str) -> Path:
    """Where every run of this client is recorded."""
    return Path("output") / client_id / "runs"


def run_dir(client_id: str, stamp: str) -> Path:
    """One run's own directory, named for the moment it started."""
    return runs_dir(client_id) / stamp


def log_path(client_id: str, stamp: str) -> Path:
    """Where a new run writes its log."""
    return run_dir(client_id, stamp) / LOG_NAME


def iter_logs(client_id: str) -> Iterator[Path]:
    """Every run log this client has, in both layouts, unordered.

    Callers sort by modification time rather than by name, and must keep doing so: two runs a
    second apart are ``{ts}`` and ``{ts}-1``, and ``-`` sorts *before* ``.`` — so by name the
    second of the two comes first.
    """
    directory = runs_dir(client_id)
    if not directory.is_dir():
        return
    yield from directory.glob(f"*/{LOG_NAME}")
    yield from directory.glob("*.jsonl")


def newest_log(client_id: str) -> Path | None:
    """The most recent run log by modification time, or ``None`` when there are none."""
    paths = sorted(iter_logs(client_id), key=lambda path: path.stat().st_mtime)
    return paths[-1] if paths else None


def stamp_of(log: Path) -> str:
    """The run name a log belongs to, whichever layout it is in.

    ``runs/20260925T101500/run.jsonl`` and ``runs/20260925T101500.jsonl`` are the same run under
    two spellings, and every screen that labels a run has to read them the same way.
    """
    return log.parent.name if log.name == LOG_NAME else log.stem


def sibling(log: Path, name: str) -> Path:
    """Where a run's other documents sit, given its log.

    For a legacy flat log there is no directory yet, so one is named after it — a result sheet
    asked for today about a run from last month still has somewhere to go that belongs to that run
    and not to the newest one.
    """
    return (log.parent if log.name == LOG_NAME else log.with_suffix("")) / name
