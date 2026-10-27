"""Unit tests for lib/quality_report.py — the pure consolidated-report renderer.

Pure and deterministic: given already-parsed issue lists + products, it returns markdown.
No I/O, no clock (the snapshot date is injected), so the same inputs render byte-identical.
"""

from __future__ import annotations

import re

from lib.gdsn import GdsnSource
from lib.mandatory import MandatoryGap
from lib.quality_report import MatrixInput, render_quality_report
from lib.quality_report_video import TITLE as _VIDEO_TITLE
from lib.quality_report_video import VIDEO_SAMPLE as _VIDEO_SAMPLE
from lib.records import LocalisedText, ProductRecord, SourceIssue

_FRESH = {
    "generated": "2026-07-26",
    "source": "2026-07-19",
    "video_map": "2026-07-20",
    "category": "2026-07-26",
}


def _issue(  # noqa: PLR0913 — a test factory mirroring every SourceIssue field
    gtin: str,
    issue: str,
    *,
    field: str = "f",
    value: str = "",
    source: str = "",
    market_values: tuple[tuple[str, str], ...] = (),
) -> SourceIssue:
    return SourceIssue(
        gtin=gtin,
        field=field,
        source=source,
        issue=issue,
        value=value,
        detail="d",
        market_values=market_values,
    )


def _products(*gtins: str) -> dict[str, ProductRecord]:
    out: dict[str, ProductRecord] = {}
    for g in gtins:
        p = ProductRecord(
            gtin=g, brand="Democlient", product_name=LocalisedText(values={"nl": f"prod-{g[-4:]}"})
        )
        out[p.gtin14] = p
    return out


# Sections are found by the words of their heading, never by their number. The numbers move — a
# section inserted early renumbers every one after it — and twenty assertions spelling `"## 3."`
# would each have to move with them, or would silently start reading the wrong section. A number
# is asserted in exactly three tests, each a claim about the numbering itself: the order, the
# contiguity, and a subsection carrying its parent's number.
_COPY = "Blocks publish — no marketing message"
_CLAIMS = "Review before publish — inferred claims"
_MYGS1 = "Source-data fixes in MyGS1"
_TRANSLATED = "Translated to fill a language gap"
_VIDEO = _VIDEO_TITLE
_CATEGORIES = "Categories"

#: The numbered sections in the order a reader meets them, §0 aside. Video is first: a product held
#: for want of one is not published at all, and on the pilot that is 48 of 110.
_ORDER = (_VIDEO, _COPY, _CLAIMS, _MYGS1, _TRANSLATED, _CATEGORIES)

_NUMBERED = re.compile(r"^## (\d+)\. (.*)$", re.MULTILINE)


def _heading(md: str, title: str) -> str:
    """The one ``## N. …`` line whose text starts with ``title``. Fails on none or several."""
    found = [m.group(0) for m in _NUMBERED.finditer(md) if m.group(2).startswith(title)]
    assert len(found) == 1, f"expected one section headed {title!r}, found {found}"
    return found[0]


def _section(md: str, title: str) -> str:
    """That section, from its heading line up to the next ``## `` heading (``###`` stays in)."""
    start = md.index(_heading(md, title))
    following = re.search(r"^## ", md[start + 1 :], re.MULTILINE)
    return md[start : start + 1 + following.start()] if following else md[start:]


def _subheads(md: str, title: str) -> list[str]:
    return [line for line in _section(md, title).splitlines() if line.startswith("### ")]


def _render(**over: object) -> str:
    base: dict[str, object] = {
        "client_id": "democlient",
        "source_issues": [],
        "generated_issues": [],
        "video_map_issues": [],
        "category_issues": [],
        "products": {},
        "snapshot": "2026-07-27",
        "freshness": _FRESH,
        "languages": ["nl", "fr"],
    }
    base.update(over)
    return render_quality_report(**base)  # type: ignore[arg-type]


def test_the_header_leads_with_when_the_report_was_written() -> None:
    """On its own line, near the top — the reader should not have to check a file browser."""
    md = _render(snapshot="2026-08-13 22:02 CEST")

    lines = md.splitlines()
    assert "**Generated 2026-08-13 22:02 CEST**" in lines
    # Prominent, not buried: within the first handful of lines, under the title.
    assert lines.index("**Generated 2026-08-13 22:02 CEST**") < 4


def test_the_header_names_the_oldest_input_not_only_the_generation_time() -> None:
    """A report can be minutes old and still describe a month-old export — say which."""
    md = _render(snapshot="2026-08-13 22:02 CEST")

    assert "The oldest input is from 2026-07-19" in md  # the `source` entry in _FRESH


def test_a_missing_freshness_entry_does_not_become_an_ancient_date() -> None:
    """An unknown date is unknown; treating it as old would put a false scare in the header."""
    md = _render(snapshot="2026-08-13 22:02 CEST", freshness={})

    assert "oldest input" not in md
    assert "**Generated 2026-08-13 22:02 CEST**" in md


def test_inferences_are_listed_for_verification() -> None:
    gen = [
        _issue(
            "02987653007915",
            "generation_inference",
            field="generated_description.nl",
            value="Bevestig hem op elke metalen ondergrond",
        )
    ]
    md = _render(generated_issues=gen, products=_products("02987653007915"))

    assert "Bevestig hem op elke metalen ondergrond" in md


def test_blank_title_is_a_blocker_not_a_source_fix() -> None:
    """R-c removed §1d, which listed these. What must survive it is pinned here.

    §1d said what §0's `product·3301` column and the Summary row already said, and named one GTIN
    neither did — an out-of-scope one, which is a whole-catalogue finding leaking into a scoped
    report. Dropping the section must not quietly demote the finding to §4's "does not block"
    list, which is the one way this change could go wrong.
    """
    src = [
        _issue(
            "02987653007649",
            "value_blank",
            field="product_name.fr",
            source="TradeItemDescription attr 3301",
        )
    ]
    md = _render(source_issues=src, products=_products("02987653007649"))

    assert "Blank title / image" in md  # summary row, still marked a blocker
    assert "02987653007649" not in _section(md, _MYGS1)  # never demoted to "do not block publish"


def test_a_blank_non_critical_field_is_counted_but_not_listed_again() -> None:
    """§0's matrix is where a blank field is read; §3a listed the same thing in prose.

    Every field §3a could name has a matrix column — `net·3510` for this one — so the section
    repeated the ○ already on the row, with the attribute number the header carries too. The
    Summary keeps the count, exactly as it does for the blank title/image findings whose section
    (§1d) went for the same reason.
    """
    src = [
        _issue(
            "02987653000794",
            "value_blank",
            field="net_content",
            source="TradeItemMeasurements attr 3510",
        )
    ]
    md = _render(source_issues=src, products=_products("02987653000794"))

    assert "Blank non-critical fields" in md  # the Summary row survives
    assert "Blank non-critical fields" not in _section(md, _MYGS1)  # the section does not
    assert "02987653000794" not in _section(md, _MYGS1)


