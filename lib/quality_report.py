"""Render the four per-step issue reports into one human-readable markdown worklist.

The pipeline writes several machine-readable issue files
(``source_issues.json``, ``generated_issues.json``, ``video_map_issues.json``,
``category_issues.json``) — each a flat list of :class:`~lib.records.SourceIssue`. This module
folds them into a single report grouped by **owner and action** (what blocks publishing, what a
human must review, what the client fixes in MyGS1), so the operator has one thing to read and hand
off rather than four JSON files.

Pure and deterministic: :func:`render_quality_report` takes already-parsed inputs (including the
snapshot date and per-source freshness) and returns markdown. All I/O and clock access live in
``scripts/report_quality.py``, so the same inputs render byte-identically and the renderer is unit
-testable without touching the filesystem. Each section is built by a small ``_*_lines`` helper.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from lib.gdsn import is_mandatory
from lib.mandatory import MandatoryGap, value_for
from lib.quality_report_balance import THIN_TEXT, TITLE, balance_lines
from lib.quality_report_video import VideoReport, held_count, summary_rows, video_lines
from lib.records import ProductRecord, SourceIssue
from lib.report_markdown import cell as _cell
from lib.report_markdown import table as _table

if TYPE_CHECKING:
    from lib.gdsn import GdsnSource

#: Issue kinds emitted by the generator merge (``generated_issues.json``).
_HELD = "missing_generation_input"
_INFERENCE = "generation_inference"
_GENERATED = "content_generated"
#: A value the generator rendered into a language the feed lacked — §5's MyGS1 work queue.
_TRANSLATED = "value_translated"
#: Issue kinds emitted by the export parser (``source_issues.json``).
_BLANK = "value_blank"
_INCONSISTENT = "value_inconsistent_across_markets"
_WRONG_LANG = "value_wrong_language"

#: Length of an ISO ``YYYY-MM-DD`` freshness date. Anything else (``—``) is "unknown", not old.
_ISO_DATE_LEN = 10

#: Base fields whose blank makes a published page broken/degraded enough to block it: the
#: page title (``product_name``) and the hero image (``image_url``). A blank in any other
#: field (e.g. ``net_content``) only degrades a detail line, so it is a source fix, not a block.
_BLOCKING_BLANK_FIELDS = frozenset({"product_name", "image_url"})


#: Matrix cell marks. A filled circle reads as "there" at a glance across 30 rows in a way that
#: a tick and a dash do not, and the half circle says "one language only" without a legend lookup.
_PRESENT = "●"
_PARTIAL = "◐"
_ABSENT = "○"

#: Column header for the video pair — not a ``gdsn_map`` field, but the same kind of fact.
_VIDEO_COLUMN = "video"

#: Suffix marking a column whose gap only thins the page. A word, not a symbol: this report is read
#: both rendered and as raw markdown, so anything HTML shows as a tag in the second — and a bare
#: mark (`~`, bold) still sends the reader to the legend to find out what it meant.
_OPTIONAL_LABEL = "(optional)"


class MatrixInput(NamedTuple):
    """Everything the §0 coverage matrix needs, gathered by the caller.

    Bundled rather than passed as five more keyword arguments: they are one fact — "the scope and
    the shape of its data" — and a renderer signature that already takes nine sources does not
    need five more that must always travel together.
    """

    #: In-scope products, in any order; the matrix sorts them itself.
    products: list[ProductRecord]
    gdsn_map: dict[str, GdsnSource]
    gdsn_extras: dict[str, GdsnSource]
    #: Language → GTIN-14s with a client-confirmed video in it.
    video_confirmed: dict[str, set[str]]


class FieldColumn(NamedTuple):
    """One column of the coverage matrix: where the value comes from and whether it is required."""

    #: The field(s) behind the column. More than one for a ``required_group``, which is **one**
    #: requirement satisfied by any member — so it is one column, and a member's own gap says
    #: nothing on its own. Rendering the members separately made the legend's "a gap here holds
    #: the whole SKU" untrue of each of them.
    fields: tuple[str, ...]
    #: Short header. GDSN attribute numbers are what an operator searches MyGS1 by, so they win
    #: over the internal field name wherever there is one. A group is named for the requirement.
    header: str
    localised: bool
    required: bool


#: The one column that does not come from the feed: a client-confirmed video, per language.
#: Mandatory because a GTIN without one is held out of publishing entirely (E24), so it belongs
#: with the other columns whose gap stops the SKU rather than off at the end of the row.
_VIDEO = FieldColumn((_VIDEO_COLUMN,), _VIDEO_COLUMN, localised=True, required=True)


def _group_columns(
    gdsn_map: dict[str, GdsnSource], gdsn_extras: dict[str, GdsnSource]
) -> dict[str, FieldColumn]:
    """One column per ``required_group``, keyed by group name.

    Built across **both** maps, the way :func:`lib.mandatory.missing_mandatory` groups them, so a
    group whose members are split between them stays one requirement rather than becoming two
    half-columns that each say nothing.

    A member with ``in_matrix: false`` still counts towards satisfying the group — that flag says
    "this value needs no column of its own", not "this value does not exist". The group appears as
    long as at least one member asked for a column.
    """
    members: dict[str, list[tuple[str, GdsnSource]]] = defaultdict(list)
    for name, src in [*gdsn_map.items(), *gdsn_extras.items()]:
        if src.required_group:
            members[src.required_group].append((name, src))
    return {
        group: FieldColumn(
            tuple(name for name, _ in group_members),
            group.replace("_", "·"),
            any(src.localised for _, src in group_members),
            True,
        )
        for group, group_members in members.items()
        if any(src.in_matrix for _, src in group_members)
    }


def _either_or_sentence(gdsn_map: dict[str, GdsnSource], gdsn_extras: dict[str, GdsnSource]) -> str:
    """The legend clause naming each ``required_group`` and the attributes that satisfy it.

    A collapsed column reads as one more mandatory field otherwise, and the attribute numbers are
    how an operator finds the value in MyGS1 — the same reason mapped columns are headed by their
    number rather than by the internal field name. Empty for a client with no group at all, rather
    than a sentence explaining a concept their report does not contain.
    """
    sources = {**gdsn_map, **gdsn_extras}
    clauses = []
    for column in _group_columns(gdsn_map, gdsn_extras).values():
        attributes = [sources[f].attribute for f in column.fields if sources[f].attribute]
        joined = " or ".join(f"attr {a}" for a in attributes)
        clauses.append(f"`{column.header}` is one requirement, satisfied by {joined}")
    return (" " + "; ".join(clauses) + ".") if clauses else ""


def _columns(
    gdsn_map: dict[str, GdsnSource], gdsn_extras: dict[str, GdsnSource]
) -> list[FieldColumn]:
    """Matrix columns, every mandatory one first, derived from config rather than listed here.

    Derived so the matrix cannot drift from what the pipeline actually enforces: marking a field
    ``required`` in ``clients.yml`` moves it into the mandatory block here with no code change,
    which is the whole point of a coverage table nobody has to maintain by hand. **Both maps are
    read for it** — an extra marked ``required`` is mandatory exactly as a mapped field is, and
    hard-coding every extra optional here made a client's flag mean nothing.

    **This is the only thing that orders the matrix** — the renderer takes the list as given and
    neither re-sorts nor splices. Otherwise the header could group the columns one way while the
    cells were built another, and the test pinning a single mandatory→optional crossing would be
    pinning the renderer's own partition instead of this config-derived order.

    The two maps keep separate header formats because they identify a column differently: a
    mapped field is searched in MyGS1 by its GDSN attribute number, an extra by its own name.
    """
    groups = _group_columns(gdsn_map, gdsn_extras)
    emitted: set[str] = set()

    def build(
        sources: dict[str, GdsnSource], header: Callable[[str, GdsnSource], str]
    ) -> list[FieldColumn]:
        out: list[FieldColumn] = []
        for name, src in sources.items():
            if src.required_group:
                # One column per group, at the position of its first member in declaration order.
                if src.required_group in emitted or src.required_group not in groups:
                    continue
                emitted.add(src.required_group)
                out.append(groups[src.required_group])
            elif src.in_matrix:
                out.append(
                    FieldColumn((name,), header(name, src), src.localised, is_mandatory(src))
                )
        return out

    mapped = build(gdsn_map, lambda name, src: f"{name.split('_')[0]}·{src.attribute}")
    extras = build(gdsn_extras, lambda name, _src: name.replace("_", "·"))
    mandatory = [
        *(c for c in mapped if c.required),
        _VIDEO,
        *(c for c in extras if c.required),
    ]
    return [
        *mandatory,
        *(c for c in mapped if not c.required),
        *(c for c in extras if not c.required),
    ]


def _mark(
    product: ProductRecord,
    column: FieldColumn,
    languages: list[str],
    video_confirmed: dict[str, set[str]],
) -> tuple[str, int]:
    """The cell for one product/column, and how many language slots it fills (for the sort).

    A ``gdsn_extras`` field is counted per language when the record actually carries it that way
    (:attr:`ProductRecord.extras_localised`) and as a single slot otherwise. The distinction is
    the record's, not the config's: ``localised`` describes the *source attribute*, and a
    ``products.json`` written before extras were kept per language holds one flat string however
    the attribute looked. Counting that flat value as a language group would find nothing and
    report every extra missing — wrong in the direction that invents work for the client.
    """
    if column is _VIDEO:
        # Confirmation is per language, which is the shape _language_mark already reads: a video
        # in one of two languages is the same half-filled cell as a name in one of two.
        confirmed = {
            lang: "confirmed"
            for lang in languages
            if product.gtin14 in video_confirmed.get(lang, set())
        }
        return _language_mark(confirmed, languages)
    if len(column.fields) > 1:
        return _group_mark(product, column, languages)
    field = column.fields[0]
    if field not in type(product).model_fields:
        localised = product.extras_localised.get(field)
        if localised is None:
            filled = bool(str(product.extras.get(field) or "").strip())
            return (_PRESENT if filled else _ABSENT), int(filled)
        return _language_mark(localised.values, languages)
    value = getattr(product, field, None)
    if not column.localised:
        filled = bool(str(value or "").strip())
        return (_PRESENT if filled else _ABSENT), int(filled)
    return _language_mark(getattr(value, "values", {}) or {}, languages)


def _group_mark(
    product: ProductRecord, column: FieldColumn, languages: list[str]
) -> tuple[str, int]:
    """The cell for a ``required_group``: satisfied where **any** member carries a value.

    Unioned per language rather than taken from the best member, because one member in nl and the
    other in fr leaves nothing missing: member-wise both read half-filled, and the SKU would show
    ◐ while being fully publishable. It asks through :func:`lib.mandatory.value_for` — the same
    function E23 asks — so the column and the hold cannot disagree about what "present" means.

    It fills at most one language-slot per language however many members carry a value: the
    requirement is satisfied once, and ``score`` sorts the worklist by how close a SKU is to
    publishable, not by how much text the feed happens to hold.
    """
    if not column.localised:
        filled = any(value_for(product, field, "") for field in column.fields)
        return (_PRESENT if filled else _ABSENT), int(filled)
    satisfied = {
        lang: "satisfied"
        for lang in languages
        if any(value_for(product, field, lang) for field in column.fields)
    }
    return _language_mark(satisfied, languages)


def _language_mark(values: dict[str, str], languages: list[str]) -> tuple[str, int]:
    """The cell for a per-language value: full, half, or empty, plus the slots it fills."""
    have = sum(bool(str(values.get(lang) or "").strip()) for lang in languages)
    if have == len(languages):
        return _PRESENT, have
    return (_PARTIAL if have else _ABSENT), have


def _stalest(freshness: dict[str, str]) -> str:
    """The oldest input date, or "" when none is known.

    Dates are ISO ``YYYY-MM-DD`` so they sort lexically; anything unparseable (``—``) is ignored
    rather than treated as ancient, which would put a scare in the header on missing information.
    """
    dates = [v for v in freshness.values() if len(v) == _ISO_DATE_LEN and v[:4].isdigit()]
    return min(dates) if dates else ""


def _blocks_publish(field: str) -> bool:
    """True when a blank in ``field`` should hold the GTIN out of publishing (title/image)."""
    return field.split(".", 1)[0] in _BLOCKING_BLANK_FIELDS


def _market_cell(issue: SourceIssue) -> str:
    """All target-market values for a cross-market conflict, chosen (highest-ranked) marked ✓.

    Shows the conflicting texts side by side so a reader can compare on the spot instead of
    seeing only the winning value. Falls back to the bare value for legacy issues that predate
    the structured ``market_values`` breakdown.
    """
    if not issue.market_values:
        return _cell(issue.value)
    parts = [
        f"{market}=`{_cell(value)}`" + (" ✓" if idx == 0 else "")
        for idx, (market, value) in enumerate(issue.market_values)
    ]
    return " · ".join(parts)


def _name(products: dict[str, ProductRecord], gtin: str) -> str:
    """Best-effort product label for a GTIN (nl name, then fr); blank if unknown."""
    if not gtin.isdigit():
        return ""
    product = products.get(gtin.zfill(14))
    if product is None or product.product_name is None:
        return ""
    values = product.product_name.values
    return values.get("nl") or values.get("fr") or ""


def _short(gtin: str) -> str:
    """The trailing four digits operators use to refer to a GTIN in chat."""
    return "…" + gtin[-4:] if gtin else "(unassigned)"


def _lang(field: str) -> str:
    """The language suffix of a dotted field like ``generated_description.nl`` (else empty)."""
    return field.rsplit(".", 1)[-1] if "." in field else ""


def _label(products: dict[str, ProductRecord], gtin: str) -> str:
    """A ``\\`gtin\\` (…1234) — Name`` cell, name omitted when unknown."""
    name = _name(products, gtin)
    return f"`{gtin}` ({_short(gtin)})" + (f" — {name}" if name else "")


def _header_lines(
    client_id: str, snapshot: str, freshness: dict[str, str], *, live: bool = False
) -> list[str]:
    """The title block, leading with **when this document was written**.

    On its own line, in local time with the zone named, and first — because the question a reader
    asks of a worklist is how old it is, and the answer used to be a date buried mid-sentence.
    Whoever opens the file sees it without checking a file browser or asking the person who ran it.

    The generation time and the *input* freshness are deliberately separate facts: a report can be
    minutes old and still describe a month-old export, which is exactly the case here.
    """

    def f(key: str) -> str:
        return freshness.get(key, "—")

    stale = _stalest(freshness)
    return [
        f"# {client_id.title()} — Data quality report" + (" — complete" if live else ""),
        "",
        f"**Generated {snapshot}**",
        "",
        f"_Describes data as of: generated `{f('generated')}`, source `{f('source')}`, "
        f"video-map `{f('video_map')}`, categories `{f('category')}`."
        + (
            f" **The oldest input is from {stale}** — anything fixed since is not reflected here._"
            if stale
            else "_"
        ),
        "",
        f"> Regenerate the underlying data: `run_plan {client_id}` (generated + categories), "
        f"`parse_export {client_id}` (source), `build_video_map {client_id} --check` (video-map); "
        f"then `python -m scripts.report_quality {client_id}{' --complete' if live else ''}`.",
        "",
    ]


def _summary_lines(  # noqa: PLR0913 — one parameter per source feeding a summary row
    generated_issues: list[SourceIssue],
    source_issues: list[SourceIssue],
    video_map_issues: list[SourceIssue],
    category_issues: list[SourceIssue],
    mandatory_gaps: dict[str, list[MandatoryGap]],
    video_held: list[str],
    video: VideoReport | None,
) -> list[str]:
    by_kind = Counter(i.issue for i in generated_issues)
    held = {i.gtin for i in generated_issues if i.issue == _HELD}
    blanks = [i for i in source_issues if i.issue == _BLANK]
    blocking_blanks = [i for i in blanks if _blocks_publish(i.field)]
    degrade_blanks = [i for i in blanks if not _blocks_publish(i.field)]
    inconsistent = [i for i in source_issues if i.issue == _INCONSISTENT]
    rows = [
        [
            "Source",
            "**Missing mandatory data (E23)**",
            f"{sum(len(g) for g in mandatory_gaps.values())} gaps / {len(mandatory_gaps)} GTINs",
            "Client (MyGS1)",
            "**Yes — whole SKU**",
        ],
        [
            "Media",
            "**No confirmed video (E24)**",
            held_count(video, video_held, mandatory_gaps),
            "Client",
            "**Yes — whole SKU**",
        ],
        [
            "Copy",
            "No marketing message (1083)",
            f"{by_kind[_HELD]} rows / {len(held)} GTINs",
            "Client (MyGS1)",
            # Not a flat "Yes": a unit whose 1067 carries copy publishes from it. §2 marks which
            # rows are which, and an unqualified blocker count that includes non-blockers is how
            # the real ones stop being urgent.
            "**Yes** — where 1067 is blank too",
        ],
        [
            "Source",
            "Blank title / image",
            str(len(blocking_blanks)),
            "Client (MyGS1)",
            "**Yes**",
        ],
        [
            "Copy",
            "Inferred claims to verify",
            str(by_kind[_INFERENCE]),
            "Operator/Client",
            "Review",
        ],
        [
            "Source",
            "Blank non-critical fields",
            str(len(degrade_blanks)),
            "Client (MyGS1)",
            "Degrade only",
        ],
        [
            "Source",
            "Cross-market inconsistencies",
            str(len(inconsistent)),
            "Client (MyGS1)",
            "No — but review",
        ],
        [
            "Source",
            "Possible wrong-language values",
            str(len([i for i in source_issues if i.issue == _WRONG_LANG])),
            "Client (MyGS1)",
            "Worth a glance",
        ],
        [
            "Source",
            TITLE,
            str(
                len(
                    {
                        (i.gtin, i.field.split(".", 1)[0])
                        for i in source_issues
                        if i.issue == THIN_TEXT
                    }
                )
            ),
            "Client (MyGS1)",
            "No — the page says less",
        ],
        [
            "Source",
            "Values translated to fill a language gap",
            str(len([i for i in generated_issues if i.issue == _TRANSLATED])),
            "Client (MyGS1)",
            "No — §5 to paste back",
        ],
        *summary_rows(video, video_map_issues),
        [
            "Category",
            "Unmapped GPC bricks",
            str(len(category_issues)),
            "Operator",
            "—" if not category_issues else "Yes",
        ],
    ]
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return [
        "## Summary",
        "",
        "| Area | Finding | Count | Owner | Blocks publish? |",
        "|---|---|--:|---|---|",
        *body,
        "",
    ]


def _matrix_lines(  # noqa: PLR0913 — a matrix needs its rows, its columns, and their sources
    products: list[ProductRecord],
    gdsn_map: dict[str, GdsnSource],
    gdsn_extras: dict[str, GdsnSource],
    languages: list[str],
    video_confirmed: dict[str, set[str]],
    names: dict[str, ProductRecord],
) -> list[str]:
    """Per-SKU coverage of every field, richest first.

    One row per in-scope product, one column per configured field plus the video pair. Sorted by
    how much is filled, so the SKUs closest to publishable sit at the top and the worst-served at
    the bottom — the order someone works in, rather than GTIN order, which carries no information.

    Mandatory columns come first and ``score`` sits on the boundary, so the two groups are
    separated by the number that only counts the first of them: a gap before ``score`` stops the
    SKU, a gap after it only thins the page. Those two facts look identical in a matrix otherwise.

    **Saying which is which took four goes, and only the last reads without a legend.**
    ``**bold**`` was invisible, markdown making header cells bold anyway. A ``MANDATORY<br>``
    group label rendered correctly and put a literal HTML tag in front of anyone reading the
    markdown as text — this report has both surfaces. A trailing ``~`` was plain on both and
    meant nothing on its own. The word ``(optional)`` needs no decoding.

    **``score`` counts the mandatory columns only**, because the question it answers is how close
    this SKU is to publishable, and the table is sorted by it. Counting everything let a product
    with both optional values and a missing mandatory one outrank a publishable one — putting the
    wrong SKU at the top of a worklist whose whole purpose is the order.
    """
    if not products:
        return ["## 0. Coverage matrix", "", "_No products in scope._", ""]

    columns = _columns(gdsn_map, gdsn_extras)
    # Mandatory first is `_columns`' contract and a test pins the single crossing, so counting
    # them is enough to know where the boundary falls — nothing here re-sorts or re-partitions.
    split = sum(1 for c in columns if c.required)
    rows: list[tuple[int, list[str]]] = []
    for product in products:
        gtin = product.gtin14
        cells, score = [], 0
        for index, column in enumerate(columns):
            mark, filled = _mark(product, column, languages, video_confirmed)
            cells.append(mark)
            score += filled if index < split else 0
        name = _name(names, gtin)[:20]
        rows.append((score, [f"`{gtin}`", name, *cells[:split], str(score), *cells[split:]]))

    # Descending by mandatory fill, then by GTIN so equal rows keep a stable order between runs.
    rows.sort(key=lambda r: (-r[0], r[1][0]))
    header = (
        ["#", "GTIN", "Name"]
        + [c.header for c in columns[:split]]
        + ["score"]
        + [f"{c.header} {_OPTIONAL_LABEL}" for c in columns[split:]]
    )
    # The counter is applied after the sort: it numbers the worklist as shown, so "row 12" means
    # the same thing to two people reading the same report.
    numbered = [[str(n), *cells] for n, (_, cells) in enumerate(rows, start=1)]
    return [
        "## 0. Coverage matrix",
        "",
        f"**{len(products)} SKU{'s' if len(products) != 1 else ''} in scope**. "
        f"{_PRESENT} present · {_PARTIAL} one language only · {_ABSENT} missing. The columns "
        f"before `score` are mandatory — a gap there holds the whole SKU; `score` counts their "
        f"filled language-slots, so a localised field contributes {len(languages)}, and the "
        f"table is sorted with the closest to publishable at the top. The columns after `score` "
        f"are marked {_OPTIONAL_LABEL} and only thin the page."
        + _either_or_sentence(gdsn_map, gdsn_extras),
        "",
        *_table(header, numbered),
        "",
    ]


def _has_feature_benefit(products: dict[str, ProductRecord], gtin: str, language: str) -> bool:
    """Whether the feed carries attr 1067 for this unit — the fallback copy is written from.

    Recomputed from ``products.json`` rather than read off the finding, the same rule
    ``report_quality._publish_blocks`` follows for E23/E24: the report says what is true of the
    data *today*, from an export the operator may have replaced since the last run.
    """
    product = products.get(gtin.zfill(14))
    if product is None or product.description_long is None:
        return False
    return bool((product.description_long.values.get(language) or "").strip())


class _Requirement(NamedTuple):
    """One either-or requirement, as §2 needs it: the group's name, its column, and its sources."""

    group: str
    column: FieldColumn
    sources: dict[str, GdsnSource]


