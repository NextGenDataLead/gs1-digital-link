"""Tests for report §4e (``lib.quality_report_languages``): layout, coverage line, Summary row.

The report is read raw (no HTML) and forwarded to the client, so what is pinned is what a reader
of the ``.md`` sees: one column per configured language, values kept on one line and short enough
to scan, pipes that cannot split a column, and a coverage line that says which products have no
judgment yet rather than letting an empty table read as "all agree".
"""

from __future__ import annotations

from lib.language_check import (
    VERDICT_DIFFERS,
    VERDICT_MISSING,
    VERDICT_WRONG_LANGUAGE,
    Finding,
    LanguageReport,
    ReportRow,
)
from lib.quality_report_languages import TITLE, language_lines, summary_row
from lib.records import ProductRecord

GTIN = "08713195006109"


def _label(_products: dict[str, ProductRecord], gtin: str) -> str:
    return f"`{gtin}`"


def _row(**values: str) -> ReportRow:
    return ReportRow(
        gtin=GTIN,
        label="MarketingInformation attr 1083",
        values=values,
        findings=[
            Finding(
                field="description_short",
                language="fr",
                verdict=VERDICT_DIFFERS,
                note="About human hair,\nnot pets.",
            )
        ],
    )


def _md(report: LanguageReport | None, languages: list[str] | None = None) -> str:
    return "\n".join(language_lines(report, {}, languages or ["nl", "fr"], _label))


def test_one_row_per_product_and_attribute_with_a_column_per_language() -> None:
    md = _md(LanguageReport([_row(nl="Borstel", fr="Brosse")], products=1, judged=1))

    assert f"### 4e. {TITLE}" in md
    assert "| GTIN | Source attribute | Value (nl) | Value (fr) | Finding |" in md
    assert (
        f"| `{GTIN}` | MarketingInformation attr 1083 | Borstel | Brosse | "
        "fr: says something else — About human hair, not pets. |"
    ) in md


def test_a_third_language_adds_a_column_not_a_row() -> None:
    md = _md(
        LanguageReport([_row(nl="a", fr="b", de="c")], products=1, judged=1), ["nl", "fr", "de"]
    )

    assert "| Value (nl) | Value (fr) | Value (de) |" in md
    assert md.count(f"`{GTIN}`") == 1


def test_a_long_value_is_cut_and_a_pipe_cannot_split_the_column() -> None:
    md = _md(LanguageReport([_row(nl="a | b", fr="x" * 400)], products=1, judged=1))

    assert "a \\| b" in md
    assert "x" * 400 not in md
    assert "…" in md


def test_every_verdict_has_words_a_client_reads() -> None:
    row = ReportRow(
        gtin=GTIN,
        label="TradeItemDescription attr 3301",
        values={"nl": "snoeischaar"},
        findings=[
            Finding(field="functional_name", language="fr", verdict=VERDICT_MISSING),
            Finding(
                field="functional_name", language="nl", verdict=VERDICT_WRONG_LANGUAGE, note="EN"
            ),
        ],
    )

    md = _md(LanguageReport([row], products=1, judged=1))

    assert "fr: missing; nl: another language — EN" in md


def test_products_not_yet_compared_are_said_not_hidden() -> None:
    md = _md(LanguageReport([], products=49, judged=40))

    assert "40 of 49 products compared." in md
    assert "**9 not yet**" in md


def test_all_compared_says_so_without_a_to_do() -> None:
    md = _md(LanguageReport([], products=49, judged=49))

    assert "49 of 49 products compared." in md
    assert "not yet" not in md


def test_without_product_data_the_subsection_keeps_its_number() -> None:
    assert _md(None).startswith(f"### 4e. {TITLE}")


def test_the_summary_row_counts_values_and_products() -> None:
    rows = [_row(nl="a", fr="b"), _row(nl="c", fr="d")]

    assert summary_row(LanguageReport(rows, products=2, judged=2))[2] == "2 values / 1 GTINs"
    assert summary_row(None)[2] == "—"
    assert summary_row(None)[1] == TITLE
