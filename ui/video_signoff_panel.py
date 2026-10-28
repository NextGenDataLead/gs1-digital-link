"""The client's video sign-off sheet as a panel: upload it, say which column is which, apply.

A component, not a screen: it sits on the Data screen, where the batch is chosen. It is the one
place the shell writes ``mapping.yml`` — the mapping is otherwise edited in the file itself (see
:mod:`ui.video_map_panel`) — and it writes through a :class:`~ui.video_map_panel.MappingSession`,
which re-reads the file before re-planning so a hand edit made meanwhile is never overwritten.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple
from xml.etree import ElementTree as ET

from nicegui import events, ui

from lib import video_signoff, video_signoff_archive, xlsx
from lib.config import ClientConfig
from lib.errors import VideoMapError
from lib.media_video import files_by_language, load_video_map
from ui import DATA_ROOT, context, theme, video_map_edit
from ui.video_map_panel import MappingSession


def render(  # noqa: PLR0913 — the client, the mapping, what to redraw after, and its place
    cfg: ClientConfig,
    cid: str,
    session: MappingSession,
    *,
    applied: Callable[[], None],
    archived: Callable[[], None],
    step: int | None = None,
    below: Callable[[], None] | None = None,
    plan_into: list[ui.column] | None = None,
) -> None:
    """Take the client's filled-in sheet, show what it would change, and apply it.

    This is the one input the pilot has been waiting on, and until now it arrived as a spreadsheet
    somebody then re-typed into the rows below. The retyping is the whole cost: 173 rows, and one
    transposed digit maps a video to the wrong product with nothing downstream to catch it.

    **Nothing is written by the upload.** Reading the sheet produces a plan, the plan is shown, and
    a second press applies it — the same shape as the row-by-row editor, where edits accumulate and
    one Save writes them. :mod:`lib.video_signoff` decides; this only renders and asks.

    **Every sheet is kept**, dated, in ``videos/signoff/`` beside the mapping it is about, and the
    column choice is noted beside it once all three are set (:mod:`lib.video_signoff_archive`).
    That is what lets the data-quality report re-read the newest sheet against the mapping on every
    render and say what it *still* changes. Kept only once it has been read as a spreadsheet and is
    not a GS1 export — an unreadable file or the wrong document filed as "the client's newest
    sheet" would make the report describe something nobody sent.

    ``plan_into`` lets the caller put the review somewhere wider than this section: the Data screen
    sets the upload in a row of three, where the column pickers and the plan would not fit. It is a
    list the caller fills *after* this returns — the container has to be created below the row —
    and the upload reads it only when a sheet arrives.
    """
    with theme.section(
        "Upload the video sign-off sheet",
        step=step,
        anchor="video-signoff",
        explain=(
            "The spreadsheet the client fills in — `python -m scripts.report_video_candidates` "
            "writes the one to send them, and this reads it back. Any sheet with a language, a "
            "filename and a barcode column will do, whatever they are called: the headings are "
            "read where they are recognised and you can change any of them, so a sheet nobody "
            "anticipated still works. Every other column is ignored, and the table may sit below "
            "a title row on any sheet of the workbook. Uploading writes nothing: it shows what "
            "the sheet would change, and a second press applies it. The sheet leads for every "
            "barcode it names: that barcode gets exactly the video(s) the sheet gives it, and a "
            "video that loses its last barcode becomes skip. A barcode the sheet does not name "
            "keeps its video. One video may carry several barcodes — separate them with commas. "
            "A sheet that would give a product two videos in one language is refused. Each sheet "
            "is kept, dated, in videos/signoff/, with the columns "
            "you chose, so the data-quality report can say what it still changes."
        ),
    ):

        async def receive(event: events.UploadEventArguments) -> str:
            data = await event.file.read()
            try:
                grid = video_signoff.read_sheet(data)
            except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
                theme.notify_problem(f"That file could not be read as a spreadsheet: {exc}")
                return "Not read."
            if grid is None:
                theme.notify_problem(
                    "No sheet in that file has a row that could be a header — every row is empty "
                    "or holds a single cell."
                )
                return "Not read — no sheet with a header row in it."
            if video_signoff.looks_like_an_export(grid):
                theme.notify_problem(
                    "That looks like a GS1 Data Source export, not a sign-off sheet — it belongs "
                    "on the Data screen."
                )
                return "Not read — that is a GS1 export."
            try:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                sheet = video_signoff_archive.archive(session.path, data, stamp=stamp)
            except OSError as exc:
                theme.notify_problem(f"The sheet could not be kept, so nothing was read: {exc}")
                return "Not kept."

            exported = {product.gtin14 for product in context.load_products(cid)}
            box = plan_into[0] if plan_into else plan_box
            box.clear()
            with box:
                _columns_then_plan(
                    grid,
                    session,
                    cfg,
                    applied,
                    exported,
                    kept=_Kept(sheet, event.file.name, stamp),
                )
            # The sheet is on disk now, so the report has something new to re-plan — even though
            # nothing has been applied to the mapping.
            archived()
            return f"{len(grid.rows)} row(s) read from {event.file.name}, and kept as {sheet.name}."

        theme.upload("Sign-off sheet (.xlsx)", receive, busy="Reading the sheet…")
        # After the upload it fills: upload, then which column is which, then what it would do.
        plan_box = ui.column().classes("w-full")
        if below is not None:
            below()


class _Kept(NamedTuple):
    """The archived copy of the sheet on screen, and what it was called when it arrived."""

    sheet: Path
    given_name: str
    stamp: str


def _columns_then_plan(  # noqa: PLR0913 — the sheet, where it goes, and what it is checked against
    grid: xlsx.Grid,
    session: MappingSession,
    cfg: ClientConfig,
    applied: Callable[[], None],
    exported: set[str],
    *,
    kept: _Kept,
) -> None:
    """Which column is which — always shown, pre-filled from a guess — and the plan below it.

    **The guess is never the last word.** The accepted spellings in :mod:`lib.video_signoff` are a
    list of names somebody here imagined a client might use, and it was wrong about the only real
    sheet that exists: it calls the barcode ``current_gtin``, so the import refused the file it was
    built for. A list like that cannot be finished by thinking harder, so it pre-fills three pickers
    instead of deciding anything, and the operator — who has the file open in Excel — settles it.

    Shown even when the guess is right, because that is what makes the guess auditable. A column
    silently read as the barcode is the one mistake here that would publish the wrong video.
    """
    detected = video_signoff.columns(grid) or {}
    options = video_signoff.column_options(grid)
    chosen: dict[str, Any] = {}

    # A subhead, not a section: this sits inside the upload's own section, and a section inside a
    # section is the nesting ``theme.subhead`` exists to avoid.
    theme.subhead("Which column is which, in the video sign-off sheet")
    ui.label(
        "Read from the sheet's own headings where they are recognised. Change any of them — "
        "nothing is written by looking."
    ).classes("note")
    with ui.row().classes("gap-4 flex-wrap mt-2"):
        for name, label in _COLUMN_LABELS:
            select = ui.select(
                options, value=detected.get(name), label=label, clearable=True
            ).props("dense outlined")
            select.classes("min-w-56")
            chosen[name] = select
    # Below the pickers: you say which column is which, then read what that makes the sheet do.
    plan_box = ui.column().classes("w-full mt-4")

    def redraw() -> None:
        where = {
            name: int(select.value) for name, select in chosen.items() if select.value is not None
        }
        plan_box.clear()
        with plan_box:
            if len(where) < len(_COLUMN_LABELS):
                missing = [label for name, label in _COLUMN_LABELS if name not in where]
                theme.band(
                    f"Choose a column for: {', '.join(missing).lower()}. Then this will say what "
                    "the sheet would change.",
                    "quiet",
                )
                return
            # Noted on every complete choice, so the report re-plans against the columns the
            # operator settled on last — with the headings they had, so a sheet edited in place
            # afterwards is refused there rather than read with a column moved.
            problem = video_signoff_archive.record(
                session.path,
                sheet=kept.sheet,
                given_name=kept.given_name,
                at=datetime.strptime(kept.stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC).isoformat(),
                rows=len(grid.rows),
                columns=where,
                headers={name: grid.header[index] for name, index in where.items()},
            )
            if problem is not None:
                theme.notify_warning(f"The sheet is kept, but {problem}.")

            def replan() -> video_signoff.SignoffPlan:
                return video_signoff.plan(
                    grid,
                    load_video_map(session.path),
                    exported=exported,
                    languages=cfg.wordpress.languages,
                    where=where,
                )

            def done() -> None:
                applied()
                redraw()  # the plan now describes the file after the changes: nothing left to apply

            _plan_view(
                replan(), session, done, replan, cfg.wordpress.languages, lambda: _on_disk(cfg)
            )

    for select in chosen.values():
        select.on_value_change(redraw)
    redraw()


#: The three columns the import needs, labelled as the operator would describe them rather than as
#: the mapping names them. "Barcode" and not "GTIN": nobody outside this project says GTIN.
_COLUMN_LABELS: Final = (
    ("language", "Language"),
    ("file", "Video filename"),
    ("gtin", "Barcode"),
)


#: The plan's row tables, after the changes: what stops the sheet, then what is wrong in it.
#: ``unchanged`` and ``blank`` get no table — they are counts, and on a real sheet most of it.
_PLAN_SECTIONS: Final = (
    (
        video_signoff.AMBIGUOUS,
        "Two videos for one product ({n}) — the sheet cannot be applied",
        "The sheet gives these barcodes a second video in the same language. A product carries "
        "one video per language: with two, neither would show. Keep one video per barcode in the "
        "sheet, or write skip against the other, and upload it again.",
    ),
    (
        video_signoff.REJECTED,
        "Rejected ({n})",
        "Each of these says what is wrong with it, and the rest of the sheet is applied without "
        "it. A barcode problem is fixed in the sheet and the file uploaded again. An unknown "
        "filename usually means the mapping has no row for that video yet: when the file is in "
        "its video folder, the Add button above adds the row; when it is not, check the spelling, "
        "or copy the video into the folder first.",
    ),
)


def _plan_view(  # noqa: PLR0913 — the plan, the file it writes, and what it is checked against
    decided: video_signoff.SignoffPlan,
    session: MappingSession,
    applied: Callable[[], None],
    replan: Callable[[], video_signoff.SignoffPlan],
    languages: list[str],
    on_disk: Callable[[], dict[str, list[str]]],
) -> None:
    """What the sheet would do, then the button that does it.

    The counts come first and are deliberately not summed: a fill is work the import does, a
    conflict is work a person has to settle, a rejection is a defect in the sheet, and a blank is a
    row the client has not reached yet. Added together, the only number anybody could act on would
    be the one that disappeared.

    **Every figure that is also counted elsewhere carries the elsewhere number.** Two-video clashes
    are counted in products, the mapping's and this sheet's together — the figure report §1b and
    step 4's *Not eligible* show — and *left blank* says how it relates to the doctor's "rows with
    no GTIN yet". A 0 here beside a 2 there read as a contradiction, because it was a different
    count under the same name (operator feedback, 2026-10-07).
    """
    try:
        context = video_signoff.mapping_context(decided, load_video_map(session.path), languages)
    except VideoMapError as exc:
        theme.band(str(exc), "danger")
        return
    rejected = len(decided.of(video_signoff.REJECTED))
    blank = len(decided.of(video_signoff.BLANK))
    # First, right under the column choice: every figure below changes once these rows exist, and
    # on a long sheet the Rejected table they come from is far down the page.
    _add_new_files(decided, session, applied, replan, on_disk)
    with theme.figures():
        theme.figure(
            str(len(decided.changes)),
            "video(s) change",
            "barcodes added, moved or skipped",
        )
        theme.figure(
            str(context.clashing_products),
            "two-video clash(es)",
            f"products once applied · {len(context.existing_products)} in the mapping · "
            f"{len(context.resolved)} solved · {len(context.new_clashes)} new from this sheet",
        )
        theme.figure(str(rejected), "rejected", "see Rejected below")
        theme.figure(
            str(len(decided.of(video_signoff.UNCHANGED))),
            "already set",
            "sheet agrees with mapping",
        )
        theme.figure(
            str(blank),
            "left blank",
            f"no barcode · {context.unset} mapping row(s) have none",
        )

    if decided.refused:
        theme.band(
            "This sheet cannot be applied: it would give a product two videos in one language. "
            "Nothing is written — see the table below.",
            "danger",
        )
    if decided.changes:
        _changes_table(decided.changes)
    if decided.losing:
        theme.band(
            "These products would be left with no video: "
            + ", ".join(f"{gtin} ({language})" for language, gtin in decided.losing)
            + ". A page that is already live keeps the video it has — the tool can put a video "
            "on a page, not take one off. Remove it in WordPress if it should go.",
            "warn",
        )
    if context.clashes:
        _clash_table(context.clashes, context.resolved)
    for outcome, title, note in _PLAN_SECTIONS:
        rows = decided.of(outcome)
        if rows:
            _plan_table(title.format(n=len(rows)), note, rows)

    changes = decided.edits
    if not changes:
        if not decided.refused:
            theme.band(
                "This sheet changes nothing in the mapping, so there is nothing to apply.", "quiet"
            )
        return

    def apply() -> None:
        # **Re-planned now**, against the file as it is at this moment — never the changes
        # computed when the plan was drawn. In between, mapping.yml may have been edited by hand
        # (or by another window), and applying the old changes would undo that edit for every
        # video the sheet does not name — which the sheet-leads rule promises to leave alone.
        session.reload()
        if session.problem is not None:
            theme.notify_problem(session.problem)
            return
        try:
            now = replan().edits
        except VideoMapError as exc:
            theme.notify_problem(str(exc))
            return
        if not now:
            theme.notify_warning(
                "Nothing left to change — the mapping already says what this sheet says, or the "
                "sheet can no longer be applied as the mapping is now."
            )
            applied()
            return
        backup = session.write_edits(now)
        if backup is None:
            return
        # Everything that counts from the mapping recounts — the coverage figures, the mapping
        # table, the batch's Video column — because the session re-read the file after writing.
        applied()
        # A dialog, not a toast: this says how many videos changed and where the previous version
        # went, which is more than a toast has time for.
        theme.announce(
            f"{len(now)} video(s) changed",
            f"The previous version of the mapping is kept beside it as {backup.name}. Everything "
            "on this screen that counts from the mapping has been recounted. A page already live "
            "gets its new video on the next pages run."
            + (
                f" The mapping had changed since the sheet was read: {len(now)} video(s) changed "
                f"where the screen showed {len(changes)}."
                if len(now) != len(changes)
                else ""
            ),
        )

    theme.action(f"Apply this sheet — {len(changes)} video(s) change", apply)


def _on_disk(cfg: ClientConfig) -> dict[str, list[str]]:
    """The files in each language's video folder, resolved against the data folder."""
    folders = cfg.media.video_folders if cfg.media is not None else {}
    return files_by_language(
        {
            language: str(Path(folder) if Path(folder).is_absolute() else DATA_ROOT / folder)
            for language, folder in folders.items()
        }
    )


