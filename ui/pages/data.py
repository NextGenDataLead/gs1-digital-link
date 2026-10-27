"""The batch: what it is made of, what each product still needs, and which products are in it.

**The selection list is the spine.** Everything else on this screen is measured against it, in the
order an operator assembles a batch:

1. the **product selection list** — which barcodes this batch may touch;
2. the **GS1 Data Source export** — the product data, parsed into ``products.json``;
3. the client's **video sign-off sheet** — which video is which product's, applied to
   ``mapping.yml``. The mapping itself is edited in that file, never on this screen;

side by side, because they are three documents arriving from three places and none waits on
another. Under them: what the sign-off sheet would change, the **coverage** funnel — in product
list → eligible → selected — and

4. **choose and save** — the list split into not in the export, not eligible (and why), missing
   video(s), and the eligible products, the only ones with a tick box (:mod:`ui.batch_grid`).

They are different documents from different places, and confusing them is the most expensive
mistake available here, so each has its own upload and its own name — and the sign-off upload
refuses a GS1 export outright. The config key is still ``process_list``: it appears in
``clients.yml``, the schema, the doctor payload and five call sites, and renaming it would break
every install. Only what the operator reads changed.

**Both batch uploads go to the configured path, never to a new one.** Neither ``parse_export`` nor
the list reader has an input-path override, so a file dropped anywhere else is invisible to the
tool. Writing to the configured path is what makes an upload mean anything.

**Everything is remembered between sittings** — the batch is read from disk — so the way to start
over is *Clear all — start fresh*, which sets this batch's live inputs aside
(:mod:`lib.batch_reset`). The client's video mapping is never part of that: it outlives batches.

**A row is ticked to keep it.** The previous version of this screen had the opposite verb — a tick
meant *remove this* — so no control here may carry the old wording, and the caption above Next
says what the save will do before it is pressed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from nicegui import events, ui

from lib import (
    batch_reset,
    input_layout,
    issue_report_files,
    list_columns,
    provenance,
    ticked_snapshot,
    video_signoff,
)
from lib.config import ClientConfig, ProcessListConfig
from lib.eligibility import eligibility_for, link_targets
from lib.errors import ProcessListError
from lib.gates import PERMANENCE_WARNING, REVERSIBLE_NOTE, Mode
from lib.input_layout import export_archive_path, write_readme
from lib.issue_report import Issue, IssueReport
from lib.link_targets import check_targets, host_problem
from lib.process_list import ProcessListSheet, listed_urls
from ui import (
    DATA_ROOT,
    batch_grid,
    context,
    handover,
    process_list_edit,
    progress,
    runner,
    theme,
    video_map_panel,
    video_signoff_panel,
)


@dataclass
class _Session:
    """The one thing about this screen that is genuinely about *this sitting*, and not about disk.

    It used to hold three booleans — export arrived, list arrived, selection saved — and the first
    two gated whether the grid appeared at all. That made the batch **invisible and live at the same
    time**: restarting the shell hid the selection while a run went on consuming it, as the band
    that replaced the grid admitted in so many words.

    So arrival is no longer remembered. What is in force is read from the files, by
    :func:`lib.batch.in_force`, which is what a run reads too — and the risk the old rule was
    reaching for, a batch whose ticks were chosen against a *different* export, is now detected and
    said out loud instead of being hidden behind an absent grid.

    ``saved`` stays, because it is not a fact about disk: it answers "did *I*, in this sitting,
    already choose a batch?", which is what makes "this replaced the selection you saved earlier"
    true or false. A file's timestamp cannot tell you that.
    """

    saved: bool = False


#: One per client, for the life of the process. See :class:`_Session`.
_BATCHES: dict[str, _Session] = {}

#: The rows unticked in step 5, per client, for the life of the process — see :mod:`ui.batch_grid`.
_TICKS: dict[str, batch_grid.Ticks] = {}


@dataclass
class _Mode:
    """What this batch publishes, as picked in step 4 — saved only by Next, with the selection.

    Seeded from the saved batch the first time the screen draws, so a restart shows the choice the
    batch was saved with. ``None`` until one is picked: a batch saved before the choice moved here
    has none, and the screen asks rather than assuming ``pages``.
    """

    value: Mode | None = None
    seeded: bool = False


#: One per client, for the life of the process. See :class:`_Mode`.
_MODES: dict[str, _Mode] = {}


def _forgets_mode(which: str) -> bool:
    """Whether a change to the batch clears step 4's choice.

    Clearing sets aside the selection the choice was saved with. A new product list starts a new
    batch, and a new batch is described afresh: inheriting the last one's mode is how a links-only
    choice once sat waiting under a batch meant for both (operator, 2026-10-21). A new export
    re-checks the same batch, so it keeps the choice.
    """
    return which in {"list", "cleared"}


#: What each link target answered, for the life of the process: ``url -> None`` (it loads) or the
#: problem. Checked once per address and then remembered, so ticking does not re-fetch forty pages.
#: A fixed page is seen again after a restart — or by the run, which checks every target anyway.
_LINK_CHECKS: dict[str, str | None] = {}


def _resolve(path: str) -> Path:
    """A configured path, against the data folder — every path in clients.yml is relative."""
    resolved = Path(path)
    return resolved if resolved.is_absolute() else DATA_ROOT / resolved


def render() -> None:  # noqa: PLR0915 — the wiring: four redraws share one set of containers
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Data",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Data"),
            "Data",
            "What a batch is made of, what each product still needs, and which ones are in it.",
        )
        if cfg is None or cid is None:
            theme.blocked(
                "clients.yml did not load, so this screen has nothing to work from.",
                link_label="Open Setup →",
                route="/setup",
            )
            return

        session = _BATCHES.setdefault(cid, _Session())
        ticks = _TICKS.setdefault(cid, batch_grid.Ticks())
        mode = _MODES.setdefault(cid, _Mode())
        if not mode.seeded:
            mode.value = context.batch_mode(cfg)
            mode.seeded = True
        #: How many products step 5 has ticked — Next needs that *and* a mode.
        ticked = [0]
        mapping = video_map_panel.session_for(cid, cfg)
        #: The grid's save, hoisted so the Next button can call it. Empty until there is a grid.
        commit: dict[str, Callable[[], bool]] = {}
        ready = False

        theme.jumps(
            [
                ("Uploads", "uploads"),
                ("Publishes", "publishes"),
                ("Coverage", "coverage"),
                ("Choose", "choose"),
                ("Issue report", "issue-report"),
            ]
        )

        def batch_changed(which: str) -> None:
            """A list or an export arrived, or the batch was cleared: redraw from **disk**.

            A new list (or none) renumbers every row, so the remembered unticks go with it.
            """
            nonlocal ready
            if which in {"list", "cleared"}:
                ticks.unticked.clear()
                ticks.seeded = False
            if _forgets_mode(which):
                mode.value = None
                draw_mode()
            in_force = context.batch_in_force(cfg)
            ready = in_force is not None and in_force.ready
            # Disabled here; the grid turns it on once a product is ticked — see ``draw_choose``.
            onward.disable()
            draw_choose()
            report_changed()

        def mapping_changed() -> None:
            """The sign-off sheet was applied: eligibility (a video can unblock a product) and the
            report. Ticks survive — :class:`ui.batch_grid.Ticks` outlives the rebuild."""
            draw_choose()
            report_changed()

        def report_changed() -> None:
            """The issue report follows the ticks — step 5 redraws it — so a sheet arriving alone
            changes nothing here. Clear it when there is no batch to report on."""
            if not ready:
                quality.clear()

        def reported(built: IssueReport) -> None:
            quality.clear()
            with quality:
                _issue_report(cid, built)

        def draw_choose() -> None:
            selection.clear()
            commit.clear()
            coverage.clear()
            with selection:
                if ready:
                    # Next only once something is ticked: nothing ticked is nothing to save, and a
                    # button that is pressable and then refuses is a button that seems broken.
                    _choose(
                        cfg,
                        cid,
                        commit,
                        caption,
                        ticks,
                        coverage,
                        mode,
                        chosen=count_ticked,
                        recheck=draw_choose,
                        reported=reported,
                    )
                else:
                    caption.text = ""
                    theme.band(
                        "Upload the selection list and the export (steps 1 and 2) to choose the "
                        "products for this run.",
                        "quiet",
                    )

        def count_ticked(count: int) -> None:
            ticked[0] = count
            unlock()

        def unlock() -> None:
            # Next only once something is ticked *and* the batch says what it publishes: nothing
            # ticked is nothing to save, and a batch with no mode is a run nobody has described.
            onward.set_enabled(ticked[0] > 0 and mode.value is not None)

        def mode_picked(value: Mode) -> None:
            mode.value = value
            draw_mode()
            # Ticks survive; what each product needs depends on the mode, so step 5 redraws.
            draw_choose()
            unlock()

        def draw_mode() -> None:
            publishes.clear()
            if cfg.process_list is None:
                return  # no selection to save it with — Publish asks, as it always did
            with publishes:
                _mode_section(mode.value, mode_picked)

        signoff_area: list[ui.column] = []
        list_area: list[ui.column] = []
        with ui.element("div").classes("steps-3up").props("id=uploads"):
            _scope_list(cfg, session, batch_changed, list_area)
            _export(cfg, cid, batch_changed)
            _signoff(cfg, cid, mapping, mapping_changed, report_changed, signoff_area)
        _clear_all(cfg, cid, session, batch_changed)
        list_area.append(ui.column().classes("w-full mt-8"))
        signoff_area.append(ui.column().classes("w-full mt-8"))

        publishes = ui.column().classes("w-full gap-0")
        draw_mode()

        coverage = ui.element("section").classes("section").props("id=coverage")
        selection = ui.column().classes("w-full gap-0")
        quality = ui.column().classes("w-full gap-0")

        def save_and_go() -> None:
            # One intention, one button. On a screen with unsaved work, "go on" and "commit what
            # I chose" are the same act, and two buttons is how the second gets missed.
            if cfg.process_list is not None and mode.value is None:
                theme.notify_warning("Choose what this batch publishes first — step 4.")
                return
            save = commit.get("save")
            if save is not None and not save():
                return  # refused, and it said why — stay put rather than carry the refusal away
            # Straight on. There was a four-second beat here so the one-word "Saved" could be
            # read before the page changed, and it read as a click that did nothing — the
            # operator clicked again and took the second click for the one that worked. Content
            # opening is the receipt.
            # A links-only batch writes no page, so it has no copy to write: straight to Preflight.
            opened = progress.of(cid).advance("/data", skip=progress.not_needed(mode.value))
            ui.navigate.to(opened or "/content")

        onward, caption = theme.onward("Next", save_and_go)
        batch_changed("")


# --- Step 5 and the funnel ---------------------------------------------------------


def _choose(  # noqa: PLR0913 — the client, the save, its caption, the ticks, the funnel, the mode
    cfg: ClientConfig,
    cid: str,
    commit: dict[str, Callable[[], bool]],
    caption: ui.label,
    ticks: batch_grid.Ticks,
    coverage: ui.element,
    mode: _Mode,
    *,
    chosen: Callable[[int], None],
    recheck: Callable[[], None],
    reported: Callable[[IssueReport], None],
) -> None:
    """Read the list and the export, decide eligibility once, and build step 5 under it.

    ``chosen`` hears how many ticked products can run, every time that changes. ``recheck``
    rebuilds this step — called once the link checks of a links-only batch come back.
    """
    assert cfg.process_list is not None  # a batch is only ready with a list
    try:
        batch = process_list_edit.read_sheet(cfg.process_list)
        sheet = _uploaded(cfg.process_list, batch)
    except ProcessListError as exc:
        theme.band(str(exc), "danger")
        return
    products = context.load_products(cid)
    named = {sheet.gtin14_at(index) for index in range(len(sheet.rows))}
    # Eligibility over the products *this list* names — the same set ``in_scope`` would give once
    # the list is saved, read from the sheet on screen so a fresh upload is judged at once.
    listed = [product for product in products if product.gtin14 in named]
    links_only = mode.value is Mode.LINKS
    targets: dict[str, str] = {}
    if links_only:
        targets = link_targets(
            cfg, listed_urls(sheet, cfg.process_list.target_url_column), context.load_ledger(cid)
        )
        targets = {gtin: url for gtin, url in targets.items() if gtin in named}
        _check_links(cfg, targets, recheck)
    verdict = eligibility_for(cfg, listed, mode.value, targets=targets, checked=_LINK_CHECKS)

    def record(saved: Path, chosen: ProcessListSheet) -> None:
        # Which export these ticks were made against — so a run can say what it was chosen from.
        export = _resolve(cfg.export.path)
        assert cfg.process_list is not None
        provenance.record_selection(
            provenance.history_path(export),
            _resolve(str(saved)),
            product_list=input_layout.archive_path(_resolve(cfg.process_list.path)),
            export=export,
            rows=len(chosen.rows),
            mode=mode.value.value if mode.value is not None else None,
        )

    def counted(counts: batch_grid.Funnel) -> None:
        batch_grid.draw_funnel(coverage, counts, links_only=links_only)
        chosen(counts.can_run)

    def report(issues: list[Issue], products: list[tuple[str, str]]) -> None:
        what = mode.value.value if mode.value is not None else "no run type chosen yet"
        reported(
            IssueReport(
                title=f"Product issues - {cfg.display_name}",
                subtitle=f"Selection of {len(products)} product(s) - {what} - before the run.",
                selected=tuple(gtin for gtin, _ in products),
                issues=tuple(issues),
            )
        )

    with theme.section(
        "Choose the products and save",
        step=5,
        anchor="choose",
        explain=(
            "Every product on your list, with what a run would do with it. Your saved batch "
            "arrives ticked, or every row for a list just uploaded; untick a product to leave it "
            "out. Under the list, the problems of the ticked products — and Coverage above counts "
            "the ticked products too. Next saves the ticked products that can run — exactly "
            "those, and nothing else, are what the next screens work on. Every save is kept, "
            "dated, under process/selection/. Nothing is published here."
        ),
    ):
        batch_grid.choose(
            sheet,
            products,
            verdict,
            record=record,
            commit=commit,
            caption=caption,
            ticks=ticks,
            counted=counted,
            saved=_ticked_last_time(batch),
            target=batch.path,
            links_only=links_only,
            report=report,
        )


def _ticked_last_time(batch: ProcessListSheet) -> frozenset[str]:
    """The barcodes ticked when this selection was saved — the saved selection itself when no
    snapshot was kept with it. Next saves only the products that can run, so without this a ticked
    product that could not run would come back unticked, and this screen's report would no longer
    be the run's (operator, 2026-10-09: the two are the same report)."""
    ticked = ticked_snapshot.read(ticked_snapshot.path_for(batch.path), batch.path)
    if ticked is None:
        return batch.listed_gtins()
    return frozenset(gtin for gtin, _ in ticked.products)