def test_a_subsection_carries_its_parents_number_with_no_gap() -> None:
    """A dangling `3b.` under a `3.` with no `3a.` is the drift this report keeps being read for.

    One of the three tests that assert a number: whatever the MyGS1 section is numbered, its
    subsections are that number with `a`, `b`, `c`, and nothing after.
    """
    md = _render(
        source_issues=[
            _issue("02987653000001", "value_inconsistent_across_markets", field="product_name.nl"),
            _issue("02987653000002", "value_wrong_language", field="product_name.fr", value="x"),
        ],
        products=_products("02987653000001", "02987653000002"),
    )

    parent = _NUMBERED.match(_heading(md, _MYGS1))
    assert parent is not None
    number = parent.group(1)
    assert _subheads(md, _MYGS1) == [
        f"### {number}a. Values inconsistent across markets",
        f"### {number}b. Possible wrong-language values (worth a glance)",
        f"### {number}c. Marketing text much shorter in one language",
    ]


def test_cross_market_values_shown_side_by_side() -> None:
    src = [
        _issue(
            "02987653007496",
            "value_inconsistent_across_markets",
            field="product_name.fr",
            value="Désherbant",
            market_values=(("528", "Désherbant"), ("056", "Desherbant")),
        )
    ]
    md = _render(source_issues=src, products=_products("02987653007496"))

    # both conflicting market texts are visible, with the chosen (highest-ranked) marked
    assert "528=`Désherbant` ✓" in md
    assert "056=`Desherbant`" in md


def test_wrong_language_values_are_listed() -> None:
    src = [
        _issue(
            "02987653000527",
            "value_wrong_language",
            field="product_name.fr",
            value="Schoonmaakdoek",
        )
    ]
    md = _render(source_issues=src, products=_products("02987653000527"))

    assert "Possible wrong-language values" in md  # the Summary row
    assert any("Possible wrong-language values" in line for line in _subheads(md, _MYGS1))
    assert "Schoonmaakdoek" in md


def test_marketing_text_much_shorter_in_one_language_is_listed_with_word_counts() -> None:
    """The Bottle Lamp on the e2e run: one Dutch bullet beside four French ones, from the source.

    Each language as a column of word counts, so the reader sees the imbalance without opening
    MyGS1 — and the Summary says it does not block, because it does not.
    """
    gtin = "02987653005898"
    product = ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values={"nl": "Tafellamp"}),
        description_short=LocalisedText(
            values={"nl": "Draadloze led-wijnfleslamp", "fr": " ".join(["mot"] * 189)}
        ),
    )
    src = [
        _issue(
            gtin,
            "value_thin_in_one_language",
            field="description_short.nl",
            source="MarketingInformation attr 1083",
            value="Draadloze led-wijnfleslamp",
        )
    ]

    md = _render(source_issues=src, products={product.gtin14: product})

    assert "| Source | Marketing text much shorter in one language | 1 |" in md
    assert any("Marketing text much shorter" in line for line in _subheads(md, _MYGS1))
    section = _section(md, _MYGS1)
    assert "| GTIN | Source attribute | Words (nl) | Words (fr) |" in section
    assert "| MarketingInformation attr 1083 | 2 | 189 |" in section


def test_both_languages_short_in_one_product_is_still_one_row() -> None:
    """Rows are (product, attribute); a third language would add a column, never a row."""
    gtin = "02987653005898"
    product = ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values={"nl": "x"}),
        description_short=LocalisedText(
            values={"nl": "kort", "fr": "court", "de": " ".join(["Wort"] * 40)}
        ),
    )
    src = [
        _issue(gtin, "value_thin_in_one_language", field=f"description_short.{lang}", source="a")
        for lang in ("nl", "fr")
    ]

    md = _render(
        source_issues=src, products={product.gtin14: product}, languages=["nl", "fr", "de"]
    )

    rows = [line for line in _section(md, _MYGS1).splitlines() if "| a |" in line]
    assert len(rows) == 1
    assert rows[0].endswith("| 1 | 1 | 40 |")


def test_summary_counts_each_area() -> None:
    md = _render(
        generated_issues=[
            _issue("02987653000001", "content_generated"),
            _issue("02987653000002", "missing_generation_input"),
            _issue("02987653000003", "generation_inference", value="x"),
        ],
        source_issues=[_issue("02987653000004", "value_blank")],
        video_map_issues=[_issue("", "video_unconfirmed", value="A.mpg")],
    )

    assert "## Summary" in md
    assert "Videos not yet mapped to a GTIN" in md  # the media row exists
    # counts come from the passed data: one blank + one video issue each render a count of 1
    assert "| 1 |" in md


def test_categories_clean_line_when_no_issues() -> None:
    assert "No unmapped GPC bricks" in _render()


# --- §5 translated values ------------------------------------------------------


def _translated(gtin: str, field: str, value: str, source: str, detail: str = "d") -> SourceIssue:
    return SourceIssue(
        gtin=gtin,
        field=field,
        source=source,
        issue="value_translated",
        value=value,
        detail=detail,
    )


def test_translated_values_are_listed_with_the_text_to_paste() -> None:
    """The value is the deliverable here, not evidence for a count.

    §3 deliberately became a pointer rather than a per-row dump, because nobody acts on generated
    copy row by row. This section is the opposite: each row is one paste into MyGS1, so the text
    has to be in the table.
    """
    gen = [
        _translated(
            "02987653000001",
            "product_name.fr",
            "Pic d'arrosage",
            "TradeItemDescription attr 3301",
        )
    ]
    md = _render(generated_issues=gen, products=_products("02987653000001"))

    assert _heading(md, _TRANSLATED)
    assert "Pic d'arrosage" in md
    assert "TradeItemDescription attr 3301" in md
    assert "fr" in md