def _add_new_files(
    decided: video_signoff.SignoffPlan,
    session: MappingSession,
    applied: Callable[[], None],
    replan: Callable[[], video_signoff.SignoffPlan],
    on_disk: Callable[[], dict[str, list[str]]],
) -> None:
    """Give the videos the sheet names, and the mapping lacks, a row each — before anything else.

    A video copied into its folder after the mapping was drafted has no row, so the sheet's
    barcode for it is rejected and nothing on this screen could add one — the operator had to
    edit ``mapping.yml`` by hand. This adds blank rows only (:func:`video_map_edit.append_rows`,
    which never touches an existing one), and only for files that are in the folder; the plan is
    then drawn again, and the sheet's barcodes for them are applied by its own button.
    """
    missing = video_map_edit.files_missing_from_map(session.text, on_disk())
    wanted = video_signoff.addable(decided, missing)
    count = sum(len(files) for files in wanted.values())
    if not count:
        return
    theme.band(
        f"{count} video(s) this sheet names are in the video folders but have no row in the "
        "mapping yet, so their rows are rejected (see Rejected below). Add them first: each gets "
        "a blank row, the sheet is read again, and its barcodes for them can then be applied.",
        "warn",
    )

    def add() -> None:
        # Re-read and re-derived now, like Apply: the file or the folders may have changed since
        # the plan was drawn, and a row is never added for a file that is no longer there.
        session.reload()
        if session.problem is not None:
            theme.notify_problem(session.problem)
            return
        now = video_signoff.addable(
            replan(), video_map_edit.files_missing_from_map(session.text, on_disk())
        )
        if not now:
            theme.notify_warning("Nothing left to add — the mapping already has these videos.")
            applied()
            return
        text = session.text
        for language, files in now.items():
            text = video_map_edit.append_rows(text, language, files)
        backup = session.write(text)
        if backup is None:
            return
        applied()
        added = sum(len(files) for files in now.values())
        theme.announce(
            f"{added} video file(s) added to the mapping",
            f"Each has a blank row now. The previous version of the mapping is kept beside it as "
            f"{backup.name}. The sheet has been read again: its barcodes for these videos are "
            "under What the mapping would become, and Apply this sheet writes them.",
        )

    theme.action(f"Add {count} new video file(s) to the mapping", add)