def _check_links(cfg: ClientConfig, targets: dict[str, str], done: Callable[[], None]) -> None:
    """Check the addresses not yet checked, off the event loop, then rebuild step 5 once.

    Only the ones on the client's own host: an address elsewhere is refused for that alone and
    is not fetched. Until the answers arrive those products read *checking link* and cannot run.
    """
    pending = sorted(
        {
            url
            for url in targets.values()
            if url not in _LINK_CHECKS and host_problem(url, cfg.wordpress.site_url) is None
        }
    )
    if not pending:
        return

    async def check() -> None:
        found = await asyncio.to_thread(check_targets, pending)
        _LINK_CHECKS.update(found)
        # The screen may have been left while the pages were fetched; the answers are kept.
        with contextlib.suppress(RuntimeError):
            done()

    ui.timer(0, check, once=True)


#: The order step 4 offers them in (operator, 2026-10-09): both, links, pages.
_MODE_ORDER: Final = (Mode.BOTH, Mode.LINKS, Mode.PAGES)


def _mode_section(current: Mode | None, picked: Callable[[Mode], None]) -> None:
    """Step 4: what this batch publishes — pages, GS1 links, or both.

    Asked here, before anything is counted, because it decides what a product needs: a links-only
    batch writes no page, so it needs no copy and no video, and skips the Content step. Saved with
    the selection by Next; the Publish screen shows it again and asks for a confirmation before
    anything runs — a choice remembered is not a run approved.
    """
    with theme.section(
        "What this batch publishes",
        step=4,
        anchor="publishes",
        explain=(
            "Both: the product pages, then the GS1 links pointing at them. Links: the GS1 Digital "
            "Link record and QR code for each product, pointing at its page — permanent. Pages: "
            "the product pages on the website, reversible. Saved with your selection when you "
            "press Next; the Publish screen asks you to confirm it before anything runs."
        ),
    ):
        ui.toggle(
            {mode.value: f"{mode.value} — {mode.summary}" for mode in _MODE_ORDER},
            value=current.value if current is not None else None,
            on_change=lambda event: picked(Mode(event.value)) if event.value else None,
        ).props("no-caps").classes("mb-3")
        if current is None:
            theme.band("Choose one — Next waits for it.", "warn")
        elif current.is_permanent:
            theme.band(PERMANENCE_WARNING, "danger")
        else:
            theme.band(REVERSIBLE_NOTE)
        if current is Mode.LINKS:
            ui.label(
                "A links-only batch writes no page, so the Content step is passed over."
            ).classes("note")


