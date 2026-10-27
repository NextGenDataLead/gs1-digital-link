"""Screen 6 — what actually ran, per row.

The run log is the only durable account of a publish, and since it is appended row by row as the
run goes, a log that stops mid-way is a run that stopped mid-way. That is the case an operator
most needs to see: live pages and permanent GS1 records may already exist for the rows that
landed, and nothing else on this machine will tell them which.

So a partial log is shown as a partial log, not quietly rendered as a finished one.
"""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from lib import issue_report_files
from lib.provenance import read_run
from lib.records import RunOutcome
from lib.run_files import SOURCES_NAME, sibling
from ui import DATA_ROOT, context, handover, runner, theme


def render() -> None:
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Runs",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Runs"),
            "Runs",
            "Every row of every run, as it was recorded at the time.",
        )
        if cid is None:
            theme.blocked(
                "clients.yml did not load, so this screen has nothing to work from.",
                link_label="Open Setup →",
                route="/setup",
            )
            return

        _reconcile(cid)
        _complete_report(cid)

        runs = context.recent_runs(cid)
        if not runs:
            ui.label("No runs yet.").classes("note")
            return

        for run in runs:
            _run(run)


def _reconcile(cid: str) -> None:
    """Ask the site what is actually there, and compare it to the ledger.

    Everything below this on the screen is what *this machine* recorded, which cannot show a page
    created by anything else — another machine, a hand edit, or a run that failed part-way and
    logged the row as an error after the page was already live. That last one is not hypothetical:
    it is what the first real publish through this shell did, leaving ten entries in the ledger
    and eleven pages on the site.

    Read-only. It only GETs, and it reads state without quarantining a corrupt one.
    """
    with theme.section("Does the site match the ledger?"):
        ui.label(
            "Everything below is what this machine recorded. This asks the site instead, and "
            "compares the two in both directions — the only way to see a page that exists "
            "without a state entry, or an entry whose page is gone. Nothing is written."
        ).classes("note")

        argv = runner.reconcile_argv(cid)
        theme.command(argv)
        body = ui.column().classes("w-full mt-3")

        # Async, and the subprocess runs off the event loop. Both halves are needed, for the
        # reason `runner.run_off_the_loop` gives: a blocking call in a sync handler holds the
        # loop until the command has already finished, so the running-state the button paints
        # arrives at the browser only once there is nothing left to report.
        async def run() -> None:
            payload, result = await runner.run_json_off_the_loop(argv)
            body.clear()
            with body:
                if payload is None or not isinstance(payload, dict):
                    theme.band(
                        result.stderr or "The reconciliation did not return readable results.",
                        "danger",
                    )
                    return
                _report(payload)

        theme.action("Compare against the site", run)


def _complete_report(cid: str) -> None:
    """The complete data-quality report, built on request.

    Operator, 2026-10-08: "a complete insight into all missing data/video's, data to be adjusted,
    etc. So an actionable report." The Data screen's report is about the batch in progress; this
    one covers every product on the client's list and everything live — who does what, where each
    product stands (live pages checked on the site), the list's own problems, then the sections.
    A button rather than on every visit: it asks the site, which takes a while and needs the
    credentials the subprocess loads itself.
    """
    report = DATA_ROOT / "output" / cid / "complete-data-quality-report.md"
    with theme.section("Complete data-quality report"):
        ui.label(
            "Every product on your product list and everything live, with what is still to do "
            "for each and by who — the client in MyGS1, the client's videos, you, or the site's "
            "maintainer. Live pages are checked on the site. Saved as a file you can forward. "
            "Nothing is written to the site."
        ).classes("note")
        body = ui.column().classes("w-full mt-3")

        async def build() -> None:
            result = await runner.run_off_the_loop(runner.report_quality_argv(cid, complete=True))
            body.clear()
            with body:
                if not result.ok or not report.is_file():
                    theme.band(result.stderr or "The report could not be built.", "warn")
                    return
                ui.label(
                    f"{report.relative_to(DATA_ROOT)} — built {context.file_fact(report).age}"
                ).classes("mono")
                ui.markdown(report.read_text(encoding="utf-8")).classes("prose max-w-none")

        theme.action("Build the report", build)


