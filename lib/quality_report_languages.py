"""§4e of the data-quality report: source values that do not say the same in every language.

Its own module because :mod:`lib.quality_report` is past the project's size limit. The findings
come from :mod:`lib.language_check`; this only lays them out.

**One row per (product, source attribute), one column per configured language** — the rule §2,
§4 and §4c settled on: a third language adds a column, never a row. The values sit side by side
so the reader can compare them on the spot, which is what makes a model's verdict checkable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from lib.language_check import VERDICT_DIFFERS, VERDICT_MISSING, VERDICT_WRONG_LANGUAGE
from lib.report_markdown import cell, table

if TYPE_CHECKING:
    from collections.abc import Callable

    from lib.language_check import Finding, LanguageReport
    from lib.records import ProductRecord

#: The subsection heading — also the Summary row's wording, so the two cannot drift apart.
TITLE: Final = "Languages that do not say the same"

#: Longest a value is shown in a cell. A 1083 runs to a paragraph; the start shows which text it
#: is, and the whole value is in MyGS1 where it is fixed.
_MAX_CELL: Final = 160

_VERDICT_WORDS: Final = {
    VERDICT_MISSING: "missing",
    VERDICT_DIFFERS: "says something else",
    VERDICT_WRONG_LANGUAGE: "another language",
}


def summary_row(report: LanguageReport | None) -> list[str]:
    """The Summary table's row: values flagged, across how many products."""
    if report is None:
        count = "—"
    else:
        findings = sum(len(row.findings) for row in report.rows)
        gtins = len({row.gtin for row in report.rows})
        count = f"{findings} values / {gtins} GTINs"
    return ["Source", TITLE, count, "Client (MyGS1)", "No — §4e to compare"]


def language_lines(
    report: LanguageReport | None,
    products: dict[str, ProductRecord],
    languages: list[str],
    label: Callable[[dict[str, ProductRecord], str], str],
) -> list[str]:
    """The §4e subsection: why it matters, how much was compared, then the side-by-side rows."""
    heading = [f"### 4e. {TITLE}", ""]
    if report is None:
        unread = "_Not compared: the client config or the product data could not be read._"
        return [*heading, unread, ""]
    rows = [
        [
            label(products, row.gtin),
            cell(row.label),
            *(_cell(row.values.get(lang, "")) for lang in languages),
            _findings_cell(row.findings),
        ]
        for row in report.rows
    ]
    return [
        *heading,
        "Every attribute below is filled in per language in MyGS1, and nothing makes the languages "
        "agree. Where they do not, each language's page tells the buyer something different — the "
        "title directly, the rest through the text written from it. A value **missing** in one "
        "language is read straight from the export. **Says something else** and **another "
        "language** are an AI reviewer's reading of the two texts: look at both values before "
        "changing anything. Not a blocker. Fix it in MyGS1; the next export picks it up.",
        "",
        _coverage(report),
        "",
        *table(
            [
                "GTIN",
                "Source attribute",
                *(f"Value ({lang})" for lang in languages),
                "Finding",
            ],
            rows,
        ),
        "",
    ]


def _coverage(report: LanguageReport) -> str:
    """How many products a judgment is shown for — and how to judge the rest."""
    left = report.products - report.judged
    line = f"{report.judged} of {report.products} products compared."
    if left:
        line += (
            f" **{left} not yet**: their values are new or changed since the last comparison, so "
            "only missing values are shown for them. Generating the text (or `python -m "
            "scripts.check_languages`) compares the products on the product list; `python -m "
            "scripts.check_languages --all` also compares live products that are no longer on it."
        )
    return line


def _findings_cell(findings: list[Finding]) -> str:
    """``fr: says something else — <note>``, one per finding, joined in reading order."""
    return cell(
        "; ".join(
            f"{f.language}: {_VERDICT_WORDS.get(f.verdict, f.verdict)}"
            + (f" — {_one_line(f.note)}" if f.note else "")
            for f in findings
        )
    )


def _cell(value: str) -> str:
    """A value fit for one table cell: one line, trimmed to :data:`_MAX_CELL` characters."""
    flat = _one_line(value)
    return cell(flat if len(flat) <= _MAX_CELL else flat[: _MAX_CELL - 1].rstrip() + "…")


def _one_line(text: str) -> str:
    """Collapse newlines and runs of whitespace — a markdown table row is one line."""
    return " ".join(text.split())
