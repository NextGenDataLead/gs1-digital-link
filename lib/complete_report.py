"""The sections only the complete data-quality report has. Pure.

Operator, 2026-10-08: the report "needs to give a complete insight into all missing data/video's,
data to be adjusted, etc. So an actionable report." The batch report on the Data screen answers
for the products being worked on; this one answers for **every product the client sent and
everything that is live**, and adds what no per-product section can show:

* **where every product stands** — live (and, per page, whether the site shows its video and its
  text), ready to publish, held and why, or not in the export at all;
* **what is wrong with the product list itself** — barcodes the export does not carry, and one
  barcode on several rows (7 Days and Joy Grill share one; the export says it is the grill);
* **who does what** — a table of jobs by owner, each pointing at the section with the detail.

The site is the authority on a live page. A page the site could not be asked about is "not
checked", never "video · text": a report that guesses in the reassuring direction is the failure
this whole project is arranged against. ``scripts/report_quality --complete`` does the reading.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from lib.report_markdown import cell, table

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lib.live_inventory import PublishedTotals
    from lib.process_list import ProcessListSheet

LIVE: Final = "live"
READY: Final = "ready to publish"
NOT_IN_EXPORT: Final = "not in the export"
HELD: Final = "held"

#: A language the product has no page in.
_NO_PAGE: Final = "—"


@dataclass(frozen=True)
class ListedRow:
    """One row of the client's product list: its barcode (``None`` when blank) and how it reads."""

    gtin: str | None
    label: str


@dataclass(frozen=True)
class PageState:
    """A live page as the site answered for it. ``None`` throughout when the site was not asked."""

    on_site: bool | None
    video: bool | None
    text: bool | None


@dataclass(frozen=True)
class StatusRow:
    """One product, one status, one cell per language, and what it is waiting on."""

    gtin: str
    name: str
    status: str
    cells: list[str]
    note: str = ""


@dataclass(frozen=True)
class Action:
    """One job in the "what to do, by who" table."""

    owner: str
    what: str
    count: int
    where: str


def status_rows(  # noqa: PLR0913 — one keyword per source of a status
    *,
    listed: Sequence[ListedRow],
    exported: Mapping[str, str],
    held: Mapping[str, str],
    missing_video: Mapping[str, str],
    live: Mapping[str, Mapping[str, PageState]],
    languages: Sequence[str],
) -> list[StatusRow]:
    """Every product on the list, then every live product the list does not name.

    Args:
        listed: The product list as the client sent it, in its own order.
        exported: ``{gtin14: name}`` for every product the export carries.
        held: ``{gtin14: why}`` — the plan will hold it (:func:`lib.eligibility.eligibility`).
        missing_video: ``{gtin14: what it waits on}`` for products that would publish without a
            video somewhere.
        live: ``{gtin14: {language: PageState}}`` for every live product.
        languages: The configured languages, one cell each.
    """
    order: list[str] = []
    labels: dict[str, str] = {}
    for row in listed:
        if row.gtin and row.gtin not in labels:
            order.append(row.gtin)
            labels[row.gtin] = row.label
    order += sorted(gtin for gtin in live if gtin not in labels)

    rows = []
    for gtin in order:
        name = exported.get(gtin) or labels.get(gtin, "")
        if gtin in live:
            pages = live[gtin]
            cells = [_page_cell(pages.get(language)) for language in languages]
            rows.append(StatusRow(gtin, name, LIVE, cells))
            continue
        blank = [_NO_PAGE] * len(languages)
        if gtin not in exported:
            rows.append(StatusRow(gtin, name, NOT_IN_EXPORT, blank))
        elif gtin in held:
            rows.append(StatusRow(gtin, name, f"{HELD} — {held[gtin]}", blank))
        else:
            rows.append(StatusRow(gtin, name, READY, blank, missing_video.get(gtin, "")))
    return rows


def _page_cell(page: PageState | None) -> str:
    if page is None:
        return _NO_PAGE
    if page.on_site is None:
        return "not checked"
    if not page.on_site:
        return "not on the site"
    if page.video and page.text:
        return "video · text"
    return " · ".join(
        part for part, ok in (("no video", page.video), ("no text", page.text)) if not ok
    )


def status_lines(rows: Sequence[StatusRow], languages: Sequence[str], site_note: str) -> list[str]:
    """The section: counts by status, then the table."""
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[HELD if row.status.startswith(HELD) else row.status] += 1
    tally = ", ".join(
        f"**{counts[status]}** {status}"
        for status in (LIVE, READY, HELD, NOT_IN_EXPORT)
        if counts[status]
    )
    return [
        "## Every product and where it stands",
        "",
        f"Every product on the client's product list, and every product this tool has put live: "
        f"{len(rows)} in all — {tally}. For a live product each language says what the site "
        f"shows: its video and its text, or which is missing. {site_note}",
        "",
        *table(
            ["GTIN", "Product", "Status", *languages, "Waiting on"],
            [
                [f"`{row.gtin}`", cell(row.name), cell(row.status), *row.cells, cell(row.note)]
                for row in rows
            ],
        ),
        "",
    ]


