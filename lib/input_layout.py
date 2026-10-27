"""The shape of a client's ``input/`` folder, and the note that explains it on the spot.

    input/{client}/
      process/
        uploads/    GS1 export/export.xlsx              the export a run reads
                    GS1 export/export-{stamp}.xlsx      every export ever uploaded
                    product-list.xlsx                   the list as the operator sent it
                    product-list-{stamp}.xlsx           every list ever uploaded
        selection/  selections.xlsx                     the selection a run reads
                    selection-{stamp}.xlsx              every selection ever saved
      videos/     mapping.yml                         which video is which product's
                  signoff/signoff-{stamp}.xlsx        every sign-off sheet the client sent
                  signoff/signoff.json                which column of the newest is which
      reference/  test-uploads/  superseded/          nothing a run reads

``process/`` holds the two halves of one act: **what arrived**, and **what was chosen from it**.
Each document sits with its own history, so a person looking for "the list I sent in August" opens
``uploads/`` and a person asking "what did we publish last week?" opens ``selection/``.

Nothing in ``uploads/`` shares a name with anything in ``selection/``. The uploaded list is a
``product-list``; a chosen one is a ``selection``. They were both called "selection" for a while
and the folders stopped distinguishing themselves the moment they were.

**Live files keep fixed names** because ``clients.yml`` points at them and there is no
command-line override. A layout where runs took "the newest file in the folder" would mean any
stray copy — a Finder duplicate, a re-download landing as ``selections (1).xlsx`` — silently
becoming what the next run publishes, with nothing raised and the plan looking ordinary. A wrong
fixed path fails the other way: the count on the Data screen is wrong the moment you look.

**Everything is archived on the way *in*, never on the way out.** An upload is copied the moment
it lands; a selection is copied as it is saved. Archiving the file being *replaced* instead —
which is what this did first — gets two things wrong at once, and both are visible by counting the
files after one upload and two saves: the untouched upload gets filed as a selection nobody chose,
and the selection actually in force is the one list with no dated copy at all.

Both archives are permanent: ``input/`` is gitignored, so a delete cannot be undone, and nothing
here ever deletes.

**Every path in this layout is named here and nowhere else.** They used to be spelled on the
screens that wrote them, and ``scripts/`` cannot import ``ui/`` — so three consumers each carried
their own guess and two of them were wrong for a week: ``run_execute`` looked for the uploaded list
at ``selection/uploaded.xlsx`` and ``report_scope_result`` at ``selections.source.xlsx``, neither of
which anything writes. Both failures were silent, because both sites treat an absent file as "this
client never uploaded one".

**Nothing here creates a directory.** A reader asking *where does the upload live* must not make
the folder as a side effect: ``scripts/run_execute`` asks exactly that, and a run's whole contract
is that it reads ``input/`` and writes ``output/``. The two functions that write into ``uploads/``
call :func:`ensure_uploads_dir` themselves.

A ``README.md`` is written into the client's folder so the layout explains itself to somebody
looking at it in Finder, who has no reason to have read any of this.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from lib.config import ClientConfig

#: The folder under a client that holds everything a run touches. Everything beside it —
#: reference workbooks, test uploads, videos — is there for a person, not for a run.
PROCESS_DIR: Final = "process"

#: The half of ``process/`` holding what arrived, as it arrived.
UPLOADS_DIR: Final = "uploads"

#: Beside ``mapping.yml``: the client's sign-off sheets, and the note of which column is which.
SIGNOFF_DIR: Final = "signoff"
SIGNOFF_INDEX: Final = "signoff.json"

#: The note the tool drops beside the files. Rewritten whenever it changes, so it cannot go stale
#: against the code that writes the files it describes.
README_NAME: Final = "README.md"

_README: Final = """# What is in this folder

Nothing here is edited by hand. The operator shell writes all of it.

Everything a run touches is under **`process/`**. It has two halves: what arrived, and what you
chose from it.

## `process/uploads/` — what you sent, kept forever

| File | What it is |
|---|---|
| `GS1 export/export.xlsx` | The GS1 Data Source export **a run reads**. |
| `GS1 export/export-{stamp}.xlsx` | Every export you ever uploaded, dated. |
| `product-list.xlsx` | Your product list exactly as you sent it, before any ticking. |
| `product-list-{stamp}.xlsx` | Every product list you ever uploaded, dated. |

## `process/selection/` — what you chose

| File | What it is |
|---|---|
| `selections.xlsx` | Your list minus the rows you unticked. **A run reads this.** |
| `selection-{stamp}.xlsx` | Every selection you saved, dated. The newest matches the live one. |

## `process/history.jsonl` — what came from what

One line per upload and per save, written as it happens. It is the only place two things are
recorded: **what you called each file when you sent it** (uploading renames it, so otherwise the
name is gone), and **which export each selection was ticked against**.

