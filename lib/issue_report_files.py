"""Writing the client's issue report — :mod:`lib.issue_report` — as a PDF and an Excel workbook.

Same content, two forms (operator, 2026-10-09): the PDF to read and forward, the workbook to filter
and work through. Both carry the three parts in the same order — the total, the issues by category,
the failed products — so whichever one the client opens, the numbers agree.

The PDF uses the core Helvetica font, which carries Latin-1 only. Product names in Dutch and French
fit; the few typographic marks this tool writes (—, ·, …, quotes) are swapped for plain ones
rather than refused, so no report ever fails to build over a dash.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from fpdf import FPDF
from fpdf.fonts import FontFace

from lib.issue_report import IssueReport, run_issues, with_ticked
from lib.media_video import canon_gtin
from lib.result_sheet import write_workbook
from lib.scope_report import IN_SCOPE
from lib.scope_report import NOT_RUN as UNIT_NOT_RUN

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from lib.records import ProductRecord, RunOutcome
    from lib.scope_report import ScopeRow
    from lib.ticked_snapshot import Ticked

#: What a run's report is called, beside its log: ``runs/{stamp}/issues.pdf`` and ``.xlsx``.
RUN_STEM: Final = "issues"

_PLAIN: Final = str.maketrans(
    {"—": "-", "–": "-", "·": "-", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"', "→": "->"}
)
_FONT: Final = "helvetica"
_MARGIN_MM: Final = 15
_HEADING: Final = FontFace(emphasis="BOLD", fill_color=(235, 235, 235))
#: Barcode, product, reason — of the 180 mm between the margins.
_WIDTHS: Final = (32, 48, 100)


def write(report: IssueReport, folder: Path, stem: str = RUN_STEM) -> tuple[Path, Path]:
    """Write ``{stem}.pdf`` and ``{stem}.xlsx`` into ``folder``; return both paths."""
    folder.mkdir(parents=True, exist_ok=True)
    pdf, xlsx = folder / f"{stem}.pdf", folder / f"{stem}.xlsx"
    write_pdf(report, pdf)
    write_xlsx(report, xlsx)
    return pdf, xlsx


def for_run(  # noqa: PLR0913 — what one run was, each named
    title: str,
    stamp: str,
    rows: Sequence[ScopeRow],
    outcomes: Sequence[RunOutcome],
    products: Sequence[ProductRecord],
    *,
    mode: str,
    dry_run: bool,
    default_language: str,
    expected_without_video: Mapping[str, str] | None = None,
    ticked: Ticked | None = None,
    confirmed: Collection[str] | None = None,
) -> IssueReport:
    """The report for one run: the products it was given, and what stopped or marked them.

    A real run knows which pages went live without a video — its log says so. A dry run wrote no
    page, so it is told instead: ``expected_without_video`` is the Data screen's own verdict
    (:attr:`lib.eligibility.Eligibility.missing_video`), which is what the real run would do.

    With ``ticked`` — what the Data screen saved at Next — the report covers every ticked product,
    as the screen's did: those the run never received keep the screen's reason.

    ``confirmed`` — the barcodes the operator confirmed for this run. A product in the batch the
    plan neither confirmed nor held was left out on purpose, so it is left out of the report too
    (operator, 2026-10-10: a re-try of 6 of 31 must not report the other 25 as failed). Only a
    confirmed product with nothing in the log is *not reached*.
    """
    not_in_run: set[str] = set()
    if confirmed is not None:
        wanted = set(confirmed)
        not_in_run = {
            row.gtin
            for row in rows
            if row.in_scope == IN_SCOPE
            and row.gtin is not None
            and row.gtin not in wanted
            and all(unit.status == UNIT_NOT_RUN for unit in row.units.values())
        }
        rows = [row for row in rows if row.gtin not in not_in_run]
    names = {p.gtin14: p.product_name.get(default_language) or "" for p in products}
    if dry_run:
        bare = dict(expected_without_video or {})
    else:
        languages: dict[str, list[str]] = {}
        for outcome in outcomes:
            if outcome.status == "ok" and outcome.video_file == "":
                languages.setdefault(canon_gtin(outcome.gtin), []).append(outcome.language)
        bare = {gtin: f"no video in {', '.join(found)}" for gtin, found in languages.items()}
    selected, issues = run_issues(rows, names, writes_pages=mode != "links", without_video=bare)
    kind = "Dry run" if dry_run else "Run"
    ran = len(selected)
    scope = f"{ran} product(s) in this run"
    if ticked is not None:
        selected, issues = with_ticked(
            ticked.products, ticked.issues, selected, issues, not_in_run=not_in_run
        )
        if len(selected) > ran:
            scope += f", {len(selected) - ran} more ticked that could not run"
    if not_in_run:
        scope += f"; {len(not_in_run)} other(s) in the batch were not part of this run"
    scope += "."
    return IssueReport(
        title=title,
        subtitle=f"{kind} {stamp} - {mode} - {scope}",
        selected=tuple(selected),
        issues=tuple(issues),
    )


def summary_lines(report: IssueReport) -> list[str]:
    """Part 1 in sentences — the same words on both forms."""
    if not report.selected:
        return ["No products selected."]
    failed = len(report.failed)
    lines = [f"{failed} of {len(report.selected)} product(s) failed."]
    if report.with_warnings:
        lines.append(
            f"{report.with_warnings} more run, with an issue that does not stop them "
            "(see 'Live without a video')."
        )
    if not report.issues:
        lines.append("No issues: every product can run as it is.")
    return lines


def to_markdown(report: IssueReport) -> str:
    """The same three parts as Markdown, for the Data screen — the report as it would be sent."""
    lines = [f"**{report.subtitle}**", "", "#### 1. Total"]
    for line in summary_lines(report):
        lines += ["", line]
    by_category = report.by_category()
    if by_category:
        lines += ["", "#### 2. Issues by category"]
        for category, issues in by_category:
            label = "" if category.failed else " — does not stop the product"
            lines += [
                "",
                f"**{category.title} ({len(issues)})**{label}  ",
                f"*{category.action}*",
                "",
            ]
            lines += _md_table("Reason", [(i.gtin, i.name, i.reason) for i in issues])
    if report.failed:
        lines += ["", "#### 3. Failed products", ""]
        lines += _md_table(
            "Why",
            [
                (
                    gtin,
                    name,
                    "; ".join(f"{i.category.title}: {i.reason}" for i in report.reasons_of(gtin)),
                )
                for gtin, name in report.failed
            ],
        )
    return "\n".join(lines)


def _md_table(last: str, rows: list[tuple[str, str, str]]) -> list[str]:
    def cell(text: str) -> str:
        for mark in ("\\", "|", "_", "*", "`"):
            text = text.replace(mark, "\\" + mark)
        return text or " "

    return [f"| Barcode | Product | {last} |", "|---|---|---|"] + [
        f"| {cell(a)} | {cell(b)} | {cell(c)} |" for a, b, c in rows
    ]


def write_pdf(report: IssueReport, path: Path) -> None:
    """The one-to-two pager: total, then categories, then the failed products."""
    pdf = FPDF(format="A4")
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=_MARGIN_MM)
    pdf.add_page()

    pdf.set_font(_FONT, "B", 16)
    pdf.multi_cell(0, 8, _plain(report.title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(_FONT, "", 9)
    pdf.multi_cell(0, 5, _plain(report.subtitle), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    _heading(pdf, "1. Total")
    pdf.set_font(_FONT, "", 10)
    for line in summary_lines(report):
        pdf.multi_cell(0, 5, _plain(line), new_x="LMARGIN", new_y="NEXT")

    by_category = report.by_category()
    if by_category:
        _heading(pdf, "2. Issues by category")
        for category, issues in by_category:
            pdf.set_font(_FONT, "B", 10)
            label = "" if category.failed else " - does not stop the product"
            pdf.multi_cell(
                0,
                6,
                _plain(f"{category.title} ({len(issues)}){label}"),
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.set_font(_FONT, "I", 8)
            pdf.multi_cell(0, 4, _plain(category.action), new_x="LMARGIN", new_y="NEXT")
            _table(pdf, [(i.gtin, i.name, i.reason) for i in issues], "Reason")
            pdf.ln(2)

    if report.failed:
        _heading(pdf, "3. Failed products")
        rows = [
            (
                gtin,
                name,
                "; ".join(f"{i.category.title}: {i.reason}" for i in report.reasons_of(gtin)),
            )
            for gtin, name in report.failed
        ]
        _table(pdf, rows, "Why")
    pdf.output(str(path))


def write_xlsx(report: IssueReport, path: Path) -> None:
    """The same three parts as sheets: Total, By category, Failed products."""
    total = [[line] for line in [report.title, report.subtitle, "", *summary_lines(report)]]
    total += [[""]] + [
        [f"{category.title}: {len(issues)}"] for category, issues in report.by_category()
    ]
    by_category = [
        [i.category.title, "yes" if i.category.failed else "no", i.gtin, i.name, i.reason]
        for _, issues in report.by_category()
        for i in issues
    ]
    failed = [
        [
            gtin,
            name,
            "; ".join(f"{i.category.title}: {i.reason}" for i in report.reasons_of(gtin)),
        ]
        for gtin, name in report.failed
    ]
    write_workbook(
        path,
        [
            ("Total", ["Summary"], total),
            (
                "By category",
                ["Category", "Stops the product", "Barcode", "Product", "Reason"],
                by_category,
            ),
            ("Failed products", ["Barcode", "Product", "Why"], failed),
        ],
    )


def _heading(pdf: FPDF, text: str) -> None:
    pdf.ln(2)
    pdf.set_font(_FONT, "B", 12)
    pdf.multi_cell(0, 7, _plain(text), new_x="LMARGIN", new_y="NEXT")


def _table(pdf: FPDF, rows: list[tuple[str, str, str]], last: str) -> None:
    pdf.set_font(_FONT, "", 8)
    with pdf.table(
        col_widths=_WIDTHS,
        text_align="LEFT",
        headings_style=_HEADING,
        line_height=4.5,
        padding=1,
    ) as table:
        for cells in [("Barcode", "Product", last), *rows]:
            row = table.row()
            for cell in cells:
                row.cell(_plain(cell))


def _plain(text: str) -> str:
    """``text`` in the PDF font's Latin-1: typographic marks made plain, anything else a '?'."""
    return text.translate(_PLAIN).encode("latin-1", "replace").decode("latin-1")