def _requirement_rows(
    requirement: _Requirement,
    gaps: dict[str, list[MandatoryGap]],
    products: dict[str, ProductRecord],
    languages: list[str],
) -> tuple[list[str], list[list[str]]]:
    """The header and rows of one either-or requirement's grid: a slot per (attribute, language).

    One row per SKU rather than per language: the hold is per *product* — E23 holds it in every
    language the moment one is short — so a row per language repeated the same consequence and
    left the reader diffing two rows to find the slot to fill.
    """
    fields, sources = requirement.column.fields, requirement.sources
    header = [
        "GTIN",
        *(f"{sources[field].attribute} {lang}" for field in fields for lang in languages),
        "Consequence",
    ]
    rows = []
    for gtin, product_gaps in sorted(gaps.items()):
        short = [g.language for g in product_gaps if g.field == requirement.group]
        if not short:
            continue
        product = products.get(gtin)
        cells = [
            _PRESENT if product is not None and value_for(product, field, lang) else _ABSENT
            for field in fields
            for lang in languages
        ]
        why = (
            "no language carries either"
            if len(short) == len(languages)
            else f"{', '.join(short)} carries neither"
        )
        rows.append([_label(products, gtin), *cells, f"**Held** — {why}"])
    return header, rows


def _blocking_lines(
    mandatory_gaps: dict[str, list[MandatoryGap]],
    products: dict[str, ProductRecord],
    matrix: MatrixInput | None,
    languages: list[str],
) -> list[str]:
    """§2 — the SKUs an either-or source requirement holds, and which slot would release them.

    It was §1 until the video section went in ahead of it, and before that §1c, under a §1 that
    also held three subsections repeating §0's matrix: E23's mandatory gaps (§1a), E24's missing
    videos (§1b), and the blank title/image findings (§1d), whose only GTIN beyond the matrix was
    out of scope entirely — a whole-catalogue finding leaking into a scoped report. All three went,
    and with one subsection left there is no subsection.

    **It listed the wrong population.** The rows came from ``missing_generation_input``, which
    fires on a blank attr 1083 — but 1083 is half of a ``required_group``, so a unit whose 1067
    carries the copy publishes perfectly well. Under a heading reading *"Blocks publish"* that was
    accurate only by luck: no in-scope unit is in that state, so the non-blocking row never
    appeared. It lists what the requirement actually holds now, and a blank 1083 the feed rescues
    is a datapool gap in §4 instead.

    **The grid is here because §0 stopped saying it.** #103 collapsed the members into one
    ``marketing·copy`` column, since neither is individually mandatory — so this is the only place
    left that can name the slot to fill, and one row per SKU says it where one row per *language*
    left two rows to be compared by eye.
    """
    groups = _group_columns(matrix.gdsn_map, matrix.gdsn_extras) if matrix else {}
    sources = {**matrix.gdsn_map, **matrix.gdsn_extras} if matrix else {}
    attributes = [f"attr {sources[field].attribute}" for c in groups.values() for field in c.fields]
    tables: list[str] = []
    for group, column in groups.items():
        header, rows = _requirement_rows(
            _Requirement(group, column, sources), mandatory_gaps, products, languages
        )
        tables += [*_table(header, rows), ""]
    return [
        f"## 2. Blocks publish — no marketing message in the feed ({' or '.join(attributes)})",
        "",
        "A page's copy is written from these attributes, and the requirement is satisfied by "
        "**either** of them. A SKU carrying neither, in any one configured language, has nothing "
        "to write copy from and nothing to translate from — so it is **held out of the plan "
        "entirely**, in *every* language, rather than published half-live.",
        "",
        "**This is a source finding for MyGS1, not a generator failure.** Re-running generation "
        "cannot fill a field the datapool does not have, and a unit already live and unchanged "
        "has no copy written this run by design — neither is what this lists.",
        "",
        "**Action: fill any one ● -less slot on a row, for every language, in MyGS1.**",
        "",
        *(tables or [*_table(["GTIN", "Consequence"], []), ""]),
    ]