def test_every_filled_value_is_one_row_and_the_summary_says_the_same_number() -> None:
    """One row per filled value, and one number above it that agrees.

    The count and the table are assembled in different places, so they can drift — and they once
    did, for a reason since removed: attr 3301 was declared twice, as `product_name` and again as
    `extras.functional_name`, so one MyGS1 paste appeared as two rows. That was deduplicated here.
    The duplicate declaration is gone and `lib.config` refuses one at load, which leaves this test
    holding the half that still matters: a summary saying 2 above a table of 1 is the kind of
    small contradiction that makes a reader stop trusting the whole document.
    """
    same = "TradeItemDescription attr 3301"
    md = _render(
        generated_issues=[
            _translated("02987653007649", "product_name.fr", "câble magnétique", same),
            _translated("02987653007649", "product_name.de", "Magnetkabel", same),
        ],
        products=_products("02987653007649"),
    )

    section = _section(md, _TRANSLATED)
    assert "câble magnétique" in section
    assert "Magnetkabel" in section
    rows = [line for line in section.splitlines() if line.startswith("| `0298")]
    filled = sum(cell.strip() != "" for row in rows for cell in row.split("|")[3:-1])
    summary = next(line for line in md.splitlines() if "Values translated" in line)

    # One row now, carrying two pastes: the same field in two languages is one place to paste in
    # MyGS1 per language, and the row is keyed on the field. So the Summary's count is filled
    # *cells*, not rows — and the section states both, or a reader counts rows and finds a
    # contradiction that is not there.
    assert len(rows) == 1
    assert f"| {filled} |" in summary
    # "across 1 row" is a prefix of "across 1 rows", so the trailing text is what makes this
    # assertion able to fail — the substring form let a broken pluraliser through.
    assert "2 values to paste, across 1 row —" in section
    # `de` is not among the configured languages here, and its column is appended rather than
    # dropped: a stale findings file must not make a MyGS1 paste instruction vanish silently.
    header = next(line for line in section.splitlines() if line.startswith("| GTIN |"))
    assert header.strip().endswith("| Value to paste (de) |")


def test_a_translated_value_lands_after_the_other_mygs1_fixes_not_among_the_blockers() -> None:
    # It is MyGS1 work that does not hold the GTIN — §4's neighbourhood, not §2's.
    md = _render(
        generated_issues=[
            _translated(
                "02987653000001", "product_name.fr", "Pic", "TradeItemDescription attr 3301"
            )
        ],
        products=_products("02987653000001"),
    )

    assert md.index(_heading(md, _TRANSLATED)) > md.index(_heading(md, _MYGS1))
    assert "02987653000001" not in _section(md, _COPY)


def test_the_sections_come_in_the_order_a_reader_needs_them() -> None:
    """The order, by name: what blocks publishing first, the backlog and the categories last.

    One of the three tests that assert where a section sits. Pinned once, here, so moving a
    section is one edit to `_ORDER` rather than a hunt through every test that knew its number.
    """
    titles = [m.group(2) for m in _NUMBERED.finditer(_render()) if m.group(1) != "0"]

    assert [next(t for t in _ORDER if title.startswith(t)) for title in titles] == list(_ORDER)


def test_the_sections_are_numbered_contiguously_with_no_gap_or_repeat() -> None:
    """§0 when there is a matrix, then 1, 2, 3 … unbroken.

    One of the three tests that assert a number. A section dropped or inserted without renumbering
    the rest leaves a hole or a duplicate, which a reader of the forwarded report takes for a
    missing page.
    """
    without = [int(m.group(1)) for m in _NUMBERED.finditer(_render())]
    with_matrix = [int(m.group(1)) for m in _NUMBERED.finditer(_render(matrix=_matrix()))]

    assert without == list(range(1, len(_ORDER) + 1))  # no matrix, no §0
    assert with_matrix == list(range(0, len(_ORDER) + 1))


def test_an_empty_translation_section_says_none_rather_than_a_headerless_table() -> None:
    md = _render()

    assert "_None._" in _section(md, _TRANSLATED)


def test_the_summary_counts_translated_values_as_non_blocking_mygs1_work() -> None:
    md = _render(
        generated_issues=[
            _translated(
                "02987653000001", "product_name.fr", "Pic", "TradeItemDescription attr 3301"
            )
        ],
        products=_products("02987653000001"),
    )

    row = next(line for line in md.splitlines() if "Values translated" in line)
    assert "MyGS1" in row
    assert "no" in row.lower()


def test_observations_section_renders_notes() -> None:
    md = _render(
        observations=[
            "…0527's French title reads Dutch (Schoonmaakdoek).",
            "…7496 resolved only after a brief propagation lag — re-check.",
        ]
    )
    assert "## Observations (this run)" in md
    assert "Schoonmaakdoek" in md
    assert "propagation lag" in md


def test_observations_section_placeholder_when_empty() -> None:
    md = _render()
    assert "## Observations (this run)" in md
    assert "None recorded for this run" in md


def test_render_is_deterministic() -> None:
    args = {
        "generated_issues": [_issue("02987653000001", "content_generated", value="src")],
        "products": _products("02987653000001"),
    }
    assert _render(**args) == _render(**args)


def test_pipe_in_value_is_escaped_for_markdown_table() -> None:
    gen = [
        _issue(
            "02987653000001",
            "generation_inference",
            field="generated_description.nl",
            value="a | b",
        )
    ]
    md = _render(generated_issues=gen, products=_products("02987653000001"))
    # the raw pipe must be escaped so it does not break the table column
    assert "a \\| b" in md


def test_generated_copy_is_a_pointer_not_a_per_row_dump() -> None:
    # 2b used to list every generated row; now only a count + pointer, no per-row source text.
    gen = [
        _issue(
            "02987653000001", "content_generated", field="generated_description.nl", value="src"
        ),
        _issue(
            "02987653000002", "content_generated", field="generated_description.fr", value="txt"
        ),
    ]
    md = _render(generated_issues=gen, products=_products("02987653000001", "02987653000002"))

    assert "2 generated-copy row(s) written this run are reviewed" in md  # count-based pointer
    assert "generation_results.json" in md
    assert "src" not in md and "txt" not in md  # no per-row source dump
    assert not _subheads(md, _CLAIMS)  # the old subsection is gone


# --- §0 coverage matrix -------------------------------------------------------


def _matrix(**over: object) -> MatrixInput:
    base: dict[str, object] = {
        "products": [],
        "gdsn_map": {
            "product_name": GdsnSource(sheet="S", attribute="3301", localised=True, required=True),
            "brand": GdsnSource(sheet="S", attribute="3336", required=True),
            "net_content": GdsnSource(sheet="S", attribute="3510"),
        },
        "gdsn_extras": {"material": GdsnSource(sheet="S", attribute="Material")},
        "video_confirmed": {"nl": set(), "fr": set()},
    }
    base.update(over)
    return MatrixInput(**base)  # type: ignore[arg-type]


def _p(gtin: str, **over: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": gtin,
        "brand": "Democlient",
        "product_name": LocalisedText(values={"nl": "a", "fr": "b"}),
    }
    base.update(over)
    return ProductRecord.model_validate(base)