def _changes_table(changes: tuple[video_signoff.FileChange, ...]) -> None:
    """Every video whose barcodes would change, before and after — what the button writes."""
    theme.subhead(f"What the mapping would become ({len(changes)})")
    ui.label(
        "Each video whose barcodes change. A video with no sheet row changes because another row "
        "gave its barcode to a different video; with none left it becomes skip."
    ).classes("note")
    columns = [
        {"name": "language", "label": "Lang", "field": "language", "align": "left"},
        {"name": "file", "label": "Video", "field": "file", "align": "left", "sortable": True},
        {"name": "before", "label": "Now", "field": "before", "align": "left"},
        {"name": "after", "label": "After", "field": "after", "align": "left"},
        {"name": "lines", "label": "Sheet row", "field": "lines", "align": "left"},
    ]
    data = [
        {
            "key": n,
            "language": change.language,
            "file": change.file,
            "before": video_signoff.describe(change.before),
            "after": video_signoff.describe(change.after),
            "lines": ", ".join(map(str, change.lines)) or "— another row took its barcode",
        }
        for n, change in enumerate(changes)
    ]
    table = ui.table(columns=columns, rows=data, row_key="key", pagination=0)
    table.classes("w-full mt-2 mb-6").props("dense flat bordered")


def _clash_table(clashes: tuple[video_signoff.Clash, ...], resolved: frozenset[str]) -> None:
    """The mapping's own two-video clashes — the products report §1b and *Not eligible* list."""
    products = len({clash.gtin for clash in clashes})
    theme.subhead(f"Two-video clashes already in the mapping ({products})")
    ui.label(
        "These products have two videos confirmed for one language, so neither is attached and the "
        "product is held — the same ones report §1b and Not eligible in step 4 list. Applying this "
        f"sheet solves {len(resolved)} of them; give the rest one video in the sheet, or mark the "
        "other skip."
    ).classes("note")
    columns = [
        {"name": "gtin", "label": "GTIN", "field": "gtin", "align": "left"},
        {"name": "language", "label": "Lang", "field": "language", "align": "left"},
        {"name": "files", "label": "Files", "field": "files", "align": "left"},
    ]
    data = [
        {
            "key": n,
            "gtin": c.gtin,
            "language": c.language,
            "files": ", ".join(c.files) + (" — solved by this sheet" if c.gtin in resolved else ""),
        }
        for n, c in enumerate(clashes)
    ]
    table = ui.table(columns=columns, rows=data, row_key="key", pagination=0)
    table.classes("w-full mt-2 mb-6").props("dense flat bordered")


def _plan_table(title: str, note: str, rows: tuple[video_signoff.SignoffRow, ...]) -> None:
    """One outcome's rows. The spreadsheet row number comes first: it is the address of the fix."""
    theme.subhead(title)
    ui.label(note).classes("note")
    columns = [
        {"name": "line", "label": "Row", "field": "line", "align": "left", "sortable": True},
        {"name": "language", "label": "Lang", "field": "language", "align": "left"},
        {"name": "file", "label": "File", "field": "file", "align": "left", "sortable": True},
        {"name": "given", "label": "In the sheet", "field": "given", "align": "left"},
        {"name": "gtin", "label": "Read as", "field": "gtin", "align": "left"},
        {"name": "detail", "label": "Why", "field": "detail", "align": "left"},
    ]
    data = [
        {
            "line": row.line,
            "language": row.language,
            "file": row.file,
            "given": row.given,
            "gtin": row.gtin or "—",
            "detail": row.detail,
        }
        for row in rows
    ]
    table = ui.table(columns=columns, rows=data, row_key="line", pagination=0)
    table.classes("w-full mt-2").props("dense flat bordered")