def _counted(n: int, noun: str) -> str:
    """``"1 value"`` / ``"21 values"`` — a count and its noun, so a pluralised label cannot lie.

    Both pivoted sections carry two counts (findings, and the rows they fold into) because the
    Summary counts the *work* — one paste, one claim — while the table counts subjects. Stating
    both is what stops a reader counting rows against the Summary and concluding one is wrong.
    """
    return f"{n} {noun}{'' if n == 1 else 's'}"


class _ExtraKey(NamedTuple):
    """Row-key parts beyond the GTIN, with their column titles — kept together so they cannot
    disagree about how many there are."""

    of: Callable[[SourceIssue], tuple[str, ...]]
    headers: tuple[str, ...]


def _by_language(
    issues: list[SourceIssue],
    products: dict[str, ProductRecord],
    languages: list[str],
    value_header: str,
    extra_key: _ExtraKey | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Pivot per-language findings into one row per subject, one column per language.

    **Rows scale with the language count; columns do not.** A row per ``(subject, language)`` was
    two rows for a two-language client and would be three for a three-language one — the shape of
    a table should follow the configured languages rather than however many the current export
    happens to have filled. Columns come from ``wordpress.languages``, so adding German widens
    these tables and changes no code.

    An empty cell is a finding in its own right: nothing was written for that language.

    Args:
        issues: Findings whose ``field`` ends in ``.<language>``.
        products: For the GTIN label.
        languages: The configured site languages, in order — the column set.
        value_header: Column title; each language gets ``"{value_header} ({lang})"``.
        extra_key: Extra row-key parts beyond the GTIN, for a subject that is not the product
            alone — §5 keys on the field too, because the paste target differs per field.

    Returns:
        ``(header, rows)`` ready for :func:`_table`, rows in first-seen order so two runs over the
        same findings render identically.
    """
    rows: dict[tuple[str, ...], dict[str, str]] = {}
    for issue in issues:
        row = rows.setdefault((issue.gtin, *(extra_key.of(issue) if extra_key else ())), {})
        row[_lang(issue.field)] = _cell(issue.value)
    # A finding in a language the client has not configured still gets a column, appended after
    # the configured ones. It should not happen — the generator writes the configured languages —
    # but a `generated_issues.json` left over from a config that named more of them would
    # otherwise drop a MyGS1 paste instruction off the table with nothing to say so. Config leads
    # the order; the anomaly shows up at the end rather than disappearing.
    unexpected = [
        lang
        for lang in dict.fromkeys(found for row in rows.values() for found in row)
        if lang not in languages
    ]
    columns = [*languages, *unexpected]
    headers = extra_key.headers if extra_key else ()
    header = ["GTIN", *headers, *(f"{value_header} ({lang})" for lang in columns)]
    return header, [
        [_label(products, subject[0]), *subject[1:], *(values.get(lang, "") for lang in columns)]
        for subject, values in rows.items()
    ]


def _review_lines(
    inferences: list[SourceIssue],
    generated_count: int,
    products: dict[str, ProductRecord],
    client_id: str,
    languages: list[str],
) -> list[str]:
    header, inf_rows = _by_language(inferences, products, languages, "Claim to verify")
    return [
        "## 3. Review before publish — inferred claims",
        "",
        "Claims the copy makes that go **beyond the literal feed text** (plausible, but derived). "
        "Confirm each holds for the real product before it goes live — this is the actionable "
        "slice of copy review.",
        "",
        "Each claim sits in the **product description** on the product page — the body text under "
        "the title and image, not the title itself and not the specification list below it. That "
        "is the block to read when checking whether a claim holds.",
        "",
        f"One row per product, one column per language: {_counted(len(inferences), 'claim')} "
        f"across {_counted(len(inf_rows), 'product')}. The claims are **not** translations of each "
        "other — each language's copy is written separately, so they can infer different things "
        "about the same product, and side by side you can see where they disagree.",
        "",
        *_table(header, inf_rows),
        "",
        f"_The {generated_count} generated-copy row(s) written this run are reviewed in context "
        "at the operator gate (Review Gate #1); raw text in "
        f"`output/{client_id}/data/generation_results.json`. Copy is written only for the rows a "
        "run publishes, so this counts **this run's batch**, not every unit in scope._",
        "",
    ]


def _source_lines(
    inconsistent: list[SourceIssue],
    wrong_lang: list[SourceIssue],
    thin: list[SourceIssue],
    products: dict[str, ProductRecord],
    languages: list[str],
) -> list[str]:
    """§4 — source findings that are worth fixing but hold nothing.

    **There is no blank-fields subsection.** It listed every non-blocking blank with its source
    attribute, which is what §0's matrix already shows: a blank ``net_content`` is the ○ under
    ``net·3510``, and the header carries the attribute number the subsection was repeating. The
    Summary keeps the count, exactly as it does for the blank title/image findings whose own
    subsection (§1d) went for the same reason.

    What is left is the findings a matrix *cannot* show, because each is about the value rather
    than its presence: markets disagreeing about a value, a value that reads as the wrong language,
    and marketing text far shorter in one language than another (§4c).
    """
    inc_rows = [[_label(products, i.gtin), i.field, _market_cell(i)] for i in inconsistent]
    lang_rows = [[_label(products, i.gtin), i.field, _cell(i.value)] for i in wrong_lang]
    return [
        "## 4. Source-data fixes in MyGS1 (do not block publish)",
        "",
        "### 4a. Values inconsistent across markets",
        "",
        "The same field carries different text across GS1 target markets (priority "
        "`528 > 056 > 276 > 442`); the tool used the highest-ranked (marked ✓). Both/all market "
        "texts are shown so you can compare on the spot and align the authoritative one in MyGS1.",
        "",
        *_table(["GTIN", "Field", "Value per market (✓ = used)"], inc_rows),
        "",
        "### 4b. Possible wrong-language values (worth a glance)",
        "",
        "Heuristic: a localised value carrying letter patterns that belong to the *other* language "
        "(e.g. a French title still reading `Schoonmaakdoek`). Not a blocker — skim and fix the "
        "translation at source if the flag is right.",
        "",
        *_table(["GTIN", "Field", "Value (reads like the wrong language)"], lang_rows),
        "",
        *balance_lines(thin, products, languages, _label),
    ]


def _translated_lines(
    translated: list[SourceIssue], products: dict[str, ProductRecord], languages: list[str]
) -> list[str]:
    """§5 — the values the tool rendered into a language the feed did not carry them in.

    A work queue, not a confession: each row is one paste into MyGS1, after which the next export
    carries the value for real and the tool stops writing it. So unlike §3 — which is a count and
    a pointer, because nobody acts on generated copy row by row — the text belongs in the table.

    Rows whose attribute has no per-language slot in GS1 (attr 4.012 Material) say so instead of
    naming a field, rather than sending the operator to look for one that does not exist.

    One row per filled value, undeduplicated. This used to collapse rows asking for the same paste,
    because attr 3301 was declared twice — as ``product_name`` and again as
    ``extras.functional_name`` — so one MyGS1 cell was reported as two jobs. The duplicate
    declaration is gone and ``lib.config`` now refuses one at load, which is the better place for
    it: a dedupe here would have quietly absorbed the next one instead of surfacing it.
    """
    header, rows = _by_language(
        translated,
        products,
        languages,
        "Value to paste",
        extra_key=_ExtraKey(lambda issue: (_cell(issue.source),), ("Source attribute",)),
    )
    return [
        "## 5. Translated to fill a language gap — paste these into MyGS1",
        "",
        "The feed carries each of these in another language but not in this one, so the tool "
        "**translated it** and the page shows LLM-written text where it should show the client's. "
        "Nothing here blocks publishing. Putting the value back in MyGS1 is what ends that: the "
        "next export carries it for real and the tool stops writing it.",
        "",
        f"**Action: paste each value into the named attribute for that language in MyGS1.** "
        f"{_counted(len(translated), 'value')} to paste, across {_counted(len(rows), 'row')} — a "
        f"row can carry a paste in more than one language.",
        "",
        *_table(header, rows),
        "",
    ]


def _category_lines(category_issues: list[SourceIssue]) -> list[str]:
    body = (
        f"{len(category_issues)} unmapped GPC brick(s) — see `category_issues.json`."
        if category_issues
        else "No unmapped GPC bricks — every product resolves to a site category."
    )
    return ["## 6. Categories", "", body, ""]


def _observations_lines(observations: list[str]) -> list[str]:
    """Free-text, in-session review notes — the assistant's own 'worth a glance' flags.

    Unlike every other section (deterministic renders of the pipeline's issue files), these are
    qualitative judgements the in-session producer wrote while reviewing a run — the same
    heads-ups it would give the user in chat, captured here so they persist beyond the chat.
    """
    body = (
        [f"- {note}" for note in observations]
        if observations
        else ["_None recorded for this run._"]
    )
    return [
        "## Observations (this run)",
        "",
        "Qualitative flags the in-session assistant noted while reviewing this run — not "
        "deterministic checks. Skim and act if relevant.",
        "",
        *body,
        "",
    ]


def render_quality_report(  # noqa: PLR0913 — a document renderer needs each source plus its metadata
    *,
    client_id: str,
    source_issues: list[SourceIssue],
    generated_issues: list[SourceIssue],
    video_map_issues: list[SourceIssue],
    category_issues: list[SourceIssue],
    products: dict[str, ProductRecord],
    snapshot: str,
    freshness: dict[str, str],
    languages: list[str],
    observations: list[str] | None = None,
    mandatory_gaps: dict[str, list[MandatoryGap]] | None = None,
    video_held: list[str] | None = None,
    matrix: MatrixInput | None = None,
    video: VideoReport | None = None,
    preface: list[str] | None = None,
    live: bool = False,
) -> str:
    """Render the consolidated data-quality report as markdown.

    Args:
        client_id: The client the report is for (titles the document).
        source_issues: ``source_issues.json`` (blank / cross-market source-data findings).
        generated_issues: ``generated_issues.json`` (held, generated, and inferred copy).
        video_map_issues: ``video_map_issues.json`` (videos not yet mapped to a GTIN).
        category_issues: ``category_issues.json`` (unmapped GPC bricks).
        products: Parsed products keyed by GTIN-14, for human-readable names.
        snapshot: The report date (injected, so the output is deterministic).
        freshness: Last-updated date per source, keyed ``generated``/``source``/``video_map``/
            ``category`` — each source has its own producer run, so they can differ.
        languages: The client's configured site languages, in order. **One list for the whole
            document**: §0's per-language marks, §3's claim columns and §5's paste columns are all
            derived from it, and two sources for it could disagree about how many columns a table
            has. It is the column set, not the languages the current export happens to have filled
            — a configured language with nothing in it renders as an empty cell, which is itself
            the finding.
        observations: Free-text, in-session review notes (``observations.json``) — the
            assistant's own qualitative flags for this run. ``None``/empty renders a placeholder.
        mandatory_gaps: Missing mandatory source values per GTIN (E23), from
            :func:`lib.mandatory.missing_mandatory`. Computed by the caller rather than derived
            here, because it needs the client's ``gdsn_map`` and this renderer stays pure.
        video_held: GTINs held for want of a client-confirmed video in every language (E24).
        matrix: In-scope products plus the field definitions and video confirmations behind the
            §0 coverage matrix. ``None`` omits the section — a report for a client with no
            ``gdsn_map`` has nothing to tabulate.
        video: The in-scope products joined to the video mapping, and the client's sign-off sheet
            re-planned — §1. ``None`` still renders §1, as one line, so every later section keeps
            its number whether or not the client attaches videos.

        preface: Lines placed after the title block — the complete report's jobs, status table and
            product-list findings (:mod:`lib.complete_report`). ``None`` adds nothing.
        live: The complete report: titled so, its regenerate hint carries ``--complete``, and the
            run notes are left out — they describe one run, and beside every product the client
            sent they read as a claim about all of them.

    Returns:
        The full markdown document.
    """
    inferences = [i for i in generated_issues if i.issue == _INFERENCE]
    generated_count = sum(1 for i in generated_issues if i.issue == _GENERATED)
    translated = [i for i in generated_issues if i.issue == _TRANSLATED]
    # `value_blank` findings are not listed anywhere: §0's matrix is where a missing value is
    # read, one ○ under the column whose header already carries the attribute number. The Summary
    # keeps both counts — blocking (title/image) and degrading — as it has since §1d went.
    inconsistent = [i for i in source_issues if i.issue == _INCONSISTENT]
    wrong_lang = [i for i in source_issues if i.issue == _WRONG_LANG]
    thin = [i for i in source_issues if i.issue == THIN_TEXT]

    lines = [
        *_header_lines(client_id, snapshot, freshness, live=live),
        # The complete report's opening (``lib.complete_report``): jobs, statuses, the list.
        *(preface or []),
        *_summary_lines(
            generated_issues,
            source_issues,
            video_map_issues,
            category_issues,
            mandatory_gaps or {},
            video_held or [],
            video,
        ),
        # Off for the complete report: the notes are about one run, and next to an
        # inventory of every live product they read as a claim about all of them.
        *([] if live else _observations_lines(observations or [])),
        *(
            _matrix_lines(
                matrix.products,
                matrix.gdsn_map,
                matrix.gdsn_extras,
                languages,
                matrix.video_confirmed,
                products,
            )
            if matrix is not None
            else []
        ),
        *video_lines(
            video,
            video_map_issues,
            client_id=client_id,
            languages=languages,
            also_held=frozenset(mandatory_gaps or {}),
        ),
        *_blocking_lines(mandatory_gaps or {}, products, matrix, languages),
        *_review_lines(inferences, generated_count, products, client_id, languages),
        *_source_lines(inconsistent, wrong_lang, thin, products, languages),
        *_translated_lines(translated, products, languages),
        *_category_lines(category_issues),
    ]
    return "\n".join(lines)