def test_matrix_sorts_richest_first() -> None:
    """The order someone works in — GTIN order carries no information."""
    thin = _p("02987653000001", product_name=LocalisedText(values={"nl": "a"}))
    rich = _p("02987653000002", net_content="10 cm", extras={"material": "PP"})
    md = _render(matrix=_matrix(products=[thin, rich]))

    body = [line for line in md.splitlines() if "| `0298" in line]
    assert body[0].startswith("| 1 | `02987653000002`")  # richer first


def _headers(md: str) -> list[str]:
    """The §0 header cells, in rendered order."""
    header = next(line for line in md.splitlines() if line.startswith("| # |"))
    return [c.strip() for c in header.split("|")[1:-1]]


def _cell_for(md: str, gtin: str, header: str) -> str:
    """The cell under ``header`` for one product, found by name rather than by position.

    Every lookup here used to count backwards from the end of the row, which made a column
    reorder break tests that have nothing to do with ordering — twice now. The header row is
    already the index; use it.
    """
    idx = next(i for i, h in enumerate(_headers(md)) if h.removesuffix(" (optional)") == header)
    row = next(line for line in md.splitlines() if f"| `{gtin}`" in line)
    return row.split("|")[1:-1][idx].strip()


def test_the_optional_columns_say_optional_in_words() -> None:
    """Four goes at this, and the first three were all too clever.

    `**…**` was invisible (markdown makes header cells bold anyway); `MANDATORY<br>…` rendered
    correctly and showed a literal HTML tag to anyone reading the markdown as text; a trailing
    `~` was plain enough but meant nothing without the legend. The word is the version a reader
    does not have to decode.
    """
    md = _render(matrix=_matrix(products=[_p("02987653000001")]))

    assert "material (optional)" in _headers(md)
    assert "product·3301" in _headers(md)  # mandatory: unmarked
    assert not [h for h in _headers(md) if "**" in h or "<" in h or "~" in h]


def test_the_header_makes_exactly_one_crossing_from_mandatory_to_optional() -> None:
    """Mandatory columns first, optional after, and the boundary crossed once.

    A regression pin, not a fix: `_columns` already emits them in that order. It is worth pinning
    because the split is derived from `required` / `required_group` in the **gitignored**
    `clients.yml`, so a field flipping its flag re-groups the header with no code change — and
    nothing in git can see the config that did it. Interleaved groups would make the mark noise:
    the legend says "everything up to X", which is only true of a contiguous run.
    """
    md = _render(matrix=_matrix(products=[_p("02987653000001")]))
    fields = [h for h in _headers(md) if h not in {"#", "GTIN", "Name", "score"}]
    optional = [h.endswith(" (optional)") for h in fields]

    assert set(optional) == {True, False}
    crossings = [(a, b) for a, b in zip(optional, optional[1:], strict=False) if a != b]
    assert crossings == [(False, True)]  # mandatory → optional, once


def test_score_sits_on_the_boundary_between_the_two_groups() -> None:
    """`score` is the divider, which is why the groups need no marker between them.

    Placed after the last mandatory column and before the first optional one, so what it counts
    is legible from where it sits rather than from the legend.
    """
    md = _render(matrix=_matrix(products=[_p("02987653000001")]))
    cols = _headers(md)
    at = cols.index("score")

    assert not cols[at - 1].endswith(" (optional)")  # last mandatory column
    assert cols[at + 1].endswith(" (optional)")  # first optional one
    assert all(h.endswith(" (optional)") for h in cols[at + 1 :])


def test_score_counts_the_mandatory_columns_only() -> None:
    """The score answers "how close is this SKU to publishable", so optional fill cannot raise it.

    It used to count every column, which let a product with both optional values and a missing
    mandatory one outrank a publishable one — and the table is *sorted* by it, so that put the
    wrong SKU at the top of the worklist.
    """
    gtin = "02987653000001"
    md = _render(
        matrix=_matrix(
            products=[_p(gtin, net_content="10 cm", extras={"material": "PP"})],
            video_confirmed={"nl": {gtin}, "fr": {gtin}},
        )
    )

    # product·3301 (2 languages) + brand (1) + video (2) = 5; net_content and material are
    # optional and add nothing, though both are present.
    assert _cell_for(md, gtin, "net·3510") == "●"
    assert _cell_for(md, gtin, "material") == "●"
    assert _cell_for(md, gtin, "score") == "5"


def test_a_thin_sku_with_optional_extras_ranks_below_a_publishable_one() -> None:
    """The consequence of the rule above, at the level the operator actually reads: row order."""
    publishable = _p("02987653000001")  # both languages, no optional values
    padded = _p(  # one language, but every optional column filled
        "02987653000002",
        product_name=LocalisedText(values={"nl": "a"}),
        net_content="10 cm",
        extras={"material": "PP"},
    )
    md = _render(matrix=_matrix(products=[padded, publishable]))

    body = [line for line in md.splitlines() if "| `0298" in line]
    assert body[0].startswith("| 1 | `02987653000001`")


def test_the_legend_explains_the_boundary_score_marks() -> None:
    """The legend and the header are one fact, so they must not be able to disagree."""
    md = _render(matrix=_matrix(products=[_p("02987653000001")]))
    legend = next(line for line in md.splitlines() if "present ·" in line)

    assert "before `score` are mandatory" in legend
    assert "after `score`" in legend
    assert "Bold columns" not in legend  # it pointed at a marker that rendered as nothing


def test_the_context_line_says_how_many_skus_the_table_holds() -> None:
    """The first question asked of a worklist is how big it is, and it was answered nowhere."""
    md = _render(matrix=_matrix(products=[_p(f"0298765300000{n}") for n in range(1, 4)]))

    assert "**3 SKUs in scope**" in md
    assert len([line for line in md.splitlines() if "| `0298" in line]) == 3


def test_rows_are_numbered_in_the_order_they_are_shown() -> None:
    """A counter to refer to a row by, following the sort rather than the GTIN."""
    thin = _p("02987653000001", product_name=LocalisedText(values={"nl": "a"}))
    rich = _p("02987653000002", net_content="10 cm", extras={"material": "PP"})
    md = _render(matrix=_matrix(products=[thin, rich]))

    body = [line for line in md.splitlines() if "| `0298" in line]
    assert [line.split("|")[1].strip() for line in body] == ["1", "2"]
    assert body[0].startswith("| 1 | `02987653000002`")  # richest first, numbered from the top


def test_a_required_extra_joins_the_mandatory_run() -> None:
    """`required` on a `gdsn_extras` entry means what it means on a mapped field.

    It could not before: every extra was hard-coded optional here, so a client could mark one
    required and watch the matrix ignore it — the shape #96's `multivalue` flag had.
    """
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001")],
            gdsn_extras={
                "dim_height": GdsnSource(sheet="S", attribute="3498", required=True),
                "material": GdsnSource(sheet="S", attribute="Material"),
            },
        )
    )
    fields = [h for h in _headers(md) if h not in {"#", "GTIN", "Name", "score"}]

    assert "dim·height" in fields  # unmarked: mandatory
    assert "material (optional)" in fields
    assert fields.index("dim·height") < fields.index("material (optional)")