def published_lines(
    totals: PublishedTotals, languages: Sequence[str], default_language: str
) -> list[str]:
    """The section: how many products, pages, GS1 records, links and QR codes this tool made."""
    notes = []
    if totals.without_record:
        notes.append(
            f"**{totals.without_record}** live product(s) have their pages but no GS1 record yet "
            "(published with **pages** only) — a **links** run writes it."
        )
    if totals.records_without_qr:
        notes.append(
            f"**{totals.records_without_qr}** product(s) have a GS1 record but no QR file on this "
            "machine."
        )
    if totals.taken_down:
        notes.append(
            f"**{totals.taken_down}** product(s) were published once and taken down since."
        )
    return [
        "## What this tool has published",
        "",
        f"**{totals.products}** products live, **{totals.records}** GS1 records, "
        f"**{totals.qr_codes}** QR codes. Each GS1 record's {default_language} link is its default "
        f"link, so a scan of the QR code opens the {default_language} page.",
        "",
        *table(
            ["", *languages],
            [
                ["Pages live", *(str(totals.pages.get(lang, 0)) for lang in languages)],
                ["Links in GS1 records", *(str(totals.links.get(lang, 0)) for lang in languages)],
            ],
        ),
        "",
        *(f"- {note}" for note in notes),
        *([""] if notes else []),
        "Counted from this machine's record of what it wrote and the QR files beside it; the GS1 "
        "records are not re-read for this. Whether each page is really on the site is checked in "
        "the next section.",
        "",
    ]


def listed_rows(sheet: ProcessListSheet) -> list[ListedRow]:
    """The product list as rows a person can recognise: barcode, and the first two filled cells
    other than the barcode (on Democlient's list: the article number and the description)."""
    rows = []
    for index in range(len(sheet.rows)):
        cells = [
            value.strip()
            for n, value in enumerate(sheet.rows[index])
            if n != sheet.gtin_index and value and value.strip()
        ]
        rows.append(ListedRow(sheet.gtin14_at(index), " ".join(cells[:2])))
    return rows


def shared_barcodes(listed: Sequence[ListedRow]) -> dict[str, list[str]]:
    """``{gtin14: [row labels]}`` for every barcode on more than one row, in list order.

    One function for the Data screen's warning and this report's table, so the two cannot
    disagree about which rows share a barcode.
    """
    by_gtin: dict[str, list[str]] = defaultdict(list)
    for row in listed:
        if row.gtin:
            by_gtin[row.gtin].append(row.label)
    return {gtin: labels for gtin, labels in by_gtin.items() if len(labels) > 1}


def list_lines(listed: Sequence[ListedRow], exported: Mapping[str, str]) -> list[str]:
    """What is wrong with the product list itself — before any product is looked at."""
    missing = [row for row in listed if row.gtin and row.gtin not in exported]
    shared = shared_barcodes(listed)
    return [
        "## Your product list",
        "",
        "### Barcodes the GS1 export does not carry",
        "",
        "On the list, with nothing behind them in the export, so nothing is published for them and "
        "no run says so. Either the product is missing from MyGS1's export, or the barcode on the "
        "list is wrong.",
        "",
        *table(["GTIN", "Row on the list"], [[f"`{r.gtin}`", cell(r.label)] for r in missing]),
        "",
        "### One barcode on several rows",
        "",
        "Usually a typo: two different products cannot share a barcode. The export names the "
        "product the barcode really belongs to — correct the other row.",
        "",
        *table(
            ["GTIN", "Rows on the list", "The export says"],
            [
                [
                    f"`{gtin}`",
                    cell("; ".join(labels)),
                    cell(exported.get(gtin) or "—"),
                ]
                for gtin, labels in sorted(shared.items())
            ],
        ),
        "",
    ]


def action_lines(actions: Sequence[Action]) -> list[str]:
    """Who does what, largest jobs first within each owner. Jobs with nothing to do are left out."""
    open_jobs = [action for action in actions if action.count]
    return [
        "## What to do, by who",
        "",
        "Every open job in this report, by who does it. The section named on the right has the "
        "detail.",
        "",
        *table(
            ["Who", "What", "How many", "Where"],
            [[a.owner, a.what, str(a.count), a.where] for a in open_jobs],
        ),
        "",
    ]