def _uploaded(config: ProcessListConfig, batch: ProcessListSheet) -> ProcessListSheet:
    """The list as it arrived, which the grid shows whatever was saved since.

    The saved batch holds only the ticked rows, so drawing the grid from it would show three rows
    after a restart and lose the rest until the list was uploaded again. The upload is kept beside
    it from the moment it lands; a control file placed by hand has none until its first save, and
    then it *is* the list as it arrived.
    """
    upload = input_layout.archive_path(_resolve(config.path))
    if not upload.is_file():
        return batch
    return process_list_edit.read_sheet(
        ProcessListConfig(path=str(upload), gtin_column=config.gtin_column)
    )


# --- Step 3: the videos ------------------------------------------------------------


def _signoff(  # noqa: PLR0913 — the client, the mapping, what to redraw, and where the review goes
    cfg: ClientConfig,
    cid: str,
    mapping: video_map_panel.MappingSession | None,
    changed: Callable[[], None],
    archived: Callable[[], None],
    plan_into: list[ui.column],
) -> None:
    """Step 3, the upload only — its review renders full width below the row (``plan_into``)."""
    if mapping is None:
        with theme.section("The client's video sign-off sheet", step=3, anchor="video-signoff"):
            ui.label(
                "No `media.video_map_path` in clients.yml — this client attaches no videos, so "
                "there is nothing to sign off."
            ).classes("note")
        return
    video_signoff_panel.render(
        cfg, cid, mapping, applied=changed, archived=archived, step=3, plan_into=plan_into
    )