#: The live either-or: Democlient's copy comes from attr 1083 *or* attr 1067, never both required.
_GROUP_MAP = {
    "description_short": GdsnSource(
        sheet="S", attribute="1083", localised=True, required_group="marketing_copy"
    ),
    "description_long": GdsnSource(
        sheet="S", attribute="1067", localised=True, required_group="marketing_copy"
    ),
    "net_content": GdsnSource(sheet="S", attribute="3510"),
}


def _grouped(gtin: str, **over: object) -> str:
    return _render(matrix=_matrix(products=[_p(gtin, **over)], gdsn_map=_GROUP_MAP))


def test_a_required_group_is_one_column_named_after_the_group() -> None:
    """Two columns for one requirement said something false about both of them.

    `description_short` (1083) and `description_long` (1067) are a `required_group`: a SKU is held
    only when **both** are blank. Rendered as two mandatory columns, the legend's "a gap here
    holds the whole SKU" was untrue of either — and on the real report 26 of 37 in-scope SKUs
    publish with 1067 empty, each showing a ○ that means nothing on its own.
    """
    md = _grouped("02987653000001")
    fields = [h for h in _headers(md) if h not in {"#", "GTIN", "Name", "score"}]

    assert "marketing·copy" in fields
    assert not [h for h in fields if "1083" in h or "1067" in h]
    assert fields.index("marketing·copy") < fields.index("net·3510 (optional)")


def test_either_member_satisfies_the_group() -> None:
    """Which one the feed carries is not the matrix's question — whether the SKU is held is."""
    both_langs = LocalisedText(values={"nl": "a", "fr": "b"})
    short_only = _grouped("02987653000001", description_short=both_langs)
    long_only = _grouped("02987653000001", description_long=both_langs)
    neither = _grouped("02987653000001")

    assert _cell_for(short_only, "02987653000001", "marketing·copy") == "●"
    assert _cell_for(long_only, "02987653000001", "marketing·copy") == "●"
    assert _cell_for(neither, "02987653000001", "marketing·copy") == "○"


def test_the_group_is_satisfied_per_language_across_its_members() -> None:
    """One member in nl and the other in fr satisfies the requirement in both languages.

    The case that makes this a union per language rather than the best member's mark: taken
    member-wise, each is half-filled, and the SKU would read as ◐ while nothing about it is
    missing. E23 asks the same question the same way — a group is satisfied in a language when
    **any** member carries a value in it.
    """
    md = _grouped(
        "02987653000001",
        description_short=LocalisedText(values={"nl": "a"}),
        description_long=LocalisedText(values={"fr": "b"}),
    )

    assert _cell_for(md, "02987653000001", "marketing·copy") == "●"


def test_the_group_scores_once_however_many_members_carry_a_value() -> None:
    """Satisfying it twice is not more publishable than satisfying it once.

    It used to score both members, so a SKU with 1083 *and* 1067 outranked one with 1083 alone by
    two slots — on the real report that was rows 1 and 2, ranked apart by a field neither needed.
    """
    both = _grouped(
        "02987653000001",
        description_short=LocalisedText(values={"nl": "a", "fr": "b"}),
        description_long=LocalisedText(values={"nl": "c", "fr": "d"}),
    )
    one = _grouped("02987653000001", description_short=LocalisedText(values={"nl": "a", "fr": "b"}))

    # The absolute value, not just that the two agree: a multiplier applied to *both* would keep
    # them equal while still inflating every group in the table. `_GROUP_MAP`'s only mandatory
    # columns are marketing·copy (satisfied in 2 languages) and video (confirmed in none).
    assert _cell_for(one, "02987653000001", "score") == "2"
    assert _cell_for(both, "02987653000001", "score") == "2"


def test_a_group_member_declared_as_an_extra_still_satisfies_it() -> None:
    """`missing_mandatory` groups across both maps, so the matrix has to as well.

    Reading `gdsn_map` alone would drop the extra from the column's members and show ○ for a SKU
    the plan publishes — the matrix and the hold disagreeing, which is the whole failure this
    column exists to stop.
    """
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001", extras={"fallback_copy": "een korte tekst"})],
            gdsn_map={
                "description_short": GdsnSource(
                    sheet="S", attribute="1083", required_group="marketing_copy"
                )
            },
            gdsn_extras={
                "fallback_copy": GdsnSource(
                    sheet="S", attribute="1067", required_group="marketing_copy"
                )
            },
        )
    )

    assert "marketing·copy" in _headers(md)
    assert _cell_for(md, "02987653000001", "marketing·copy") == "●"


def test_a_group_keeps_its_column_when_only_one_member_asked_for_one() -> None:
    """`in_matrix: false` says "this value needs no column", not "this value does not exist".

    The requirement still holds SKUs, so it still needs a column — and the hidden member still
    counts towards satisfying it, or the column would report a gap that E23 does not see.
    """
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001", extras={"hidden_copy": "een korte tekst"})],
            gdsn_map={
                "description_short": GdsnSource(
                    sheet="S", attribute="1083", required_group="marketing_copy"
                )
            },
            gdsn_extras={
                "hidden_copy": GdsnSource(
                    sheet="S",
                    attribute="1067",
                    required_group="marketing_copy",
                    in_matrix=False,
                )
            },
        )
    )

    assert "marketing·copy" in _headers(md)
    assert "hidden·copy" not in _headers(md)  # no column of its own
    assert _cell_for(md, "02987653000001", "marketing·copy") == "●"  # but it satisfies the group


def test_the_legend_says_the_group_is_an_either_or() -> None:
    """A collapsed column has to say what satisfies it, or it reads as one more mandatory field."""
    legend = next(line for line in _grouped("02987653000001").splitlines() if "present ·" in line)

    assert "`marketing·copy`" in legend
    assert "1083" in legend and "1067" in legend


def test_video_sits_with_the_mandatory_columns_not_after_the_optional_ones() -> None:
    """A missing confirmed video holds the whole SKU (E24), so it belongs in the mandatory run.

    It used to render last, between `material` and `score`, which put a third group after the
    optional block and made "mandatory first" untrue of the table as a whole.
    """
    md = _render(matrix=_matrix(products=[_p("02987653000001")]))
    cols = _headers(md)

    assert "video" in cols  # unmarked: mandatory
    assert cols.index("video") < cols.index("material (optional)")
    assert cols.index("video") < cols.index("score")  # video counts towards it
    assert cols[0] == "#"