That second one is what lets the screens warn you when a selection was chosen against an export you
have since replaced — a barcode the new export has no row for publishes nothing and reports no
error, so there is otherwise nothing to notice.

Made by the tool, for the tool. Nothing breaks if it is missing — the screens just say
"not recorded".

## The other folders

**`reference/`** — workbooks a *setup* step reads, not a run. The GS1 DIY sector datamodel lives
here; `build_brick_map --datamodel` turns it into the category map.

**`test-uploads/`** — files kept on purpose so there is always something safe to practise an
upload with. Nothing reads them.

**`superseded/`** — old inputs set aside before this layout existed, with a note of their own.

**`videos/`** — the video files and `mapping.yml`, at the paths `media.video_folders` names.
`videos/signoff/` keeps every sign-off sheet the client sent back, dated, and `signoff.json` beside
them records which column of the newest one is the language, the filename and the barcode — so the
data-quality report can re-read the sheet against the mapping every time it is made. A run reads
none of it.

## The two rules worth knowing

**A run reads exactly two files:** `process/uploads/GS1 export/export.xlsx` and
`process/selection/selections.xlsx`. They are the two without a date. Everything else is history;
adding a file to any of these folders changes nothing, however new it is.

**Nothing here is ever deleted.** This folder is not in version control, so a delete would be
permanent. Every upload and every save is copied as it happens.

## Going back

The Data screen has **Start again from my uploaded file**, which puts
`process/uploads/product-list.xlsx` back as `process/selection/selections.xlsx` — no need to find
the original again. Nothing is lost: every selection you saved is already dated beside it.

## What a run keeps

Each run owns a folder under `output/{client}/runs/{stamp}/` and writes four things into it, so the
record of a batch cannot be overwritten by the next one:

