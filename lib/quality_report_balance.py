"""§4c of the data-quality report: marketing text far shorter in one language than another.

Its own module because :mod:`lib.quality_report` is past the project's size limit. The finding is
produced by the export parser (:func:`lib.gdsn.check_language_balance`); this only lays it out.

**One row per (product, source attribute), one column per configured language** — the same rule
§2 and §4 settled on: a third language adds a column, never a row. The cells are word counts, so the
imbalance is readable without opening MyGS1, and the attribute says where to fix it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from lib.report_markdown import table

if TYPE_CHECKING:
    from collections.abc import Callable

    from lib.records import ProductRecord, SourceIssue

#: The issue kind, as :data:`lib.gdsn.THIN_TEXT_ISSUE` writes it into ``source_issues.json``.
THIN_TEXT: Final = "value_thin_in_one_language"

#: The subsection heading — also the Summary row's wording, so the two cannot drift apart.
TITLE: Final = "Marketing text much shorter in one language"


def balance_lines(
    issues: list[SourceIssue],
    products: dict[str, ProductRecord],
    languages: list[str],
    label: Callable[[dict[str, ProductRecord], str], str],
    translated_from: str | None = None,
) -> list[str]:
    """The §4c subsection: a sentence on why it matters, then the word counts.

    ``translated_from`` is the default language under ``generator.translate_copy``: every page is
    then written from that language's text, which changes what a short text costs.
    """
    keys = sorted({(issue.gtin, issue.field.split(".", 1)[0], issue.source) for issue in issues})
    rows = [
        [label(products, gtin), source, *_words(products.get(gtin), field, languages)]
        for gtin, field, source in keys
    ]
    return [
        f"### 4c. {TITLE}",
        "",
        _why(translated_from),
        "",
        *table(
            ["GTIN", "Source attribute", *(f"Words ({lang})" for lang in languages)],
            rows,
        ),
        "",
    ]


def _why(translated_from: str | None) -> str:
    """Why a short text matters — it depends on which text each page is written from."""
    listed = "Listed when the shorter text has under a quarter of the words of the longer one."
    if translated_from is None:
        return (
            "Each language's page is written only from that language's own marketing text, and "
            "never with claims the text does not make. So where one language has a few words and "
            "another a full description, the short language's page says much less — one bullet "
            "instead of four. Not a blocker. Fill in the short one in MyGS1; the next generation "
            f"picks it up. {listed}"
        )
    return (
        f"Every page is written from the {translated_from} marketing text, and the other "
        "languages' pages are its translation, so the pages say the same thing whatever these "
        "counts are. "
        f"A short {translated_from} text still makes every page say less; a short text in another "
        "language changes no page, but is what that language's GS1 record carries. Not a blocker. "
        f"Fill in the short one in MyGS1. {listed}"
    )


def _words(product: ProductRecord | None, field: str, languages: list[str]) -> list[str]:
    """Word counts per configured language, ``—`` where the product carries no value."""
    localised = getattr(product, field, None) if product is not None else None
    values = localised.values if localised is not None else {}
    return [
        str(len(text.split())) if (text := values.get(lang) or "").strip() else "—"
        for lang in languages
    ]
