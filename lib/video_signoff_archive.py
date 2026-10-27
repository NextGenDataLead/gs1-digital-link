"""Keep the client's sign-off sheets, and remember which column of the newest one is which.

The sheet used to be read and not kept. That made sense while nobody had decided where operator
inputs live, and it stopped making sense once the data-quality report needed to say what the sheet
*still* changes: re-reading it on every render is what makes that answer correct after a fill, an
edit by hand, or a new sheet — and there was nothing to re-read.

**Which column is which is the operator's answer, and it is kept.** A sheet's headings are the
client's, the guess about them has already been wrong once (``current_gtin``), and the report runs
with nobody there to choose. So the choice made where the sheet is uploaded is recorded with the
headings it was made against. If the sheet is edited in place and a column moves, the headings no
longer match and the report says so — rather than re-planning against the wrong column, which
would read a filename as a barcode.

**One index per folder, not a note per sheet.** ``signoff.json`` describes the newest sheet only.
A ``.json`` beside every sheet doubles the files in a folder people open in Finder; the filename
cannot carry heading text; and ``history.jsonl`` belongs to the export's tree, with a closed set of
document kinds that four screens read.

Paths in, never a config — so this is usable from a script and a screen alike, and testable with a
temporary folder. Nothing here deletes anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

from pydantic import BaseModel, ConfigDict, ValidationError

from lib.input_layout import signoff_archives, signoff_dir, signoff_index, signoff_path

#: No sheet has ever been uploaded. The ordinary state before the client replies.
NO_SHEET: Final = "no-sheet"
#: The newest sheet arrived, and nobody has said which column is which for it yet.
NO_RECORD: Final = "no-record"
#: The note names a sheet that is no longer in the folder.
FILE_GONE: Final = "file-gone"
#: The note is there and cannot be read.
UNREADABLE_INDEX: Final = "unreadable-index"


class SignoffRecord(BaseModel):
    """What ``signoff.json`` holds about the newest sheet.

    Attributes:
        file: The archived sheet's name, inside ``signoff/``.
        at: When it arrived, ISO 8601.
        given_name: What it was called when it was uploaded — the client's name for it.
        rows: How many data rows it had.
        columns: ``{"language": i, "file": j, "gtin": k}`` — the operator's choice.
        headers: The heading text in each of those columns when the choice was made.
    """

    model_config = ConfigDict(frozen=True)

    file: str
    at: str
    given_name: str
    rows: int
    columns: dict[str, int]
    headers: dict[str, str]


class Signoff(NamedTuple):
    """The newest sheet, and the choice recorded for it."""

    sheet: Path
    record: SignoffRecord


class Absent(NamedTuple):
    """Why there is no sheet to re-plan. ``sheet`` is the newest one, when there is one."""

    reason: str
    sheet: Path | None = None


def archive(video_map: Path, data: bytes, *, stamp: str) -> Path:
    """Keep the uploaded bytes as ``signoff/signoff-{stamp}.xlsx`` and return where.

    Raises ``OSError`` if it cannot be written: an upload that was not kept must say so.
    """
    signoff_dir(video_map).mkdir(parents=True, exist_ok=True)
    path = signoff_path(video_map, stamp)
    path.write_bytes(data)
    return path


def record(  # noqa: PLR0913 — one argument per fact the note keeps
    video_map: Path,
    *,
    sheet: Path,
    given_name: str,
    at: str,
    rows: int,
    columns: Mapping[str, int],
    headers: Mapping[str, str],
) -> str | None:
    """Note which column of ``sheet`` is which. Returns a problem in words, or ``None``.

    **Never raises.** The sheet is already archived by the time this runs; reporting the upload as
    failed because the note about it could not be filed would be the wrong failure, so a problem
    comes back as text for the caller to show beside a success.
    """
    note = SignoffRecord(
        file=sheet.name,
        at=at,
        given_name=given_name,
        rows=rows,
        columns=dict(columns),
        headers=dict(headers),
    )
    try:
        signoff_index(video_map).write_text(note.model_dump_json(indent=2) + "\n", "utf-8")
    except OSError as exc:
        return f"the note of which column is which could not be written ({exc})"
    return None


def newest(video_map: Path) -> Signoff | Absent:
    """The newest archived sheet with its recorded columns — or why there is none to use.

    Newest **by modification time**, never by name: :func:`lib.input_layout.unique` explains why a
    same-second pair sorts the wrong way round by name.
    """
    sheets = signoff_archives(video_map)
    latest = sheets[0] if sheets else None
    index = signoff_index(video_map)
    if not index.exists():
        return Absent(NO_SHEET if latest is None else NO_RECORD, latest)
    try:
        note = SignoffRecord.model_validate(json.loads(index.read_text("utf-8")))
    except (OSError, ValueError, ValidationError):
        return Absent(UNREADABLE_INDEX, latest)
    sheet = signoff_dir(video_map) / note.file
    if not sheet.is_file():
        return Absent(FILE_GONE, latest)
    if latest is not None and latest != sheet:
        # A newer sheet arrived and its columns were never chosen. Re-planning the older one while
        # calling it the client's latest word would be the quiet wrong answer.
        return Absent(NO_RECORD, latest)
    return Signoff(sheet, note)


def describe(absent: Absent) -> str:
    """One sentence for the report. Each reason is a different sentence because each is a
    different thing to do — and none of them is a warning."""
    named = f" ({absent.sheet.name})" if absent.sheet is not None else ""
    return {
        NO_SHEET: "No sign-off sheet from the client yet — nothing to re-plan.",
        NO_RECORD: (
            f"The newest sign-off sheet{named} has not had its columns chosen yet — choose which "
            "column is the language, the filename and the barcode where the sheet is uploaded."
        ),
        FILE_GONE: (
            "The sign-off sheet the column choice was made for is no longer in "
            "videos/signoff/ — upload it again."
        ),
        UNREADABLE_INDEX: (
            "videos/signoff/signoff.json could not be read, so which column is which is not "
            "known — choose the columns again where the sheet is uploaded."
        ),
    }[absent.reason]
