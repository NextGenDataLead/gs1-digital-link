"""Step 5 of the Data screen, and the coverage funnel above it: which products are in the batch.

**The selection comes first, and everything under it is about the selection** (operator,
2026-10-09). The table at the top is every row of the list, each with a **Status** — what a run
would do with it — and a tick box. Under it, one folded table per problem, holding only the
**ticked** products that have it:

* **not in the GS1 export** — on the list, nothing behind it;
* for a batch that writes pages: **not eligible** (missing data, or two videos for one language,
  from :mod:`lib.eligibility`) and **missing video(s)** (they run, without a video somewhere);
* for a links-only batch: **link doesn't work** — no page to point at, or one that does not load.

Coverage counts the same ticked products. A 117-row list with four ticked is a batch of four, and
the other 113's problems are not this batch's.

The funnel counts **products** (distinct barcodes), not spreadsheet rows: the pilot's own list
carries one barcode on two rows, and a run publishes products.

**The rows are the uploaded list; the ticks are the saved batch.** The grid is built from the list
as it arrived, every row of it, and a row is ticked when the saved selection names its barcode — so
a restart shows the whole list with the batch the later screens act on ticked, never a different
one. A new upload replaces both, so everything arrives ticked again. **Next saves the ticked rows
that can run and nothing else**: the saved file *is* the batch, and every screen after this one
counts it as such — a ticked row the run would drop is named in the caption, not written.

**A tick survives anything but a new list.** Unticked rows are remembered per client for the life
of the process, so a mapping edit that rebuilds this step — a video arriving can make a product
eligible — does not cost the operator the choices they had made.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from nicegui import ui

from lib import ticked_snapshot
from lib.complete_report import listed_rows, shared_barcodes
from lib.eligibility import CHECKING, Eligibility
from lib.errors import ProcessListError
from lib.issue_report import Issue, selection_issues
from lib.process_list import ProcessListSheet
from lib.records import ProductRecord
from ui import process_list_edit, theme

#: The row key: a row's position in the sheet as first read. Fixed when the grid is built and
#: never renumbered — see ``ProcessListSheet.keeping`` for what accumulating edits does instead.
_ROW: Final = "_row"

#: The row's barcode, canonicalised the way the export join reads it. Carried, not rendered: the
#: table shows the operator's own barcode column; this is for counting *products*.
_GTIN: Final = "_gtin"

#: What a run would do with the row — one of the short words below, so it filters with a picker.
_STATUS: Final = "_status"

#: The reason behind the status, in a few words: what it waits on, why it is held, what is wrong
#: with its link.
_DETAIL: Final = "_detail"

#: Links-only batches: the address the GS1 record would point at, on the link-problem table.
_URL: Final = "_url"

CAN_RUN: Final = "can run"
CAN_RUN_NO_VIDEO: Final = "can run · no video"
NOT_IN_EXPORT: Final = "not in the export"
NOT_ELIGIBLE: Final = "not eligible"
LINK_PROBLEM: Final = "link problem"
CHECKING_LINK: Final = "checking link"

#: The statuses Next saves.
RUNNABLE: Final = frozenset({CAN_RUN, CAN_RUN_NO_VIDEO})

#: Height of the eligible table. Long enough to work in, short enough that Next stays on screen.
_TABLE_HEIGHT: Final = "55vh"


@dataclass
class Ticks:
    """The rows the operator unticked, per client, for the life of the process. See the module."""

    unticked: set[int] = field(default_factory=set)
    #: Whether :attr:`unticked` has been read from the saved batch yet. Cleared with it when a new
    #: list arrives, so the next build seeds again — from the upload, which ticks everything.
    seeded: bool = False


@dataclass(frozen=True)
class Funnel:
    """The ticked products, and what each of them still needs. Counted over the **selection**.

    Operator, 2026-10-09: the figures and the issue tables are about the products in the batch, not
    the whole list — a 117-row list with 4 ticked is a batch of 4, and its 40 missing videos are not
    this batch's business. Products, not rows: a barcode on two rows is one product.

    Attributes:
        listed: Distinct barcodes on the whole list — context, the one figure not about the ticks.
        selected: Distinct ticked barcodes.
        can_run: Of the selected, those a run will act on — what Next saves.
        not_in_export: Selected, and the export carries nothing for them.
        not_eligible: Selected, in the export, and a page run would hold them.
        missing_video: Selected, can run, and go live without a video somewhere.
        bad_link: Selected, links-only batch, and the GS1 record's target is missing or broken.
    """

    listed: int
    selected: int
    can_run: int
    not_in_export: int
    not_eligible: int
    missing_video: int
    bad_link: int = 0


def funnel(
    rows: list[dict[str, Any]],
    ticked: list[dict[str, Any]],
    verdict: Eligibility,
    exported: set[str],
) -> Funnel:
    """Count the selection: every ticked row, by product, split the way a run treats it. Pure."""
    listed = {row[_GTIN] for row in rows if row[_GTIN]}
    selected = {row[_GTIN] for row in ticked if row[_GTIN]}
    in_export = selected & exported
    runnable = {gtin for gtin in in_export if verdict.is_eligible(gtin)}
    linked = in_export & set(verdict.bad_link)
    return Funnel(
        listed=len(listed),
        selected=len(selected),
        can_run=len(runnable),
        not_in_export=len(selected - in_export),
        not_eligible=len(in_export - runnable - linked),
        missing_video=len(runnable & set(verdict.missing_video)),
        bad_link=len(linked),
    )


def draw_funnel(box: ui.element, counts: Funnel, *, links_only: bool = False) -> None:
    """Coverage: the selection as figures, each saying what it counts."""
    box.clear()
    with box:
        theme.subhead(
            "Coverage",
            explain=(
                "The products you ticked in step 5, counted as products — a barcode on two rows "
                "of your list is one. Selected → can run (what Next saves), and why the rest "
                "cannot. Tick or untick and these follow; the tables under the products list the "
                "same problems, for the same ticked products."
            ),
        )
        with theme.figures():
            theme.figure(
                str(counts.selected), "selected", f"ticked, of {counts.listed} on your list"
            )
            theme.figure(str(counts.can_run), "can run", "what Next saves")
            theme.figure(
                str(counts.not_in_export), "not in the export", "the GS1 export has no row for them"
            )
            if links_only:
                theme.figure(
                    str(counts.bad_link), "link problems", "no page to link to, or it does not load"
                )
            else:
                theme.figure(
                    str(counts.not_eligible), "not eligible", "missing data, or two videos"
                )
                theme.figure(
                    str(counts.missing_video), "missing video(s)", "can run, live without one"
                )


def shared_barcode_notes(
    sheet: ProcessListSheet, names: dict[str, str]
) -> list[tuple[str, str, str]]:
    """``(barcode, band kind, sentence)`` for every barcode on more than one row of the list. Pure.

    Two different products on one barcode is almost always a typo, and it was invisible here: the
    only trace was the caption counting 97 rows beside a Coverage of 96 products (7 Days and Fun
    Grill share ``2987653008486``; the export says it is the grill). The same product listed twice
    is harmless — it counts once — so it gets a quiet note, not a warning.
    """
    notes = []
    for gtin, labels in shared_barcodes(listed_rows(sheet)).items():
        named = names.get(gtin)
        if len(set(labels)) > 1:
            says = f" The export says it is {named}." if named else " The export does not carry it."
            notes.append(
                (
                    gtin,
                    "warn",
                    f"Barcode {gtin} is on {len(labels)} rows with different products: "
                    f"{', '.join(labels)}.{says} Correct the barcode in your product list "
                    "and upload it again.",
                )
            )
        else:
            notes.append(
                (
                    gtin,
                    "quiet",
                    f"Barcode {gtin} is on {len(labels)} rows of your list for the same product "
                    f"({labels[0]}). It counts once.",
                )
            )
    return notes


def save_line(ticked: int, can_run: int, *, onward: str = "the copy") -> str:
    """What Next will do, said before it is pressed — in products, as Coverage counts them.

    A ticked product that cannot run is **not** saved: the saved file is what the next run reads,
    and a row in it that the run then drops would read as chosen on every later screen. Said here,
    so the tick that is about to be ignored is not a surprise.
    """
    if not ticked:
        return "Tick at least one product — Next stays off until then."
    if not can_run:
        return "None of the ticked products can run — see the tables below. Next stays off."
    left_out = ticked - can_run
    if not left_out:
        return f"Next saves all {can_run} ticked product(s) and goes on to {onward}."
    return (
        f"Next saves {can_run} of {ticked} ticked product(s) — {left_out} cannot run and "
        f"{'is' if left_out == 1 else 'are'} left out, see below — and goes on to {onward}."
    )


def row_status(gtin: str | None, exported: set[str], verdict: Eligibility) -> tuple[str, str]:
    """A row's ``(status, detail)``: what a run would do with it, and the reason in a few words.

    The status is one of a handful of short words, so the Status column filters with a picker;
    the detail is the sentence the issue tables show. Pure.
    """
    if gtin is None or gtin not in exported:
        return NOT_IN_EXPORT, ""
    if verdict.problem:
        return NOT_ELIGIBLE, verdict.problem
    if gtin in verdict.bad_link:
        problem = verdict.bad_link[gtin].problem
        return (CHECKING_LINK if problem == CHECKING else LINK_PROBLEM), problem
    if gtin in verdict.not_eligible:
        return NOT_ELIGIBLE, verdict.not_eligible[gtin]
    if gtin in verdict.missing_video:
        return CAN_RUN_NO_VIDEO, verdict.missing_video[gtin]
    return CAN_RUN, ""


def choose(  # noqa: PLR0913, PLR0915 — the batch, its verdict, its save, caption, ticks, funnel
    sheet: ProcessListSheet,
    products: list[ProductRecord],
    verdict: Eligibility,
    *,
    record: Callable[[Path, ProcessListSheet], None],
    commit: dict[str, Callable[[], bool]],
    caption: ui.label,
    ticks: Ticks,
    counted: Callable[[Funnel], None],
    saved: frozenset[str],
    target: Path,
    links_only: bool = False,
    report: Callable[[list[Issue], list[tuple[str, str]]], None] | None = None,
) -> None:
    """Step 5: every row of the list, ticked or not, and under it what the ticked ones still need.

    Args:
        sheet: The list as uploaded — every row, whatever was saved since.
        products: The export's products.
        verdict: :func:`lib.eligibility.eligibility_for` over the products the list names.
        record: Called with where the save was kept and what it holds, so the page can note which
            export the selection was chosen against.
        commit: Receives ``"save"``, which :func:`ui.theme.onward`'s Next calls.
        caption: The line above Next, kept current with :func:`save_line`.
        ticks: The rows unticked so far — read to build, written on every change.
        counted: Called with the funnel whenever a tick changes.
        saved: The barcodes of the saved batch. Seeds the ticks once per list — see :class:`Ticks`.
        target: Where Next writes the batch: the control file a run reads, not the upload.
        links_only: The batch publishes GS1 links only — its problems are links, not data.
        report: Called with the ticked products' issues and the ticked ``(barcode, name)``
            whenever the ticks change — the client's issue report follows the selection.
    """
    exported = {product.gtin14 for product in products}
    columns = [
        # Positional field names. The operator's headers are their own text: two may be the same
        # word and one may be blank, and either collapses a keyed-by-label row into fewer cells.
        {"name": f"c{n}", "label": name or "—", "field": f"c{n}", "align": "left", "sortable": True}
        for n, name in enumerate(sheet.header)
    ]

    def row_of(index: int) -> dict[str, Any]:
        gtin = sheet.gtin14_at(index)
        status, detail = row_status(gtin, exported, verdict)
        issue = verdict.bad_link.get(gtin) if gtin else None
        return {
            _ROW: index,
            **{f"c{n}": value for n, value in enumerate(sheet.rows[index])},
            _GTIN: gtin,
            _STATUS: status,
            _DETAIL: detail,
            _URL: (issue.url or "—") if issue else "",
        }

    every = [row_of(n) for n in range(len(sheet.rows))]
    if not ticks.seeded:
        ticks.unticked = unticked_by(every, saved)
        ticks.seeded = True

    if verdict.problem:
        theme.band(f"No product can run: {verdict.problem}.", "danger")

    grid = _scope_table(
        [*columns, _short(_STATUS, "Status"), _wrapping(_DETAIL, "Detail")],
        every,
        untick=ticks.unticked,
    )
    issues = ui.column().classes("w-full gap-0 mt-6")
    names = {p.gtin14: (p.product_name.values.get("nl") or "") for p in products}
    keys = {int(row[_ROW]) for row in every}
    # The export's name, else the list's own words for the row — a product the export does not
    # carry still needs a name in the client's report.
    report_names = {row.gtin: row.label for row in listed_rows(sheet) if row.gtin}
    report_names.update({gtin: name for gtin, name in names.items() if name})
    onward = "the preflight" if links_only else "the copy"

    def draw_issues(ticked: list[dict[str, Any]]) -> None:
        issues.clear()
        with issues:
            _issue_tables(columns, ticked, links_only=links_only)
            chosen = {row[_GTIN] for row in ticked}
            for gtin, kind, sentence in shared_barcode_notes(sheet, names):
                if gtin in chosen:
                    theme.band(sentence, kind)
        if report is not None:
            products = ticked_products(ticked, report_names)
            report(selection_issues(products, exported, verdict), products)

    def describe() -> None:
        ticked = grid.selected
        ticks.unticked = keys - {int(row[_ROW]) for row in ticked}
        counts = funnel(every, ticked, verdict, exported)
        caption.text = save_line(counts.selected, counts.can_run, onward=onward)
        draw_issues(ticked)
        counted(counts)

    grid.on_select(describe)

    def save() -> bool:
        # The ticked rows that can run, and nothing else: the file a run reads *is* the batch, so a
        # row in it the run then drops would be counted as chosen by every screen after this one.
        chosen = batch_of(sheet, runnable_keys(grid.selected), target)
        try:
            saved_at = process_list_edit.save_sheet(chosen)
        except ProcessListError as exc:
            theme.notify_problem(str(exc))
            return False
        # What was ticked and why each could or could not run, beside the saved selection — so
        # the run's issue report is about the same products as the one on this screen.
        products = ticked_products(grid.selected, report_names)
        try:
            ticked_snapshot.write(
                chosen.path, products, selection_issues(products, exported, verdict)
            )
        except OSError as exc:
            theme.notify_warning(f"Saved, but the ticked products could not be kept: {exc}")
        record(saved_at, chosen)
        theme.notify_ok("Saved")
        return True

    commit["save"] = save


def ticked_products(ticked: list[dict[str, Any]], names: dict[str, str]) -> list[tuple[str, str]]:
    """``(barcode, name)`` of each ticked product once, in list order. Pure."""
    seen: dict[str, str] = {}
    for row in ticked:
        gtin = row[_GTIN]
        if gtin and gtin not in seen:
            seen[gtin] = names.get(gtin, "")
    return list(seen.items())


def _issue_tables(
    columns: list[dict[str, Any]], ticked: list[dict[str, Any]], *, links_only: bool
) -> None:
    """What the ticked products still need, one folded table per reason. Empty tables are absent."""

    def having(*statuses: str) -> list[dict[str, Any]]:
        return [row for row in ticked if row[_STATUS] in statuses]

    _readonly(
        f"Not in the GS1 export ({len(having(NOT_IN_EXPORT))})",
        "Ticked, but the export carries no row for these barcodes, so a run can do nothing for "
        "them. Either the product is missing from the export — fix it in MyGS1 and export again — "
        "or the barcode is wrong. Next leaves them out.",
        columns,
        having(NOT_IN_EXPORT),
        collapsed=True,
    )
    if links_only:
        _readonly(
            f"Link doesn't work ({len(having(LINK_PROBLEM, CHECKING_LINK))})",
            "Ticked, but the GS1 record would have nowhere sound to point: no address in Link "
            "naar site and no page of ours, an address that is not on the client's site, or a "
            "page that does not load. A GS1 record can never be deleted, so these are left out "
            "until the address is fixed in your product list and the list is uploaded again.",
            [*columns, _wrapping(_URL, "Address"), _wrapping(_DETAIL, "Problem")],
            having(LINK_PROBLEM, CHECKING_LINK),
            collapsed=True,
        )
        return
    held = having(NOT_ELIGIBLE)
    _readonly(
        f"Not eligible — {_held_title(held)} ({len(held)})",
        "Ticked, but a run will hold these: a mandatory value is blank (fix it in MyGS1 — it is "
        "never filled in here), or the client confirmed two videos for one language and has to "
        "keep one. The Why column says which. Next leaves them out; they can run once fixed.",
        [*columns, _wrapping(_DETAIL, "Why")],
        held,
        collapsed=True,
    )
    _readonly(
        f"Missing video(s) ({len(having(CAN_RUN_NO_VIDEO))})",
        "Ticked and they will run — but the page goes live with no video in the language named "
        "here. Every live run lists them again in its data-quality note. When the client confirms "
        "a video, the next run adds it to the page.",
        [*columns, _wrapping(_DETAIL, "Waiting on")],
        having(CAN_RUN_NO_VIDEO),
        collapsed=True,
    )


def runnable_keys(ticked: list[dict[str, Any]]) -> set[int]:
    """The ticked rows Next writes: those a run will act on. Pure."""
    return {int(row[_ROW]) for row in ticked if row[_STATUS] in RUNNABLE}


def unticked_by(rows: list[dict[str, Any]], saved: frozenset[str]) -> set[int]:
    """The rows the saved batch does not name — what a fresh build starts unticked. Pure.

    By barcode, not by row: the batch file is a subset of the upload and renumbers it. A barcode on
    two rows of the upload is ticked on both, which is one product either way. A list just uploaded
    *is* the saved batch, so every row of it arrives ticked and its problems show at once.
    """
    return {int(row[_ROW]) for row in rows if row[_GTIN] not in saved}


def batch_of(sheet: ProcessListSheet, ticked: set[int], target: Path) -> ProcessListSheet:
    """What Next writes: the ticked rows of the uploaded list, in its order, at ``target``. Pure.

    Nothing else — not the rows the export lacks, not the held ones. The file a run reads *is* the
    batch; a row kept there "so the result sheet can name it" was counted as chosen by every
    screen after this one.
    """
    return replace(sheet.keeping(ticked), path=target)


def _held_title(held: list[dict[str, Any]]) -> str:
    """Name what the not-eligible table actually holds — a two-video product is not missing data."""
    clash = any("two videos" in str(row[_DETAIL]) for row in held)
    return "missing data or two videos" if clash else "missing data"


def _short(name: str, label: str) -> dict[str, Any]:
    """A column of a few short words, sortable."""
    return {"name": name, "label": label, "field": name, "align": "left", "sortable": True}


def _wrapping(name: str, label: str) -> dict[str, Any]:
    """A column of sentences, which wraps. Table cells do not by default, and one long reason
    ("missing data: dim_height (attr 3498), dim_width …") made the whole page scroll sideways."""
    style = "white-space: normal; min-width: 10rem"
    return {"name": name, "label": label, "field": name, "align": "left", "style": style}


def _readonly(
    title: str,
    explain: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    collapsed: bool = False,
) -> None:
    """A headed table with no choice in it. Absent when empty: an empty table is not a finding.

    ``collapsed`` folds it, with the count in the title so the fold still says how many: the
    operator asked for all three read-only tables to start shut — they are reference, and the
    eligible table (which repeats the missing videos in its Video column) is the work.
    """
    if not rows:
        return
    if collapsed:
        fold = ui.expansion(title).classes("section fold-tight w-full mb-4").props("dense")
        with fold:
            ui.label(explain).classes("explain")
            _table(columns, rows)
        return
    theme.subhead(title, explain=explain)
    _table(columns, rows)


def _table(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    table = ui.table(columns=columns, rows=rows, row_key=_ROW, pagination=0)
    table.classes("w-full mt-2 mb-8").props("dense flat bordered")


class _Selection:
    """The ticked rows, as a set of row keys that filtering never touches.

    **This is the Google Sheets model, not the Excel one.** A tick is a property of a *row*, not of
    what happens to be on screen. Filtering changes the view and nothing else; a bulk tick or
    untick applies to the rows the filter is showing; the running total counts the whole file,
    visible or not. So: 100 ticked, filter to 30, untick those → 70. Or nothing ticked, filter to
    20, tick all → 20.

    Held in Python rather than read off ``table.selected``, because that list only ever holds rows
    the table is currently rendering. Filter a ticked row out of view and it drops out of
    ``selected``; save then, and the operator loses rows they never touched — silently, since the
    count would agree with itself the whole way down.
    """

    def __init__(self, keys: set[int]) -> None:
        self.keys = keys

    def sync_from(self, visible: list[dict[str, Any]], selected: list[dict[str, Any]]) -> None:
        """Fold a table event back in: only the visible rows can have changed."""
        shown = {int(row[_ROW]) for row in visible}
        self.keys = (self.keys - shown) | {int(row[_ROW]) for row in selected}

    def add(self, rows: list[dict[str, Any]]) -> None:
        self.keys |= {int(row[_ROW]) for row in rows}

    def remove(self, rows: list[dict[str, Any]]) -> None:
        self.keys -= {int(row[_ROW]) for row in rows}


#: How many distinct values a column may hold before its filter becomes free text. Below this a
#: picker is better — the operator sees what the column *contains*, which is most of why they
#: filter; above it, a list of 300 barcodes is a worse way to find one than typing four digits.
_PICKER_MAX = 12

#: What a blank cell is called in a filter, in both controls. A column in the operator's own file
#: is routinely part empty, and those rows are usually the point — "not on the website yet" is a
#: blank, not a word.
BLANK_LABEL = "(blank)"


def _scope_table(
    columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, untick: set[int]
) -> _Grid:
    """The rows a run will act on, all ticked, filterable per column.

    One box matching every column was the whole filter. It is still here — it is the fastest way
    to find one barcode and nothing per-column replaces it — but it could not answer the question
    the operator actually has, which is "show me the rows where *this* column says *that*".
    """
    theme.subhead(
        f"Your products ({len(rows)}) — tick the ones in this batch",
        explain=(
            "Every row of your list. The saved batch arrives ticked — every row, for a list just "
            "uploaded. Untick a product to leave it out. Status says what a run would do with it, "
            "and Detail why; the tables under this one list the problems of the ticked products "
            "only, and Next saves the ticked products that can run. Filters change only what you "
            "can see, never what is ticked, and the tick buttons act on the rows the filters are "
            "showing — so you can filter to twenty rows, untick all twenty, clear the filters, and "
            "the other eighty are exactly as you left them."
        ),
    )
    selection = _Selection({int(row[_ROW]) for row in rows} - untick)
    # Created before the table so they render above it. You filter, then look — controls under the
    # thing they control are read as a footer, and on a table this tall they are off screen.
    filters = ui.row().classes("items-end gap-3 w-full flex-wrap mt-3")
    bulk = ui.row().classes("items-center gap-3 mt-2 mb-1 flex-wrap")
    all_columns = columns
    table = ui.table(
        columns=all_columns,
        rows=list(rows),
        row_key=_ROW,
        selection="multiple",
        # Mandatory, not cosmetic: with pagination on, the header checkbox selects *this page*,
        # and a save would then quietly drop every row the operator never scrolled to.
        pagination=0,
    ).classes("w-full mt-2")
    table.props(f'dense flat bordered virtual-scroll style="height: {_TABLE_HEIGHT}"')
    table.classes("sticky-head")  # the column names stay visible while the rows scroll

    #: Column field -> the operator's filter for it. Read on every redraw; empty means "no filter".
    per_column: dict[str, Any] = {}
    search: Any = None
    listeners: list[Callable[[], None]] = []

    def visible() -> list[dict[str, Any]]:
        """The rows every active filter admits — all of them, ANDed."""
        text = str(getattr(search, "value", "") or "").strip().lower()
        kept = []
        for row in rows:
            if text and not any(text in str(value).lower() for value in row.values()):
                continue
            if all(_admits(per_column.get(field), row.get(field)) for field in per_column):
                kept.append(row)
        return kept

    def redraw() -> None:
        shown = visible()
        table.rows = shown
        # Rebuilt from the selection, never from what the table was showing a moment ago: the two
        # disagree the instant a filter hides a ticked row, and the table's copy is the lossy one.
        table.selected = [row for row in shown if int(row[_ROW]) in selection.keys]
        table.update()
        for listen in listeners:
            listen()

    def on_table_select() -> None:
        selection.sync_from(visible(), list(table.selected))
        for listen in listeners:
            listen()

    table.on_select(on_table_select)

    with filters:
        search = (
            ui.input(placeholder="Find in any column")
            .props("dense clearable")
            .classes("w-full max-w-xs")
        )
        search.on_value_change(redraw)
        for column in all_columns:
            per_column[column["field"]] = _column_filter(column, rows, redraw)

    with bulk:
        tick = ui.button("Tick all shown", on_click=lambda: (selection.add(visible()), redraw()))
        untick = ui.button(
            "Untick all shown", on_click=lambda: (selection.remove(visible()), redraw())
        )
        for button in (tick, untick):
            button.props("flat dense no-caps")
        shown_label = ui.label("").classes("note")

    def describe_shown() -> None:
        count = len(visible())
        ticked = sum(1 for row in visible() if int(row[_ROW]) in selection.keys)
        shown_label.text = (
            f"{count} of {len(rows)} row(s) shown; {ticked} of those ticked."
            if count != len(rows)
            else f"All {len(rows)} row(s) shown; {ticked} ticked."
        )

    listeners.append(describe_shown)
    redraw()
    return _Grid(selection, rows, listeners, redraw)


class _Grid:
    """What the save and the caption need from the table, without reaching into the widget."""

    def __init__(
        self,
        selection: _Selection,
        rows: list[dict[str, Any]],
        listeners: list[Callable[[], None]],
        redraw: Callable[[], None],
    ) -> None:
        self._selection = selection
        self._rows = rows
        self._listeners = listeners
        #: Re-render the rows from the selection — after a cell changed under it, say.
        self.redraw = redraw

    @property
    def selected(self) -> list[dict[str, Any]]:
        """Every ticked row in the file — not merely the ticked rows on screen."""
        return [row for row in self._rows if int(row[_ROW]) in self._selection.keys]

    def on_select(self, handler: Callable[[], None]) -> None:
        self._listeners.append(handler)
        handler()


def _column_filter(column: dict[str, Any], rows: list[dict[str, Any]], redraw: Any) -> Any:
    """One column's filter: a value picker when it has few values, free text when it has many.

    A picker is what the operator means by "filter on column D" — they want to see what D
    contains. It stops being that the moment the column is a barcode or a description, where the
    list is as long as the file and typing four characters is faster than finding one entry in
    three hundred.

    **An empty cell is one of the things a column contains**, and both controls can ask for it
    under the same name. This shipped building the picker's options from the column's values *minus*
    the empty one, which made blanks the single thing in a column that could not be filtered for —
    on the pilot's own list that is 52 rows of "Momenteel op Website", 16 of "Al in Gs1" and 81 of
    "Link naar site", and "show me the ones nobody has done yet" is how a batch gets prepared.
    """
    field = column["field"]
    label = str(column["label"])
    options = _picker_options(rows, field)
    if options is not None:
        control = (
            ui.select(options, multiple=True, label=label, clearable=True)
            .props("dense outlined use-chips")
            .classes("min-w-40")
        )
    else:
        control = ui.input(placeholder=label).props("dense clearable outlined").classes("w-40")
        if any(not cell for cell in _cells(rows, field)):
            # Typing cannot express "empty" — every string is a substring of nothing — so this
            # control takes the picker's word for it. On hover, because a column this wide has no
            # room to say it and the sentence belongs with the control rather than in a paragraph.
            control.tooltip(f"Type {BLANK_LABEL} to show only the rows where this column is empty")
    control.on_value_change(redraw)
    return control


def _cells(rows: list[dict[str, Any]], field: str) -> list[str]:
    """One column's cells as the filters compare them: text, stripped, blank for absent."""
    return [str(row.get(field) or "").strip() for row in rows]