# --- Clear all ---------------------------------------------------------------------


def _clear_all(
    cfg: ClientConfig, cid: str, session: _Session, cleared: Callable[[str], None]
) -> None:
    """*Clear all — start fresh*, behind a confirmation that says exactly what moves.

    Everything on this screen is remembered between sittings, because it is read from disk — so
    starting over needs a way to set the batch aside. This moves the live list, selection, export
    and parsed products into ``superseded/cleared-{stamp}/`` (:mod:`lib.batch_reset`); nothing is
    deleted, and the client's video mapping is never touched.
    """
    if cfg.process_list is None:
        return
    process_list = cfg.process_list

    def clear() -> None:
        dialog.close()
        try:
            moved = batch_reset.clear_batch(
                export=_resolve(cfg.export.path),
                selection=_resolve(process_list.path),
                products=DATA_ROOT / "output" / cid / "data" / "products.json",
                stamp=datetime.now(UTC).strftime("%Y%m%dT%H%M%S"),
            )
        except (OSError, ValueError) as exc:
            theme.notify_problem(f"Nothing was cleared: {exc}")
            return
        session.saved = False
        cleared("cleared")
        if not moved:
            theme.notify_ok("There was nothing to clear.")
            return
        theme.announce(
            "Cleared — start fresh",
            f"{len(moved)} file(s) set aside in {moved[0].parent.relative_to(DATA_ROOT)}. Upload "
            "the selection list and the export to begin a new batch. The video mapping is as it "
            "was.",
        )

    with ui.dialog() as dialog, ui.element("div").classes("card dialog-card"):
        ui.label("Clear this batch and start fresh?").classes("section-head")
        ui.label(
            "The selection list, the saved selection, the GS1 export and the products read from "
            "it are moved to input/…/superseded/cleared-<date>/ — nothing is deleted, and every "
            "dated copy stays where it is. The video mapping, the client's sign-off sheets, "
            "generated copy, run history and everything live are not touched."
        ).classes("note mt-2")
        with ui.row().classes("gap-3 mt-4"):
            theme.quiet_action("Cancel", dialog.close)
            theme.action("Clear all", clear)

    with ui.row().classes("items-center gap-3 mt-4"):
        theme.quiet_action("Clear all — start fresh", dialog.open)
        ui.label(
            "Everything here is remembered between sittings. This sets the batch aside so the "
            "screen is empty again."
        ).classes("note")