def _report(payload: dict[str, object]) -> None:
    """Render the comparison: the summary first, then a row per divergence."""
    findings = payload.get("findings")
    findings = findings if isinstance(findings, list) else []
    theme.band(
        str(payload.get("summary", "")),
        "quiet" if payload.get("agrees") else "warn",
    )
    for finding in findings[:_MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        theme.check_row(
            "warn",
            f"{finding.get('gtin')} · {finding.get('language')} · {finding.get('kind')}",
            str(finding.get("detail", "")),
            str(finding.get("explanation", "")),
        )
    if len(findings) > _MAX_FINDINGS:
        ui.label(
            f"…and {len(findings) - _MAX_FINDINGS} more. The full list is in the command's output."
        ).classes("note")


def _run(run: context.RunLog) -> None:
    with ui.element("div").classes("card mb-4"):
        ui.label(run.stamp).classes("gate-step scroll-x")
        when = run.modified.strftime("%Y-%m-%d %H:%M UTC") if run.modified else "unknown time"
        ui.label(("Dry run · " if run.dry_run else "") + when).classes("gate-title")
        _what_it_read(run)

        with ui.row().classes("gap-12 items-end my-3"):
            theme.figure(str(run.ok), "ok")
            theme.figure(str(run.errors), "error")
            theme.figure(str(len(run.outcomes)), "rows recorded")

        if run.unreadable_lines:
            theme.band(
                f"{run.unreadable_lines} line(s) could not be read — the last one is usually "
                "truncated when a run was killed mid-write. The rows above are what it managed "
                "to record before it stopped.",
                "warn",
            )

        errors = [o for o in run.outcomes if o.status == "error"]
        if errors:
            with ui.element("div").classes("mt-2"):
                for outcome in errors[:_MAX_ERRORS]:
                    ui.label(_error_line(outcome)).classes("note mono scroll-x")
            ui.label(
                "In a links-only run, 'refusing to point a permanent GS1 record at it' means the "
                "target URL did not serve — the page is not where the plan thinks it is. That is "
                "not a GS1 fault, and it is the refusal that makes the mode safe to offer."
            ).classes("note mt-2")

        # The artefact is per-run and lives beside this log, so the button that makes it belongs
        # on this card rather than on the Data screen where the list is edited. A report generated
        # from a screen that shows no run has to guess which run it is about.
        _scope_report(run)
        _issue_report(run)

        with ui.expansion("Every row").classes("mt-2"):
            rows = [
                {
                    "gtin": o.gtin,
                    "lang": o.language,
                    "status": o.status,
                    "page": o.wp_page_id or "",
                    "gs1": "yes" if o.gs1_set else "",
                    "url": o.wp_url or "",
                }
                for o in run.outcomes
            ]
            ui.table(
                columns=[
                    {"name": key, "label": key, "field": key, "align": "left"}
                    for key in ("gtin", "lang", "status", "page", "gs1", "url")
                ],
                rows=rows,
                row_key="gtin",
            ).classes("w-full")


def _what_it_read(run: context.RunLog) -> None:
    """Which export and which selection this run consumed, from its own record.

    The figures below say what happened; this says what it happened *to*. Without it the only
    honest answer to "which export did that run use?" is a shrug, because ``input/`` has been
    replaced since — which is the whole reason a run writes this down before it starts.
    """
    recorded = read_run(sibling(run.path, SOURCES_NAME))
    if recorded is None:
        ui.label(
            "This run kept no record of what it read — it predates that being written down."
        ).classes("note")
        return

    export = recorded.sources.get("export")
    selection = recorded.sources.get("selection")
    parts: list[str] = []
    if export is not None:
        # The name they sent, then the archived copy in brackets. A run identified only by
        # `export-20260920T015319.xlsx` asks the operator to remember a timestamp.
        if export.given_name:
            parts.append(f"ran against “{export.given_name}” ({export.name})")
        else:
            parts.append(f"ran against {export.name or 'an export nothing recorded'}")
    if selection is not None and selection.rows is not None:
        parts.append(f"selection of {selection.rows} row(s)")
    ui.label(" · ".join(parts) if parts else "Nothing recorded about what it read.").classes(
        "note scroll-x"
    )


def _relative(path: Path) -> Path:
    """``path`` against the data folder, or unchanged when it lies outside it."""
    try:
        return path.relative_to(DATA_ROOT)
    except ValueError:
        return path


def _scope_report(run: context.RunLog) -> None:
    """Build the result sheet for this run, and say where it went.

    The operator sent a list of barcodes and gets back a log keyed by (GTIN, language). This is
    the sheet that turns the second back into the first — their columns, their order, with what
    happened appended — so it can be forwarded to whoever asked for the batch.

    A report, written after. Nothing in it is read back by a later run: a scope list that carried
    its own run status is exactly the design ``lib/process_list.py`` documents the cost of.
    """
    cid = context.client_id()
    if cid is None:
        return

    output = ui.log().classes("console mt-3").style("display:none")

    # Async, and the subprocess runs off the event loop — see ui/pages/data.py for why both.
    async def build() -> None:
        # Relative where it can be, so the command on screen is one the operator can paste into a
        # terminal from the data folder — which is the whole reason `theme.command` exists.
        argv = runner.report_scope_argv(cid, run=str(_relative(run.path)))
        output.style("display:block")
        output.clear()
        result = await runner.run_off_the_loop(argv)
        output.push(result.display_command)
        output.push(result.stderr or result.stdout or "(no output)")
        if result.ok:
            theme.notify_ok(f"Result sheet written for {run.stamp}")
        else:
            theme.notify_warning("The result sheet could not be built — see the output.")

    with ui.row().classes("gap-3 mt-3"):
        theme.quiet_action("Build the result sheet", build)


def _issue_report(run: context.RunLog) -> None:
    """The client's short report for this run — failed products and why — written by the run."""
    pdf = sibling(run.path, f"{issue_report_files.RUN_STEM}.pdf")
    xlsx = pdf.with_suffix(".xlsx")
    if not pdf.is_file():
        return
    with ui.row().classes("items-center gap-3 mt-3"):
        ui.label("Issue report for the client:").classes("note")
        theme.quiet_action("Download PDF", lambda: handover.give(pdf))
        if xlsx.is_file():
            theme.quiet_action("Download Excel", lambda: handover.give(xlsx))


def _error_line(outcome: RunOutcome) -> str:
    """One failed row, naming the request that failed when the run recorded one.

    The call goes first because it is what the reader needs first: "which of this row's five
    HTTP calls broke" is a different question from "what did it say", and a row that only said
    ``failed: 403`` sent someone re-running with the output piped to a file to answer it.
    """
    call = f"{outcome.failed_call}: " if outcome.failed_call else ""
    return f"{outcome.gtin} ({outcome.language}): {call}{outcome.error}"


_MAX_ERRORS = 25

#: Divergences rendered inline before pointing at the command's own output.
_MAX_FINDINGS = 40