def _picker_options(rows: list[dict[str, Any]], field: str) -> dict[str, str] | None:
    """A column's picker options, or ``None`` when it has too many values to enumerate.

    Pure, and separate from the widget, so the decision and the options are testable without a
    browser — which is what the blank option needed, since getting it wrong is invisible: a filter
    that silently matches nothing looks exactly like a column with nothing in it.

    Keyed by the cell value and labelled for a person. The blank option's value is the **empty
    string** — what a blank cell compares equal to — so :func:`_admits` needs no case of its own.
    Quasar round-trips it as ``[""]``, measured in a browser rather than assumed, because an option
    the widget quietly dropped would filter to nothing while looking selected.
    """
    cells = _cells(rows, field)
    values = sorted(set(cells) - {""})
    if len(values) > _PICKER_MAX:
        return None
    # No blank option for a column that has none: an option matching nothing can only mislead.
    options = {"": BLANK_LABEL} if "" in cells else {}
    options.update({value: value for value in values})
    return options


def _admits(control: Any, value: Any) -> bool:
    """Whether one column's filter lets a cell through. No filter admits everything."""
    chosen = getattr(control, "value", None)
    if not chosen:
        return True
    cell = str(value or "").strip()
    if isinstance(chosen, list):
        # The picker's blank option carries the empty string, which is exactly what a blank cell
        # reads as — so asking for blanks is the ordinary path here, not a special case.
        return cell in chosen
    needle = str(chosen).strip()
    if needle == BLANK_LABEL:
        # The one typed string that is not a substring search. A column whose cells literally read
        # "(blank)" would be unsearchable for that word, which is a trade worth making: the word is
        # the picker's own, so the two controls cannot mean different things by it.
        return not cell
    return needle.lower() in cell.lower()