# --- Step 2: the export -------------------------------------------------------


def _keep_upload(target: Path, data: bytes) -> Path | None:
    """Keep this export upload forever, dated, beside the live file. Returns where, or ``None``.

    ``process/uploads/GS1 export/export-{stamp}.xlsx``, named by
    :func:`lib.input_layout.export_archive_path` rather than here — this had its own copy of the
    naming and its own collision loop, which is four places that spell one layout and the reason
    two of them were wrong for a week.

    Written on the way *in* — the moment the file has proved readable — rather than when the next
    upload displaces it. Archiving on replacement means a file uploaded once and never replaced has
    no dated copy at all, which is the ordinary case for a quarterly export.

    Best-effort, and now actually so: this said "best-effort" while letting an ``OSError`` out into
    ``theme.upload``, which shows the exception and re-raises — so an upload that had already
    landed and parsed would report as failed because a *copy* of it could not be filed.

    ``None`` is not swallowed: the caller says so in the sentence it leaves on screen. There is no
    logger anywhere under ``ui/``, and the handler's return value *is* this screen's way of
    reporting — a failure nobody is told about would leave the operator believing they have a dated
    copy of an export they do not.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        kept = export_archive_path(target, datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
        kept.write_bytes(data)
    except OSError:
        return None
    return kept


def _export(cfg: Any, cid: str, arrived: Callable[[str], None]) -> None:
    target = _resolve(cfg.export.path)

    with theme.section(
        "Upload the GS1 export",
        step=2,
        anchor="export",
        explain=(
            f"The product data itself, straight from GS1 Data Source. Uploading replaces "
            f"{cfg.export.path} in place and keeps the previous file beside it. That path is fixed "
            "in clients.yml and has no command-line override, so a workbook saved anywhere else is "
            "invisible to the tool — the single most common way a run quietly uses last quarter's "
            "data. The file is read as soon as it arrives: if it is not a GS1 Data Source export, "
            "or an attribute the pipeline needs is absent, it is put back and nothing changes."
        ),
    ):
        problems = ui.log().classes("console mt-3").style("display:none")

        # Async because NiceGUI 3 reads an upload through awaitable methods on ``event.file``. The
        # 2.x form — a synchronous ``event.content.read()`` — raised AttributeError *inside* the
        # handler, where NiceGUI logs it and the browser still shows a completed upload: it wrote
        # nothing while looking exactly like success.
        async def receive(event: events.UploadEventArguments) -> str:
            problems.style("display:none")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Held in memory rather than written to a ``.bak``: it exists only to put the previous
            # export back if this one will not parse, which is a rollback and not an archive. The
            # archive is the dated copy below, written only once the file has proved readable —
            # an unreadable upload is not one of the operator's originals, it is a mistake.
            previous = target.read_bytes() if target.exists() else None
            await event.file.save(target)

            # Reading it *is* the check. There was a "check it" button and a "read it" button, and
            # the second was the one that mattered, so the first was a step an operator could skip
            # into a run built on a file nobody had opened.
            result = await runner.run_off_the_loop(runner.parse_export_argv(cid))
            if result.ok:
                kept = _keep_upload(target, target.read_bytes())
                # What the operator called it, before the upload renamed it to export.xlsx. Without
                # this, "which export did that run use?" can only ever be answered with a timestamp.
                provenance.record_upload(
                    provenance.history_path(target),
                    "export",
                    kept=kept,
                    given_name=event.file.name,
                    rows=context.product_count(cid),
                )
                write_readme(cfg)
                # The selection below is a join against this export, so it now describes a
                # different one — and this is the upload that unlocks it.
                arrived("export")
                theme.notify_ok("Export read.")
                read = f"{context.product_count(cid) or 0} products read from this export."
                if kept is None:
                    return f"{read} A dated copy of it could not be written beside it."
                return read

            # Put the old one back. A failed read that leaves the bad file in place would mean the
            # next screen describes a workbook nobody can use, with no way back but a re-upload of
            # a file the operator may no longer have.
            if previous is not None:
                target.write_bytes(previous)
            problems.style("display:block")
            problems.clear()
            problems.push(result.stderr or result.stdout or "(no output)")
            theme.notify_problem(
                "That file could not be read as a GS1 Data Source export, so it was put back. "
                "The reason is on screen."
            )
            return "Not read — the previous export is still in place."

        theme.upload(
            "GS1 Data Source export (.xlsx)",
            receive,
            busy="Reading the export — this can take a moment…",
        )


# --- Step 1: the selection list --------------------------------------------------
#
# Named "Process list" on screen until two renames ago, which is the config key. It is a
# **selection**: which of the exported products this batch touches.


def _scope_list(
    cfg: Any, session: _Session, arrived: Callable[[str], None], columns_into: list[ui.column]
) -> None:
    if cfg.process_list is None:
        with theme.section("Choose the products for this batch", step=1, anchor="selection-list"):
            ui.label(
                "No `process_list` block in clients.yml — every product in the export is planned."
            ).classes("note")
        return

    control = _resolve(cfg.process_list.path)

    with theme.section(
        "Upload the product selection list",
        step=1,
        anchor="selection-list",
        explain=(
            "A spreadsheet of the barcodes this batch may touch. Being on the list is the whole "
            "meaning — the tool reads no other column and interprets no cell value — so you "
            "prepare a batch by ticking rows below, and your own columns are kept exactly as they "
            f"are. It is saved as {cfg.process_list.path}, and your upload is kept under "
            f"uploads/ as {process_list_edit.archive_path(control).name}. Every run copies that "
            "file into its own folder, which is what lets the result sheet afterwards name the "
            "rows you dropped as dropped rather than leaving them out. "
            "That path is fixed in clients.yml and has no command-line override, so a list saved "
            "anywhere else is invisible to the tool. Until the export (step 2) arrives, nothing "
            "can be matched, so step 5 waits for both."
        ),
    ):
        process_list: ProcessListConfig = cfg.process_list

        def install(data: bytes, installed: bytes | None, given_name: str) -> str:
            kept = process_list_edit.archive(process_list, data, installed)
            provenance.record_upload(
                provenance.history_path(_resolve(cfg.export.path)),
                "product-list",
                # The dated copy, not the stable name: a selection months from now points at the
                # document that will still be there, and ``product-list.xlsx`` is replaced by the
                # next upload.
                kept=next(iter(input_layout.list_archives(control)), None),
                given_name=given_name,
                rows=len(process_list_edit.read_sheet(process_list).rows),
            )
            # Redrawn rather than left for the operator to reload: the tables below now describe
            # the file that was just replaced, and a screen that keeps showing the previous list
            # after a successful upload is the silent staleness this project designs against.
            write_readme(cfg)
            replaced, session.saved = session.saved, False
            arrived("list")
            theme.notify_ok("Selection list installed.")
            if replaced:
                return "Installed — this replaced the selection you saved earlier."
            return f"Installed. Your upload is kept as {kept.name}."

        # Async because NiceGUI 3 reads an upload through awaitable methods on ``event.file`` —
        # see the export upload above for what the 2.x form did instead.
        async def receive(event: events.UploadEventArguments) -> str:
            # Read before anything is installed. A list headed the configured way installs at once,
            # read with the run's own reader, so a file that would fail on Preflight is refused
            # while the operator is still looking at the picker. Any other list waits for the
            # operator to say which column is the barcode (:mod:`lib.list_columns`).
            data = await event.file.read()
            name = event.file.name
            grid = list_columns.read_upload(data, process_list)
            if grid is None:
                raise ProcessListError(
                    "this file has no table to read: no row of headings was found. The list you "
                    "were using has not been touched."
                )
            barcode, link = list_columns.guess(grid, process_list)
            as_is = barcode is not None and list_columns.is_as_configured(
                grid, list_columns.ListColumns(barcode, link), process_list
            )

            def install_as(chosen: list_columns.ListColumns) -> str:
                problem = list_columns.barcode_problem(grid, chosen)
                if problem is not None:
                    raise ProcessListError(problem)
                header = list_columns.normalised_header(grid, chosen, process_list)
                renamed = (
                    None
                    if as_is and header == grid.header
                    else process_list_edit.normalised_workbook(header, grid.rows)
                )
                return install(data, renamed, name)

            said = (
                install(data, None, name)
                if as_is
                else ("Read. Which column is the barcode? Choose it below, then install.")
            )
            _list_columns(columns_into[0], grid, (barcode, link), process_list, as_is, install_as)
            return said

        theme.upload("Product selection list (.xlsx)", receive, busy="Checking the list…")


def _list_columns(  # noqa: PLR0913 — where it goes, what was read, what was guessed, and how to install
    area: ui.column,
    grid: Any,
    guessed: tuple[int | None, int | None],
    config: ProcessListConfig,
    installed: bool,
    install_as: Callable[[list_columns.ListColumns], str],
) -> None:
    """Which column of the uploaded list is the barcode (and the page link), as for the sign-off.

    Shown after every upload, even one headed the configured way and already installed, because
    that is what makes the guess auditable: a column silently read as the barcode would put a
    batch together from the wrong numbers. The first few values of the chosen column are listed
    beside it for the same reason.
    """
    options = video_signoff.column_options(grid)
    current: list[list_columns.ListColumns | None] = [
        list_columns.ListColumns(guessed[0], guessed[1])
        if installed and guessed[0] is not None
        else None
    ]
    area.clear()
    with area:
        # A fold, closed once the list is installed: open, it sat directly above the video
        # sign-off sheet's own column picker with nearly the same heading, and the two read as
        # one block (operator, 2026-10-23). Closed, it is one line that still names the column.
        fold = ui.expansion("", value=current[0] is None).classes("w-full list-columns")
        with fold:
            ui.label(
                "Read from the list's own headings. Change either if it is wrong, then install. "
                "The installed list carries the headings the tool reads; your upload is kept "
                "exactly as you sent it."
            ).classes("note")
            with ui.row().classes("gap-4 flex-wrap mt-2"):
                barcode = ui.select(options, value=guessed[0], label="Barcode").props(
                    "dense outlined"
                )
                barcode.classes("min-w-56")
                link: Any = None
                if config.target_url_column is not None:
                    link = ui.select(
                        options,
                        value=guessed[1],
                        label=f"Link to the page ({config.target_url_column}), optional",
                        clearable=True,
                    ).props("dense outlined")
                    link.classes("min-w-72")
            sample = ui.label("").classes("note mt-2")

            def chosen() -> list_columns.ListColumns | None:
                if barcode.value is None:
                    return None
                return list_columns.ListColumns(
                    int(barcode.value),
                    None if link is None or link.value is None else int(link.value),
                )

            def first_values(picked: list_columns.ListColumns) -> str:
                values = [row[picked.barcode] for row in grid.rows if row[picked.barcode].strip()]
                return ", ".join(values[:3]) or "(none)"

            def refresh() -> None:
                picked = chosen()
                if picked is None:
                    sample.text = ""
                    button.set_visibility(False)
                    fold.text = "Product list: choose the barcode column"
                    return
                sample.text = "First barcodes in that column: " + first_values(picked)
                pending = current[0] != picked
                # Shown only when there is something to install, so a settled list leaves no
                # button standing where the next section begins.
                button.set_visibility(pending)
                if pending:
                    fold.text = "Product list: not installed with these columns yet"
                else:
                    heading = grid.header[picked.barcode].strip() or f"column {picked.barcode + 1}"
                    fold.text = (
                        f"Product list: barcode column ‘{heading}’ "
                        f"({first_values(picked)}) — open to change"
                    )

            def go() -> None:
                picked = chosen()
                if picked is None:
                    return
                try:
                    install_as(picked)
                except ProcessListError as exc:
                    theme.notify_problem(f"Not installed: {exc}")
                    return
                current[0] = picked
                refresh()
                fold.close()

            button = theme.action("Install the list with these columns", go)
            barcode.on_value_change(lambda _event: refresh())
            if link is not None:
                link.on_value_change(lambda _event: refresh())
            refresh()


# --- Quality ------------------------------------------------------------------


def _issue_report(cid: str, built: IssueReport) -> None:
    """The client's issue report for the ticked products — what a run would report — on screen.

    Operator, 2026-10-09: the long data-quality report that stood here was archived (it is still
    written by every run and by ``report_quality``) and replaced by the one-to-two pager the client
    gets, so what is on screen and what is sent are the same document. It follows the ticks.
    """
    with theme.section(
        "Issue report for the client",
        tight=True,
        anchor="issue-report",
        explain=(
            "The ticked products' problems as the client receives them: how many failed, the "
            "issues by category, and each failed product with its reasons. A missing video is "
            "listed but does not count as failed. Every run writes the same report afterwards, "
            "about the products it ran."
        ),
    ):
        ui.markdown(issue_report_files.to_markdown(built)).classes("prose max-w-none")

        def download(kind: str) -> None:
            # Dated and kept under output/{client}/reports, so the copy that was sent can be found
            # again; then a copy in Downloads, opened in the operator's own PDF reader or Excel.
            now = datetime.now(UTC)
            dated = replace(built, subtitle=f"{built.subtitle} {now:%Y-%m-%d %H:%M} UTC.")
            path = DATA_ROOT / "output" / cid / "reports"
            path = path / f"issues-selection-{now:%Y%m%dT%H%M%S}.{kind}"
            path.parent.mkdir(parents=True, exist_ok=True)
            write = issue_report_files.write_pdf if kind == "pdf" else issue_report_files.write_xlsx
            write(dated, path)
            handover.give(path)

        with ui.row().classes("gap-3 mt-4"):
            theme.quiet_action("Download PDF", lambda: download("pdf"))
            theme.quiet_action("Download Excel", lambda: download("xlsx"))
