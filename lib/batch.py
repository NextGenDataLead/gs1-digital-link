"""What batch is in force: which two files a run would use, and whether they belong together.

A batch is the GS1 export, the product list, and the ticks the operator put on it. Every screen
needs to say which ones are in force — the selection can be adjusted again days later, in a later
session — and until now none of them could. The Data screen held three booleans in memory for the
life of the process and showed nothing at all until both files had arrived *that visit*, while
admitting on screen that "the selection is still on disk and a run would use it". A batch that is
invisible and live at the same time is the worst of the two states.

**The files on disk are the batch.** Nothing here decides what a run will use, and there is no
persisted record of "the current batch" to drift out of step with them — that second source of truth
is the failure this project has already had once, where a renamed persisted field nearly put
retracted products back on a live site. This module *reads*: two paths in, one description out.

The one thing it cannot read off the files is whether they belong together. A selection carries no
trace of the export it was chosen against, so that comes from the ledger
(:mod:`lib.provenance`) — and where the ledger is silent, :data:`Chosen.NOT_RECORDED` says so
rather than guessing. Every file that predates the ledger is in that state, which is exactly why
guessing would be wrong: the common case would be the wrong answer.

Takes **paths, never a config**, for the reason :mod:`lib.provenance` gives at length: configured
paths are relative and this module cannot know what to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple

from lib.errors import ProcessListError
from lib.gates import Mode
from lib.process_list import ProcessListSheet, read_process_list
from lib.provenance import History, SourceRef, resolve, sha256_of


class Chosen(StrEnum):
    """Whether the selection in force was made against the export in force.

    ``NOT_RECORDED`` is a third answer and not a soft "no". It is what every batch saved before the
    ledger existed reads as, and treating it as agreement or as disagreement would both be a claim
    nobody made.
    """

    THIS_EXPORT = "this export"
    EARLIER_EXPORT = "an earlier export"
    NOT_RECORDED = "not recorded"


@dataclass(frozen=True)
class Document:
    """One of the batch's files, as it is on disk now.

    ``given_name`` is what the operator called it when they sent it, which the upload discards from
    the filesystem — so this is the only place it survives, and the only answer to "which export?"
    that a person recognises. ``None`` for a file that arrived some other way.
    """

    path: Path
    modified: datetime | None
    given_name: str | None = None
    archived_name: str | None = None
    rows: int | None = None

    @property
    def exists(self) -> bool:
        return self.modified is not None


@dataclass(frozen=True)
class Batch:
    """The two files a run would read, and how they relate.

    ``listed`` is the row count of the *upload* the selection was pruned from, so a screen can say
    "11 of 13 ticked" — the count the operator chose against, not just the count that survives.
    ``None`` when no upload is on disk to compare with, which is not the same as zero.
    """

    export: Document
    selection: Document
    listed: int | None
    chosen_against: Chosen
    #: The archived export these ticks were made against, when it is not the one on disk.
    chosen_export: str | None
    #: Ledger lines that could not be read. Surfaced rather than swallowed: a truncated tail means
    #: a record is missing, and a screen claiming "not recorded" should be able to say why.
    unreadable_records: int
    #: What the operator chose to publish with *this* selection, from the record of its save.
    #: ``None`` when no save of these exact bytes recorded one — a selection saved before the
    #: choice moved to the Data screen, or replaced outside the shell. Never defaulted: guessing
    #: "pages" for a batch somebody meant as links is a guess with a permanent other half.
    mode: Mode | None = None

    @property
    def ready(self) -> bool:
        """Both files are on disk and read, so a selection can be shown and edited."""
        return self.export.exists and self.selection.exists and self.selection.rows is not None

    @property
    def stale(self) -> bool:
        """These ticks were made against an export that is no longer the one on disk."""
        return self.chosen_against is Chosen.EARLIER_EXPORT


def in_force(  # noqa: PLR0913 — six named paths and counts read better than a context object
    *,
    export: Path,
    selection: Path,
    product_list: Path,
    history: History,
    gtin_column: str,
    products: int | None = None,
) -> Batch:
    """Describe the batch a run would use right now.

    Args:
        export: The live GS1 export, resolved.
        selection: The live ticked list, resolved.
        product_list: The archived upload that selection was pruned from, resolved.
        history: The client's ledger, already read.
        gtin_column: From ``process_list.gtin_column`` — the sheets are read with the run's reader.
        products: How many products the export parsed to, which lives in ``products.json`` and is
            the caller's to supply. Counted there rather than here because a second opinion about
            how many products an export holds is a second opinion about the plan's size.

    Returns:
        A :class:`Batch`. Never raises: an unreadable workbook leaves ``rows`` as ``None`` and the
        document still reports as present, because "there is a file and it will not read" is a
        different problem from "there is no file" and the screens say different things about them.
    """
    export_ref = resolve(export, history, "export")
    agreement = _agreement(export, selection, history)
    return Batch(
        export=Document(
            path=export,
            modified=_modified(export),
            given_name=export_ref.given_name if export_ref else None,
            archived_name=export_ref.name if export_ref else None,
            rows=products,
        ),
        selection=Document(
            path=selection,
            modified=_modified(selection),
            rows=_rows(selection, gtin_column),
        ),
        listed=_rows(product_list, gtin_column),
        chosen_against=agreement.verdict,
        chosen_export=agreement.export_name,
        unreadable_records=history.unreadable,
        mode=agreement.mode,
    )


class _Agreement(NamedTuple):
    verdict: Chosen
    export_name: str | None
    mode: Mode | None = None


_UNRECORDED: Final = _Agreement(Chosen.NOT_RECORDED, None)


def _agreement(export: Path, selection: Path, history: History) -> _Agreement:
    """Which export these ticks were made against, from the record of the save that made them.

    Matched on the live selection's own hash, so it is *this* selection's record and not merely the
    most recent save — a selection restored from an older dated copy, or replaced by re-uploading
    the list, must not inherit a later save's answer.
    """
    digest = sha256_of(selection)
    if digest is None:
        return _UNRECORDED

    saved = next(
        (e for e in reversed(history.entries) if e.what == "selection" and e.of.sha256 == digest),
        None,
    )
    if saved is None:
        return _UNRECORDED
    mode = Mode(saved.mode) if saved.mode is not None else None
    against: SourceRef | None = saved.sources.get("export")
    if against is None:
        return _Agreement(Chosen.NOT_RECORDED, None, mode)
    if against.sha256 == sha256_of(export):
        return _Agreement(Chosen.THIS_EXPORT, against.name, mode)
    return _Agreement(Chosen.EARLIER_EXPORT, against.name, mode)


def _modified(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _rows(path: Path, gtin_column: str) -> int | None:
    """How many data rows this list carries, or ``None`` if it will not read."""
    sheet = _sheet(path, gtin_column)
    return None if sheet is None else len(sheet.rows)


def _sheet(path: Path, gtin_column: str) -> ProcessListSheet | None:
    from lib.config import ProcessListConfig  # noqa: PLC0415 — lib.config imports widely

    if not path.is_file():
        return None
    try:
        return read_process_list(ProcessListConfig(path=str(path), gtin_column=gtin_column))
    except ProcessListError:
        return None