def test_grouping_the_header_leaves_every_cell_where_it_was() -> None:
    """`_columns` is what `_mark` and the score iterate, so a header change can reach the cells.

    Pinned against a product with one of each mark — full, half, missing, plus a video confirmed
    in one language only — so a reorder that dropped or double-counted a column shows up here as
    a wrong score rather than as a silently different table.
    """
    gtin = "02987653000001"
    md = _render(
        matrix=_matrix(
            products=[
                _p(
                    gtin,
                    product_name=LocalisedText(values={"nl": "a"}),
                    net_content="10 cm",
                    extras={"material": "PP"},
                )
            ],
            video_confirmed={"nl": {gtin}, "fr": set()},
        )
    )

    row = next(line for line in md.splitlines() if f"| `{gtin}`" in line)
    # mandatory: product·3301 ◐ (nl only) · brand ● · video ◐ = 3 | optional: net ● · material ●
    assert row == f"| 1 | `{gtin}` |  | ◐ | ● | ◐ | 3 | ● | ● |"


def test_a_localised_field_in_one_language_is_a_half_mark() -> None:
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001", product_name=LocalisedText(values={"nl": "a"}))]
        )
    )

    row = next(line for line in md.splitlines() if "| `02987653000001`" in line)
    assert "◐" in row


#: A `gdsn_extras` entry whose source attribute carries a LanguageCode/Value pair.
_LOCALISED_EXTRA = {"functional_name": GdsnSource(sheet="S", attribute="3301", localised=True)}


def _extra_cell(md: str, gtin: str) -> str:
    return _cell_for(md, gtin, "functional·name")


def test_a_localised_extra_is_counted_per_language() -> None:
    """Now the parser keeps every language of a localised extra, the matrix can count them.

    It could not before: the value collapsed to one flat string, so a per-language reading
    found nothing and reported every extra missing.
    """
    md = _render(
        matrix=_matrix(
            products=[
                _p(
                    "02987653000001",
                    extras_localised={
                        "functional_name": LocalisedText(values={"nl": "haak", "fr": "crochet"})
                    },
                ),
                _p(
                    "02987653000002",
                    extras_localised={"functional_name": LocalisedText(values={"nl": "haak"})},
                ),
            ],
            gdsn_extras=_LOCALISED_EXTRA,
        )
    )

    assert _extra_cell(md, "02987653000001") == "●"
    assert _extra_cell(md, "02987653000002") == "◐"


def test_a_flat_extra_from_an_older_parse_is_still_one_slot() -> None:
    """A products.json written before extras were per-language must not read as all-missing.

    Wrong in the direction that invents work for the client — the fix is to re-parse, not to
    open a translation queue against a file that simply predates the field.
    """
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001", extras={"functional_name": "haak"})],
            gdsn_extras=_LOCALISED_EXTRA,
        )
    )

    assert _extra_cell(md, "02987653000001") == "●"


def test_a_language_agnostic_extra_is_one_flat_slot() -> None:
    # material has no LanguageCode pair in the feed, so one value fills its only slot.
    md = _render(matrix=_matrix(products=[_p("02987653000001", extras={"material": "PP"})]))

    assert _cell_for(md, "02987653000001", "material") == "●"


def test_the_video_column_reflects_both_languages() -> None:
    gtin = "02987653000001"
    both = _render(
        matrix=_matrix(products=[_p(gtin)], video_confirmed={"nl": {gtin}, "fr": {gtin}})
    )
    one = _render(matrix=_matrix(products=[_p(gtin)], video_confirmed={"nl": {gtin}, "fr": set()}))
    neither = _render(matrix=_matrix(products=[_p(gtin)]))

    assert _cell_for(both, gtin, "video") == "●"
    assert _cell_for(one, gtin, "video") == "◐"
    assert _cell_for(neither, gtin, "video") == "○"


def test_no_matrix_input_omits_the_section() -> None:
    assert "Coverage matrix" not in _render()


def test_an_empty_scope_says_so_rather_than_rendering_an_empty_table() -> None:
    md = _render(matrix=_matrix(products=[]))

    assert "Coverage matrix" in md
    assert "No products in scope" in md


def test_a_field_marked_out_of_the_matrix_gets_no_column() -> None:
    """A column present for every product that feeds nothing is noise in a gaps table.

    `logistics_name` (3297) and `marketing_name` (3318) are pure pass-through: carried verbatim,
    consumed by nothing, so they read present on every SKU and never indicate work.
    """
    md = _render(
        matrix=_matrix(
            products=[_p("02987653000001", extras={"logistics_name": "x", "material": "PP"})],
            gdsn_extras={
                "logistics_name": GdsnSource(sheet="S", attribute="3297", in_matrix=False),
                "material": GdsnSource(sheet="S", attribute="Material"),
            },
        )
    )

    header = next(line for line in md.splitlines() if line.startswith("| # |"))
    assert "logistics" not in header
    assert "material" in header  # the neighbouring optional column is untouched


# --- §2, the copy blocker: one section, saying what it lists (R-b, R-c, and the §1c rewrite) ----
#
# §1 used to hold four subsections. §1a (E23) and §1b (E24) repeated what §0's matrix already
# shows; §1d repeated the matrix's `product·3301` / `image·2485` columns and leaked a
# whole-catalogue GTIN into a scoped report. What is left is the copy block, which had drifted
# furthest of all: it said "the generator produced nothing for these units", while what it lists
# is products whose attr 1083 is blank in a language.


def _blank_1083(gtin: str, language: str = "nl") -> SourceIssue:
    return _issue(gtin, "missing_generation_input", field=f"description_short.{language}")


def test_the_redundant_subsections_are_gone() -> None:
    md = _render(
        generated_issues=[_blank_1083("02987653000001")],
        mandatory_gaps={"02987653000002": [MandatoryGap("brand", "", "3336")]},
        video_held=["02987653000003"],
        source_issues=[_issue("02987653000004", "blank_value", field="image_url")],
        products=_products(*[f"0298765300000{n}" for n in range(1, 5)]),
    )

    # Collapsed into the section itself, not renamed: no subsection of any letter survives, and
    # `_heading` fails unless the section appears exactly once.
    assert not _subheads(md, _COPY)


def test_the_copy_blocker_says_what_it_actually_lists() -> None:
    """It is a source-data finding about attr 1083, not a report on whether generation ran.

    The old wording sent an operator to re-run generation for a gap re-running cannot close, and
    under scoped generation it would have read as an accusation about every already-live unit.
    """
    md = _render(
        generated_issues=[_blank_1083("02987653000001")],
        products=_products("02987653000001"),
    )

    assert "1083" in md
    assert "The generator produced nothing" not in md
    # The distinction that stops it being read as a generation failure.
    assert "already live" in md