| File | What it is |
|---|---|
| `run.jsonl` | What it did, one line per product per language, as it went. |
| `inputs.json` | What it **read** — which selection, and which export those ticks came from. |
| `selection-used.xlsx` | The ticked list it consumed, copied in before it started. |
| `selection-uploaded.xlsx` | The list that came from, so dropped rows can be named. |
| `result.xlsx` | Your own list with what happened to each row appended. |
"""


# --- Where each document lives -------------------------------------------------------------
#
# Every helper takes the **live** file as its argument — the control file for the selection half,
# the configured export path for the other — and derives the rest. Derived rather than configured
# because there is exactly one layout, and a second place to declare it is a second place for it
# to be wrong. All of them are pure: see the module docstring on why none of them mkdir.


def uploads_dir(control: Path) -> Path:
    """``process/uploads/`` — everything the operator sent, derived from the live selection.

    The control file is ``process/selection/selections.xlsx``, so uploads are its aunt.
    """
    return control.parent.parent / UPLOADS_DIR


def ensure_uploads_dir(control: Path) -> Path:
    """:func:`uploads_dir`, created. Only the two functions that write into it may call this."""
    folder = uploads_dir(control)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def archive_path(control: Path) -> Path:
    """The most recent uploaded list: ``process/uploads/product-list.xlsx``.

    It lives with the other uploads and not beside the live file, because it is not one: an upload
    is what the operator sent, a selection is what they chose from it. And it carries the word
    **list** rather than **selection**, so that nothing in ``uploads/`` shares a name with
    anything in ``selection/`` — the two folders hold different documents and a shared name is how
    that stops being obvious.

    Undated on purpose — three things have to find it without guessing, the Restore control, the
    run that copies it into its own directory, and the per-run result sheet that reads it to name
    the rows the operator deselected. Resolving it as "the newest file in the folder" would let any
    stray copy dropped there become the operator's original.
    """
    return uploads_dir(control) / f"product-list{control.suffix}"


def uploads_path(control: Path, stamp: str) -> Path:
    """Where an uploaded list is kept forever, dated: ``uploads/product-list-{stamp}.xlsx``.

    Named for the document the operator sent rather than for the control file it becomes, because
    this folder is read by a person looking for "the list I sent in August" and never by the tool.
    """
    return unique(uploads_dir(control) / f"product-list-{stamp}{control.suffix}")


def selections_path(control: Path, stamp: str) -> Path:
    """Where a saved selection is kept forever, dated: ``selection/selection-{stamp}.xlsx``.

    Written when the save happens, not when the next save displaces it. ``.bak`` held exactly one
    version, so two saves lost the first; archiving-on-replacement fixed that and introduced a
    subtler error — the record was always one save behind, so the list actually in force was the
    one list with no dated copy.
    """
    # Beside the live selection, not in a folder of its own: these *are* the selection's history,
    # and one folder holding "what is chosen now" and "what was chosen before" is the smallest
    # arrangement that says so.
    return unique(control.parent / f"selection-{stamp}{control.suffix}")


def export_archive_path(export: Path, stamp: str) -> Path:
    """Where an uploaded export is kept forever: ``uploads/GS1 export/export-{stamp}.xlsx``.

    Beside the live export, in its own subfolder of ``uploads/``, because an export is never edited
    here — the current one *is* an upload, so its history belongs with it rather than one level up
    among the product lists.

    Derived from the configured export path rather than from the control file, so a client whose
    export sits somewhere else still archives beside it. That is also why the folder is not named
    in this module: ``clients.yml`` names it, and the space in ``GS1 export`` is the operator's.
    """
    return unique(export.with_name(f"{export.stem}-{stamp}{export.suffix}"))


def export_archives(export: Path) -> list[Path]:
    """Every dated copy of this export, newest **by modification time** first.

    By mtime and never by name, for the reason :func:`unique` gives: a same-second pair reads the
    wrong way round sorted lexically.
    """
    folder = export.parent
    if not folder.is_dir():
        return []
    dated = [
        path
        for path in folder.glob(f"{export.stem}-*{export.suffix}")
        if path.is_file() and path != export
    ]
    return sorted(dated, key=lambda path: path.stat().st_mtime, reverse=True)


def list_archives(control: Path) -> list[Path]:
    """Every dated copy of the uploaded product list, newest by modification time first."""
    return _dated(uploads_dir(control), f"product-list-*{control.suffix}")


def selection_archives(control: Path) -> list[Path]:
    """Every dated copy of a saved selection, newest by modification time first."""
    return _dated(control.parent, f"selection-*{control.suffix}")


def _dated(folder: Path, pattern: str) -> list[Path]:
    if not folder.is_dir():
        return []
    found = [path for path in folder.glob(pattern) if path.is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def signoff_dir(video_map: Path) -> Path:
    """``videos/signoff/`` — every sign-off sheet the client sent, beside the mapping it is about.

    Derived from ``media.video_map_path``, not from the selection, and **not under ``process/``**:
    the README states that a run reads exactly two files and names them, and a third document there
    would make that false. A sheet is about ``mapping.yml``, so it lives next to it.
    """
    return video_map.parent / SIGNOFF_DIR


def signoff_path(video_map: Path, stamp: str) -> Path:
    """Where an uploaded sign-off sheet is kept forever, dated: ``signoff/signoff-{stamp}.xlsx``."""
    return unique(signoff_dir(video_map) / f"signoff-{stamp}.xlsx")


def signoff_index(video_map: Path) -> Path:
    """``signoff/signoff.json`` — which column of the newest sheet is which, and its headings."""
    return signoff_dir(video_map) / SIGNOFF_INDEX


def signoff_archives(video_map: Path) -> list[Path]:
    """Every archived sign-off sheet, newest by modification time first."""
    return _dated(signoff_dir(video_map), "signoff-*.xlsx")


def unique(candidate: Path) -> Path:
    """``candidate``, or the next free ``-1``, ``-2``… beside it.

    Two writes inside one second are ordinary here — untick a row, glance at the count, untick
    another — and a second-resolution name silently overwrote the first, so "every version is
    kept" kept one.

    Read these back **by modification time, never by name**: ``-1`` sorts *before* the unsuffixed
    name, because ``-`` precedes ``.``. That is the same trap the run logs carry, and it means a
    sorted listing shows a same-second pair the wrong way round.
    """
    if not candidate.exists():
        return candidate
    serial = 0
    while candidate.exists():
        serial += 1
        candidate = candidate.with_name(
            f"{candidate.stem.rsplit('-', 1)[0] if serial > 1 else candidate.stem}"
            f"-{serial}{candidate.suffix}"
        )
    return candidate


# --- The note in the folder ----------------------------------------------------------------


def write_readme(cfg: ClientConfig) -> Path | None:
    """Drop the note beside the client's input files. Returns where, or ``None`` if there is no
    sensible place — a client whose export path has no parent to write into.

    Written by the tool rather than kept in ``docs/`` because the person confused by these files is
    looking at them in a file browser, not reading the repository. The condition the operator set
    when this layout was agreed was that it be intuitive *without* the documentation; a note in the
    folder is the version of documentation that is present at the moment of the question.
    """
    folder = client_root(Path(cfg.export.path))
    if folder is None or not folder.is_dir():
        return None
    path = folder / README_NAME
    path.write_text(_README, encoding="utf-8")
    return path


def client_root(path: Path) -> Path | None:
    """Walk up from a configured path to the client folder — the one holding ``process/``.

    Counting parents would work and would break the next time a document gains a subfolder of its
    own, which is exactly what the GS1 export just did. Looking for the named directory does not
    care how deep the file sits under it.
    """
    for parent in path.parents:
        if parent.name == PROCESS_DIR:
            return parent.parent
    return None