#: A held SKU as E23 reports it: the either-or group unsatisfied, per language.
def _group_gap(*languages: str) -> list[MandatoryGap]:
    return [MandatoryGap("marketing_copy", lang, "1083/1067") for lang in languages]


def _blocked(gtin: str, gaps: list[MandatoryGap], **over: object) -> str:
    product = _p(gtin, **over)
    return _render(
        products={product.gtin14: product},
        mandatory_gaps={product.gtin14: gaps},
        matrix=_matrix(products=[product], gdsn_map=_GROUP_MAP),
    )


def test_the_copy_blocker_shows_every_slot_of_the_requirement_on_one_row() -> None:
    """One row per SKU, one column per (attribute, language) — the cell to fill, named.

    It used to be one row per *language* with an identical consequence in each, and §0 no longer
    says which member is missing: #103 collapsed 1083 and 1067 into one `marketing·copy` column
    precisely because neither is individually mandatory. So this is the only place left that can
    say which of the four slots to fill, and it now says it.
    """
    md = _blocked("02987653000001", _group_gap("nl", "fr"))
    section = _section(md, _COPY)
    header = next(line for line in section.splitlines() if line.startswith("| GTIN |"))

    assert [c.strip() for c in header.split("|")[1:-1]] == [
        "GTIN",
        "1083 nl",
        "1083 fr",
        "1067 nl",
        "1067 fr",
        "Consequence",
    ]
    assert len([line for line in section.splitlines() if line.startswith("| `0298")]) == 1


def test_the_grid_shows_which_slot_the_feed_does_carry() -> None:
    """The case the old layout rendered as two rows to be diffed by eye.

    1083 present in nl, nothing else: the SKU is held for fr, and the row says exactly that
    rather than leaving the reader to compare a `nl` row against a `fr` row.
    """
    md = _blocked(
        "02987653000001",
        _group_gap("fr"),
        description_short=LocalisedText(values={"nl": "Kort en krachtig"}),
    )
    row = next(
        line for line in _section(md, _COPY).splitlines() if line.startswith("| `02987653000001`")
    )

    assert [c.strip() for c in row.split("|")[2:-2]] == ["●", "○", "○", "○"]
    assert "fr" in row.split("|")[-2]


def test_the_copy_blocker_lists_what_the_requirement_holds_not_every_blank_1083() -> None:
    """The title says "blocks publish", so the rows have to be the ones that block.

    It listed every unit with a blank attr 1083 — including those whose 1067 carries the copy,
    which publish perfectly well. That was accurate only by luck: no in-scope unit is in that
    state today, so the "publishes from 1067" row never appeared. The first one would have sat
    under a heading its own cells disproved.

    Such a unit is now reported nowhere, and that is a deliberate consequence of two decisions
    taken together: §0 shows the requirement rather than its members, and §4 no longer lists
    blank fields because the matrix does. It costs a finding no in-scope product has ever had —
    1083 is the primary copy source and 1067 the fallback, so carrying only the fallback is the
    unusual direction. Worth revisiting if a product ever turns up in that state.
    """
    products = _products("02987653000001")
    gtin14 = next(iter(products))
    products[gtin14] = products[gtin14].model_copy(
        update={"description_long": LocalisedText(values={"nl": "a", "fr": "b"})}
    )
    md = _render(
        generated_issues=[_blank_1083("02987653000001")],
        products=products,
        mandatory_gaps={},  # 1067 satisfies the group, so E23 holds nothing
        matrix=_matrix(products=[_p("02987653000001")], gdsn_map=_GROUP_MAP),
    )

    assert "02987653000001" not in _section(md, _COPY)


def test_held_gtins_are_named_under_a_heading_that_says_they_block() -> None:
    """The hold comes from the either-or gap now, not from a `missing_generation_input` finding.

    A blank attr 1083 is half of a requirement and holds nothing on its own — which is why this
    test used to pass with no E23 gap anywhere in its inputs.
    """
    md = _blocked(
        "02987653003276",
        _group_gap("nl", "fr"),
        product_name=LocalisedText(values={"nl": "plasmaaansteker", "fr": "briquet"}),
    )

    assert "**Held**" in md
    assert "02987653003276" in md
    assert "plasmaaansteker" in md  # product name resolved
    assert "BLOCKS" in md.upper()


def test_a_sku_held_for_something_else_is_not_listed_as_a_copy_block() -> None:
    """§2 answers for one requirement, so it filters the gaps to that requirement.

    Two in-scope SKUs are held for `image_url` alone. Taking every gap on the product would put
    them in this grid with all four copy slots ○ and a consequence claiming the copy is what
    holds them — a false blocker, in the section whose job is to be believed.
    """
    product = _p("02987653007922")
    md = _render(
        products={product.gtin14: product},
        mandatory_gaps={product.gtin14: [MandatoryGap("image_url", "", "2485")]},
        matrix=_matrix(products=[product], gdsn_map=_GROUP_MAP),
    )

    assert "02987653007922" not in _section(md, _COPY)


def test_a_held_unit_is_not_also_listed_as_a_non_blocking_source_fix() -> None:
    """§2 and §4 must not claim the same unit: one says it blocks, the other says it does not."""
    md = _render(
        generated_issues=[_blank_1083("02987653000001", "nl")],
        products=_products("02987653000001"),
        mandatory_gaps={"02987653000001": _group_gap("nl")},
        matrix=_matrix(products=[_p("02987653000001")], gdsn_map=_GROUP_MAP),
    )

    assert "02987653000001" in _section(md, _COPY)
    assert "02987653000001" not in _section(md, _MYGS1)


def test_the_copy_blocker_names_both_attributes_of_the_requirement() -> None:
    """Naming only 1083 said the requirement was 1083, which is the misreading this fixes."""
    md = _blocked("02987653000001", _group_gap("nl", "fr"))
    heading = _heading(md, _COPY)

    assert "1083" in heading and "1067" in heading


def _inference(gtin: str, language: str, claim: str) -> SourceIssue:
    return _issue(
        gtin, "generation_inference", field=f"generated_description.{language}", value=claim
    )


def test_the_claims_section_puts_each_language_in_its_own_column() -> None:
    """One row per product, a column per configured language.

    A row per (GTIN, language) put the two claims for one product on adjacent rows, to be read as
    a pair by eye — and they are **not** translations of each other: on the real report `…3344`'s
    Dutch claim derives the placement from the product type while the French one also derives the
    pain relief from the Dutch 1083. Side by side they can be compared. It also stops the section
    growing a row per language: 39 rows became 20, and a third language would have made it 59.
    """
    md = _render(
        generated_issues=[
            _inference("02987653000001", "nl", "afgeleid van het producttype"),
            _inference("02987653000001", "fr", "déduit du type de produit"),
        ],
        products=_products("02987653000001"),
    )
    section = _section(md, _CLAIMS)
    header = next(line for line in section.splitlines() if line.startswith("| GTIN |"))

    assert [c.strip() for c in header.split("|")[1:-1]] == [
        "GTIN",
        "Claim to verify (nl)",
        "Claim to verify (fr)",
    ]
    rows = [line for line in section.splitlines() if line.startswith("| `0298")]
    assert len(rows) == 1
    assert "afgeleid van het producttype" in rows[0]
    assert "déduit du type de produit" in rows[0]
    # Both numbers, and the singular: the Summary counts claims while the table counts products,
    # so the sentence carrying both is the only thing that stops them reading as a contradiction.
    assert "2 claims across 1 product." in section


def test_a_claim_in_one_language_only_leaves_the_other_cell_empty() -> None:
    """An empty cell is the finding: nothing was inferred in that language."""
    md = _render(
        generated_issues=[_inference("02987653000001", "nl", "alleen Nederlands")],
        products=_products("02987653000001"),
    )
    row = next(line for line in _section(md, _CLAIMS).splitlines() if line.startswith("| `0298"))

    assert [c.strip() for c in row.split("|")[1:-1]][1:] == ["alleen Nederlands", ""]


def test_the_translation_section_keeps_one_row_per_field_and_a_column_per_language() -> None:
    """§5's stable key is (GTIN, field); the language is the axis that multiplies.

    Every translation is French today, so this collapses nothing — the point is that it does not
    *grow* when a language is added. `material` and `product_name` stay separate rows because the
    "Source attribute" tells the operator where to paste, and that differs per field.
    """
    md = _render(
        generated_issues=[
            _translated("02987653000001", "material.fr", "métal", "attr Material"),
            _translated("02987653000001", "product_name.fr", "Brosse", "attr 3301"),
        ],
        products=_products("02987653000001"),
    )
    section = _section(md, _TRANSLATED)
    header = next(line for line in section.splitlines() if line.startswith("| GTIN |"))

    assert [c.strip() for c in header.split("|")[1:-1]] == [
        "GTIN",
        "Source attribute",
        "Value to paste (nl)",
        "Value to paste (fr)",
    ]
    rows = [line for line in section.splitlines() if line.startswith("| `0298")]
    assert len(rows) == 2  # one per field, not one per language
    assert all(row.split("|")[3].strip() == "" for row in rows)  # nothing to paste in nl


def test_both_sections_widen_with_the_configured_languages() -> None:
    """The reason for the whole change, and the only part today's export cannot show.

    Columns come from `wordpress.languages`, so adding German widens the tables and changes no
    code. As rows, a third language would have taken §3 from 39 to ~59 and §5 from 21 to ~41.
    """
    md = _render(
        languages=["nl", "fr", "de"],
        generated_issues=[
            _inference("02987653000001", "de", "aus dem Produkttyp abgeleitet"),
            _translated("02987653000001", "material.de", "Metall", "attr Material"),
        ],
        products=_products("02987653000001"),
    )

    for section, label in ((_CLAIMS, "Claim to verify"), (_TRANSLATED, "Value to paste")):
        header = next(
            line for line in _section(md, section).splitlines() if line.startswith("| GTIN |")
        )
        assert f"{label} (de)" in header, section
        assert [c for c in header.split("|") if "(nl)" in c or "(fr)" in c or "(de)" in c] != []
    assert "aus dem Produkttyp abgeleitet" in md
    assert "Metall" in md


def test_the_video_backlog_carries_no_html_and_is_bounded() -> None:
    """The report is read as raw markdown, so `<details>` neither folds nor hides anything.

    The old §5 wrapped 118 filenames in `<details><summary>` to keep the document short. That
    works on a rendering surface and does nothing on the one the operator actually reads: the tags
    show as text and every filename is expanded inline. Same two-surfaces problem as the `<br>`
    labels, pointing the other way — so the section is short by *being* short.
    """
    issues = [
        _issue("", "video_unconfirmed", field="video.nl", value=f"clip{n}.mpg") for n in range(30)
    ]
    md = _render(video_map_issues=issues)
    section = _section(md, _VIDEO)

    assert "<details>" not in md and "<summary>" not in md
    assert "**30**" in section  # the count is the headline
    listed = [line for line in section.splitlines() if line.startswith("- `")]
    assert len(listed) == _VIDEO_SAMPLE
    assert f"{30 - _VIDEO_SAMPLE} more" in section  # nothing is hidden silently


def test_a_short_video_backlog_is_listed_in_full() -> None:
    """Below the sample size there is no remainder to announce."""
    issues = [_issue("", "video_unconfirmed", field="video.nl", value="clip.mpg")]
    section = _section(_render(video_map_issues=issues), _VIDEO)

    assert len([line for line in section.splitlines() if line.startswith("- `")]) == 1
    assert "more" not in section


def test_the_claims_section_says_where_on_the_page_the_claim_appears() -> None:
    """A business user reads §3 without knowing what `generated_description` is.

    The claim is real and the row names the product, but nothing said *where* on the page the
    text sits — so verifying it meant guessing between the title, the summary line and the spec
    table. Every claim today is in the description body; the section says so.
    """
    md = _render(
        generated_issues=[_inference("02987653000001", "nl", "afgeleid")],
        products=_products("02987653000001"),
    )
    section = _section(md, _CLAIMS)

    assert "description" in section.lower()
    assert "product page" in section.lower()


def test_the_generated_copy_count_says_which_copy_it_counted() -> None:
    """Under scoped generation the number means "written this run", not "all in-scope copy".

    A count whose meaning moves without the words moving is the failure R-a is also about.
    """
    gen = [
        _issue("02987653000001", "content_generated", field="generated_description.nl", value="s")
    ]

    md = _render(generated_issues=gen, products=_products("02987653000001"))

    assert "1 generated-copy row(s) written this run" in md


def test_the_everything_live_report_leads_with_its_inventory_and_drops_run_notes() -> None:
    """Run notes describe one run; beside an inventory of every live product they mislead.

    Found on the first real render: August's "all 12 published products … with video showing"
    sat under an inventory showing 13 of 16 with no video.
    """
    md = _render(
        preface=["## Live on the site", "", "inventory", ""],
        observations=["All 12 published products are live."],
        live=True,
    )

    assert md.splitlines()[0] == "# Democlient — Data quality report — complete"
    assert "report_quality democlient --complete" in md
    assert md.index("## Live on the site") < md.index("## Summary")
    assert "Observations (this run)" not in md
    assert "All 12 published" not in md
