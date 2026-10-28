"""Read the client's filled-in video sign-off sheet into a plan, without touching the mapping.

The video mapping decides whether a product may publish at all, and filling it in is the
client's job, not the tool's. ``scripts/report_video_candidates`` writes them a grid to work
in — one row per (language, file), with ranked candidates — and what comes back is that sheet
with its ``gtin`` column filled in, or a three-column list somebody typed from scratch. This
module turns either into a decision per row. It writes nothing: :mod:`ui.video_map_edit`
applies the edits, and only the ones an operator confirmed.

**The sheet leads, per barcode** (operator decision, 2026-10-24 — it replaces "a confirmed row is
never overwritten"). Within one language, every barcode the sheet names gets exactly the videos the
sheet gives it; a barcode the sheet does not name keeps what it had. So:

* a row with barcodes gives that video those barcodes, *plus* any it had that the sheet names
  nowhere (:func:`_resolve`);
* a row reading ``skip`` takes the video off every product;
* a video whose barcodes the sheet hands elsewhere loses them, and becomes ``skip`` when none are
  left — a decision, not a gap. A blank cell alone changes nothing.

One video may carry **several barcodes** — one cell, separated by commas, or the same file on more
than one row — and the mapping stores it as one row per barcode, which is what every consumer that
decides publishing already reads per barcode. What the sheet may never do is give one barcode two
videos in one language: :meth:`VideoMap.resolve` would attach neither. Such a sheet is
:data:`AMBIGUOUS` and refused **whole**, because its rows move barcodes between videos and half of
a move is a different mapping from either end of it.

**What the sheet says about a barcode is not what the mapping means by one.** Excel hands back
``2987653008486`` as a number for a 13-digit EAN, ``…486.0`` as a float, ``02987653008486`` as
text when a leading zero survived, and ``298-7653008486`` when somebody formatted it. There are
already two normalisations in this codebase and they disagree:
:func:`lib.process_list._coerce_gtin` zero-pads without stripping punctuation, and
:func:`lib.media_video.canon_gtin` strips non-digits and then pads. What is written here is read
back by :func:`lib.media_video.fully_mapped_gtins`, so this normalises on **the mapping's own
rule** — ``canon_gtin`` — and refuses what that rule would silently mangle rather than passing
it through to be compared against products forever.

The validation that actually catches mistakes is **"is this GTIN in the export?"**. A transposed
digit usually produces a barcode no product has, and that is reportable; a check digit catches
the same cases and stays silent on the one that matters — a typo that happens to be another real
product. So the export is the authority, and a GTIN absent from it is rejected with the cell
quoted back.
"""

from __future__ import annotations

import io
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Final

from lib.media_video import (
    CONFIRMED,
    SKIP,
    UNSET,
    Assignment,
    VideoMap,
    assignments,
    canon_gtin,
    state_of,
)
from lib.xlsx import Grid, read_grid

#: Header spellings recognised for each column, normalised by :func:`_norm`. These are a **pre-fill,
#: not a requirement**: the screen always shows which column it read for what and lets the operator
#: change it. This list is a guess about somebody else's spreadsheet and it has already been wrong
#: once — the real sign-off sheet calls the barcode ``current_gtin``, so the import refused the one
#: file it exists for. A list like this cannot be completed by thinking harder about it.
LANGUAGE_COLUMNS: Final = ("language", "lang", "languagecode", "taal", "langue")
FILE_COLUMNS: Final = ("file", "filename", "video", "videofile", "bestand", "fichier")
GTIN_COLUMNS: Final = (
    "gtin",
    "gtin13",
    "gtin14",
    "ean",
    "ean13",
    "barcode",
    "barcodenumber",
    # ``current_gtin`` is what an earlier revision of ``report_video_candidates`` called it, and the
    # operator still has sheets in that shape. In both schemas the one barcode column is *both* the
    # mapping's current value and where the client writes their answer — they edit it in place — so
    # an untouched row decides UNCHANGED and an edited one CHANGE.
    "currentgtin",
    "currentean",
)

#: What was decided about one row. The sheet gives this video barcodes other than it has now.
CHANGE: Final = "change"
#: The mapping already says exactly this. Not an error and not work — the ordinary case for a sheet
#: that is a superset of what has already been signed off, which every later round of one is.
UNCHANGED: Final = "unchanged"
#: No barcode in the cell. Changes nothing by itself — but the video can still lose a barcode
#: another row of the sheet claims, which :attr:`SignoffPlan.changes` lists.
BLANK: Final = "blank"
#: The row cannot be applied: an unknown language or file, or a barcode that is not one.
REJECTED: Final = "rejected"
#: Applying the sheet would leave one GTIN on two videos in one language. The whole sheet is then
#: refused — see :func:`_resolve`.
AMBIGUOUS: Final = "ambiguous"
#: Before :func:`_resolve` has seen the whole sheet. Never in a returned plan.
_PENDING: Final = "pending"

_SKIPPED: Final[Assignment] = (SKIP,)

#: What separates barcodes in one cell. A space is not one: people write a barcode with spaces in.
_SEPARATORS: Final = re.compile(r"[,;\n]")

#: Canonical GTIN width, as :func:`lib.media_video.canon_gtin` pads to.
_GTIN_WIDTH: Final = 14
#: The shortest real GTIN, GTIN-8. A shorter piece of a multi-barcode cell is not one.
_SHORTEST_GTIN: Final = 8

#: Formatting people put inside a barcode, and the only characters stripped before validating.
_FORMATTING: Final = (" ", ".", "-", " ", "–")


def _norm(text: str) -> str:
    """A header cell reduced for matching: lower-cased, letters and digits only.

    So ``"Language Code"``, ``"language_code"`` and ``"LANGUAGE-CODE"`` are one column, while
    ``"gtin"`` stays distinct from ``"candidate_1_gtin"`` — which matters, because our own report
    carries both and a loose match would read the first *suggestion* as the client's answer.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class SignoffRow:
    """One row of the client's sheet, and what it means for the mapping.

    Attributes:
        line: The row's 1-based position in the spreadsheet, header included, so a rejection can be
            pointed at — "row 24" is the only address the person fixing it has.
        language: The language cell, verbatim.
        file: The filename cell, verbatim.
        given: The barcode cell exactly as the sheet carried it, kept for every message. A
            rejection that does not quote what it read is unfixable.
        gtin: What the cell was read as, for display: the canonical GTIN-14s joined by ``", "``,
            :data:`SKIP`, or ``""`` when the row yields neither.
        outcome: One of :data:`CHANGE`, :data:`UNCHANGED`, :data:`BLANK`, :data:`REJECTED`,
            :data:`AMBIGUOUS`.
        detail: Why, in one line, for the outcomes that need a reason.
        assigned: ``gtin`` as data — what this row asks the video to carry.
    """

    line: int
    language: str
    file: str
    given: str
    gtin: str
    outcome: str
    detail: str = ""
    assigned: Assignment = ()


@dataclass(frozen=True)
class FileChange:
    """One video whose barcodes the sheet would change.

    Attributes:
        language: The language block.
        file: The video.
        before: What the mapping says now.
        after: What it would say.
        lines: The sheet rows that name this video — empty when it changes only because another row
            took its barcode.
    """

    language: str
    file: str
    before: Assignment
    after: Assignment
    lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class SignoffPlan:
    """Every row of the sheet, decided, and what the mapping would become. Nothing is written.

    Attributes:
        rows: One per data row of the sheet, in sheet order.
        changes: Every video whose barcodes would change, in mapping order — including videos the
            sheet does not name, which lose a barcode another row claims.
        losing: ``(language, gtin)`` pairs that would be left with no video at all. A page already
            live keeps the video it has: the write path can set a video, never take one off.
        clashing: GTINs this sheet would put on two videos in one language — why it is refused.
        resolved: GTINs on two videos in the mapping now that the sheet would leave on one.
    """

    rows: tuple[SignoffRow, ...]
    changes: tuple[FileChange, ...] = ()
    losing: tuple[tuple[str, str], ...] = ()
    clashing: frozenset[str] = frozenset()
    resolved: frozenset[str] = frozenset()

    def of(self, outcome: str) -> tuple[SignoffRow, ...]:
        """The rows with this outcome, in sheet order."""
        return tuple(row for row in self.rows if row.outcome == outcome)

    @property
    def refused(self) -> bool:
        """Whether the sheet may not be applied at all — it would give a product two videos."""
        return bool(self.clashing)

    @property
    def edits(self) -> dict[tuple[str, str], Assignment]:
        """The changes, in the shape :func:`ui.video_map_edit.apply_edits` takes — none if refused.

        Keyed ``(language, file)``, the mapping's own coordinates. The value is the video's whole
        new assignment, not a delta, so applying it twice is applying it once.
        """
        if self.refused:
            return {}
        return {(change.language, change.file): change.after for change in self.changes}


def columns(grid: Grid) -> dict[str, int] | None:
    """Locate the three columns the sheet must have, or ``None`` when one is missing.

    Returns ``{"language": i, "file": j, "gtin": k}``. Extra columns are ignored, which is what
    lets the candidates report come back unmodified apart from the column the client filled in.
    """
    found: dict[str, int] = {}
    for name, accepted in (
        ("language", LANGUAGE_COLUMNS),
        ("file", FILE_COLUMNS),
        ("gtin", GTIN_COLUMNS),
    ):
        index = next((n for n, cell in enumerate(grid.header) if _norm(cell) in accepted), None)
        if index is None:
            return None
        found[name] = index
    return found


#: The fewest filled cells a row needs before it could be a header at all. Two, because the sheet
#: has at least a filename and a barcode in it, and a title row above the table is one cell.
_HEADER_CELLS: Final = 2


def read_sheet(source: Path | bytes) -> Grid | None:
    """The sign-off sheet, by the names we know or failing that by shape. ``None`` if neither works.

    Two passes, and the second is why the operator can fix a sheet this module does not recognise.
    The first looks for a header carrying names from :data:`GTIN_COLUMNS` and :data:`FILE_COLUMNS` —
    the ordinary case, and it needs no decisions. The second accepts the first row with
    :data:`_HEADER_CELLS` filled cells, so a sheet whose columns are called something nobody
    anticipated still arrives on screen with its headers listed, where they can be assigned.

    Without that second pass the column picker could not exist: a sheet we cannot find a header in
    is a sheet we cannot show columns for, and the operator would be told to go and rename things in
    Excel to match a list the tool never shows them.

    Taking ``bytes`` as well as a path is not incidental either: each pass needs its own stream,
    and an upload is bytes that were deliberately never written to disk.
    """
    for recognises in (is_header, _could_be_a_header):
        grid = read_grid(_stream(source), header_row=recognises)
        if grid is not None:
            return grid
    return None


#: Two headings every GS1 Data Source export opens with, and no sign-off sheet carries together.
_EXPORT_HEADINGS: Final = frozenset({"gtin", "targetmarketcountrycode"})


def looks_like_an_export(grid: Grid) -> bool:
    """Whether this "sheet" is really a GS1 Data Source export dropped in the wrong place.

    Worth its own refusal because :func:`read_sheet`'s second pass accepts any row with two filled
    cells as a header: an export *does* produce a grid, and the plan then rejects every row of it,
    loudly, about barcodes and filenames — which is the wrong thing to be told. Three identical
    ``.xlsx`` pickers on one screen make this the likeliest mix-up there is.
    """
    return {_norm(cell) for cell in grid.header} >= _EXPORT_HEADINGS


def _stream(source: Path | bytes) -> Path | IO[bytes]:
    """A fresh readable for one pass. A path reopens; bytes need wrapping each time."""
    return io.BytesIO(source) if isinstance(source, bytes) else source


def _could_be_a_header(texts: Sequence[str]) -> bool:
    """Whether a row could be a header at all, knowing nothing about what its columns are called.

    Deliberately crude. It can mistake the first data row for the header, and that is survivable
    precisely because of what happens next: the operator is shown the column names it found, and
    ``fr`` / ``Airfryer Basket_FR.mpg`` listed as column *names* is unmistakable.
    """
    return sum(1 for text in texts if text.strip()) >= _HEADER_CELLS


def column_options(grid: Grid) -> dict[int, str]:
    """Every column of the sheet, labelled for a picker: ``{position: name}``.

    A blank header cell is still a column — the sheet may have one, and it may even be the one
    wanted — so it is offered by position rather than dropped.
    """
    return {
        index: name.strip() or f"(column {index + 1}, no heading)"
        for index, name in enumerate(grid.header)
    }


def is_header(texts: Sequence[str]) -> bool:
    """Whether a row of cell texts is the sheet's header — it names a barcode **and** a file.

    Both, not either: a data row whose filename happens to read ``file`` is one cell away from
    being mistaken for the header, and the title row above a real table often carries one word.
    """
    normalised = {_norm(text) for text in texts}
    return bool(normalised & set(GTIN_COLUMNS)) and bool(normalised & set(FILE_COLUMNS))


def plan(
    grid: Grid,
    vmap: VideoMap,
    *,
    exported: Collection[str],
    languages: Collection[str],
    where: Mapping[str, int] | None = None,
) -> SignoffPlan:
    """Decide every row of the sheet against the mapping and the export.

    Args:
        grid: The sheet, as :func:`read_sheet` returned it.
        vmap: The mapping as it stands. Never modified.
        exported: GTIN-14s the export carries — ``{p.gtin14 for p in products}``. Empty skips that
            check, deliberately: an import run before ``parse_export`` should report "not in the
            export" for nothing rather than for every row.
        languages: The configured languages, so a sheet naming one the client invented is caught
            rather than silently filling a block no run reads.
        where: Which column is which, as ``{"language": i, "file": j, "gtin": k}``. The operator's
            answer when they have given one; :func:`columns` guesses when they have not. Explicit
            rather than always inferred because the guess is about a file this project does not
            control, and it has already been wrong on the only real sheet there is.

    Returns:
        The plan — empty when no columns were given and none could be recognised.
    """
    where = where if where is not None else columns(grid)
    if where is None:
        return SignoffPlan(())
    known = {
        language: {entry.file for entry in entries}
        for language, entries in vmap.by_language.items()
    }
    decided = [
        _decide(row, line, where, known, exported=exported, languages=languages)
        # Line 1 is the header to the person reading the sheet, so the first data row is line 2.
        # Off by one here and every rejection points at the row above the problem.
        for line, row in enumerate(grid.rows, start=2)
    ]
    return _resolve(_reject_contradictions(decided), vmap)


def _reject_contradictions(decided: Sequence[SignoffRow]) -> list[SignoffRow]:
    """Reject every row of a video the sheet both skips and gives a barcode — it says two things."""
    by_file: dict[tuple[str, str], set[bool]] = {}
    for row in decided:
        if row.outcome == _PENDING:
            by_file.setdefault((row.language, row.file), set()).add(row.assigned == _SKIPPED)
    torn = {key for key, kinds in by_file.items() if len(kinds) > 1}
    return [
        replace(
            row,
            outcome=REJECTED,
            detail="the sheet says both skip and a barcode for this video — keep one",
        )
        if row.outcome == _PENDING and (row.language, row.file) in torn
        else row
        for row in decided
    ]


def _resolve(decided: Sequence[SignoffRow], vmap: VideoMap) -> SignoffPlan:
    """Apply the sheet-leads rule (module docstring) per language, and decide every pending row.

    The rule, per language, with ``named`` every barcode the sheet gives some video:

    * a video the sheet reads ``skip`` for → ``skip``;
    * a video the sheet gives barcodes → those, plus what it had that ``named`` does not hold;
    * any other video → what it had, minus ``named``; ``skip`` if that empties it.

    Then a barcode in ``named`` that would sit on two videos refuses the sheet — those products are
    :data:`AMBIGUOUS` — **unless that is exactly the clash the mapping already has**. A returned
    sheet repeats every barcode it was sent with, so an existing clash comes back named on both
    videos; refusing it would refuse every round-trip of a mapping with one clash in it, fills and
    all. Left exactly as it was, it stays held and reported, as a clash on a barcode the sheet does
    not name always is. What the sheet may not do is *make* or *change* one.
    """
    sheet: dict[tuple[str, str], set[str]] = {}
    lines: dict[tuple[str, str], list[int]] = {}
    for row in decided:
        if row.outcome == _PENDING:
            sheet.setdefault((row.language, row.file), set()).update(row.assigned)
            lines.setdefault((row.language, row.file), []).append(row.line)

    changes: list[FileChange] = []
    losing: list[tuple[str, str]] = []
    clashing: dict[tuple[str, str], list[str]] = {}
    resolved: set[str] = set()
    after_by_language: dict[str, dict[str, Assignment]] = {}
    for language in sorted({language for language, _ in sheet}):
        current = assignments(vmap, language)
        asked = {file: gtins for (lang, file), gtins in sheet.items() if lang == language}
        named = {gtin for gtins in asked.values() for gtin in gtins if gtin != SKIP}
        after = {file: _after(current[file], asked.get(file), named) for file in current}
        after_by_language[language] = after

        holders, before = _holders(after), _holders(current)
        clashing.update(
            ((language, gtin), files)
            for gtin, files in holders.items()
            if gtin in named and len(files) > 1 and sorted(files) != sorted(before.get(gtin, []))
        )
        resolved.update(
            gtin
            for gtin, files in before.items()
            if len(files) > 1 and len(holders.get(gtin, [])) == 1
        )
        # Only a barcode that had *a* video can lose it: one on two videos had none that showed.
        losing.extend(
            (language, gtin)
            for gtin in sorted(before)
            if gtin not in holders and len(before[gtin]) == 1
        )
        changes.extend(
            FileChange(
                language, file, current[file], after[file], tuple(lines.get((language, file), ()))
            )
            for file in current
            if after[file] != current[file]
        )

    rows = tuple(
        _final(row, after_by_language, vmap, clashing) if row.outcome == _PENDING else row
        for row in decided
    )
    clash_gtins = frozenset(gtin for _, gtin in clashing)
    # A refused sheet solves nothing: none of it is applied.
    return SignoffPlan(
        rows,
        tuple(changes),
        tuple(losing),
        clash_gtins,
        frozenset() if clash_gtins else frozenset(resolved),
    )


def _after(before: Assignment, asked: set[str] | None, named: set[str]) -> Assignment:
    """One video's assignment once the sheet is applied — the three cases in :func:`_resolve`."""
    kept = {gtin for gtin in before if gtin != SKIP}
    if asked is not None and SKIP in asked:
        return _SKIPPED
    if asked is not None:
        return tuple(sorted(asked | (kept - named)))
    rest = kept - named
    if rest:
        return tuple(sorted(rest))
    return _SKIPPED if kept else before


def _holders(assignments: Mapping[str, Assignment]) -> dict[str, list[str]]:
    """``{gtin: [video, …]}`` over one language's assignments."""
    holders: dict[str, list[str]] = {}
    for file, gtins in assignments.items():
        for gtin in gtins:
            if gtin != SKIP:
                holders.setdefault(gtin, []).append(file)
    return holders


def _final(
    row: SignoffRow,
    after_by_language: Mapping[str, Mapping[str, Assignment]],
    vmap: VideoMap,
    clashing: Mapping[tuple[str, str], list[str]],
) -> SignoffRow:
    """A pending row's outcome, now that the whole sheet has been resolved."""
    rivals = sorted(
        (gtin, [name for name in files if name != row.file])
        for (language, gtin), files in clashing.items()
        if language == row.language and gtin in row.assigned
    )
    if rivals:
        detail = "; ".join(
            f"{gtin} would also be on {', '.join(others)} in {row.language}"
            for gtin, others in rivals
        )
        return replace(
            row,
            outcome=AMBIGUOUS,
            detail=f"{detail} — a product can carry one video per language, so neither would show",
        )
    before = assignments(vmap, row.language).get(row.file, ())
    after = after_by_language[row.language][row.file]
    if after == before:
        return replace(row, outcome=UNCHANGED)
    return replace(row, outcome=CHANGE, detail=f"{describe(before)} → {describe(after)}")


def describe(assignment: Assignment) -> str:
    """An assignment in words, for a table cell: ``unset``, ``skip`` or the barcodes."""
    if not assignment:
        return "unset"
    return ", ".join(assignment)


@dataclass(frozen=True)
class Clash:
    """One product confirmed to two or more files in one language.

    Attributes:
        gtin: Canonical GTIN-14.
        language: Where the files compete.
        files: The competing filenames, sorted — the two to choose between.
    """

    gtin: str
    language: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class MappingContext:
    """What the mapping already says, for the figures beside a sheet's plan.

    The plan counts **rows of the sheet**. Two of the things it is read beside are counted elsewhere
    in other units, and the operator compares them: report §1b and step 4's *Not eligible* count
    *products with two videos in one language*, and the doctor counts *mapping rows with no GTIN*.
    So the panel shows those numbers too, split into what the mapping already has and what this
    sheet would add — the same figure as everywhere else, with the sheet's part named.

    Attributes:
        clashes: Two-video clashes already in the mapping, before this sheet.
        new_clashes: GTINs this sheet would put on a second file in a language (its AMBIGUOUS rows),
            that are not clashing there already. Any of them refuses the sheet.
        unset: Mapping rows with no GTIN yet — the doctor's figure.
        resolved: Clashing products the sheet would leave on one video — it names their barcode on
            one video, and the sheet leads.
    """

    clashes: tuple[Clash, ...]
    new_clashes: frozenset[str]
    unset: int
    resolved: frozenset[str] = frozenset()

    @property
    def existing_products(self) -> frozenset[str]:
        """Distinct products already clashing in the mapping — report §1b's count."""
        return frozenset(clash.gtin for clash in self.clashes)

    @property
    def clashing_products(self) -> int:
        """Distinct products with a two-video clash once this sheet were applied."""
        return len((self.existing_products - self.resolved) | self.new_clashes)


def mapping_context(
    decided: SignoffPlan, vmap: VideoMap, languages: Collection[str]
) -> MappingContext:
    """The mapping's own clash and unset counts, beside the plan of a sheet against it. Pure."""
    clashes: list[Clash] = []
    for language in languages:
        files: dict[str, set[str]] = {}
        for entry in vmap.by_language.get(language, []):
            if state_of(entry.gtin) == CONFIRMED:
                files.setdefault(canon_gtin(entry.gtin), set()).add(entry.file)
        clashes.extend(
            Clash(gtin, language, tuple(sorted(names)))
            for gtin, names in sorted(files.items())
            if len(names) > 1
        )
    existing = {clash.gtin for clash in clashes}
    new = frozenset(decided.clashing - existing)
    unset = sum(
        state_of(entry.gtin) == UNSET for entries in vmap.by_language.values() for entry in entries
    )
    return MappingContext(tuple(clashes), new, unset, decided.resolved & existing)


def _decide(  # noqa: PLR0913 — one collaborator per question this row has to answer
    cells: Sequence[str],
    line: int,
    where: Mapping[str, int],
    known: Mapping[str, set[str]],
    *,
    exported: Collection[str],
    languages: Collection[str],
) -> SignoffRow:
    """One row on its own: rejected, blank, or pending :func:`_resolve` with what it asks for."""
    language = _cell(cells, where["language"])
    file = _cell(cells, where["file"])
    given = _cell(cells, where["gtin"])

    def row(outcome: str, assigned: Assignment = (), detail: str = "") -> SignoffRow:
        return SignoffRow(
            line,
            language,
            file,
            given,
            describe(assigned) if assigned else "",
            outcome,
            detail,
            assigned,
        )

    unaddressable = _unaddressable(language, file, known, languages)
    if unaddressable:
        return row(REJECTED, detail=unaddressable)
    if not given:
        return row(BLANK)

    assigned, problem = _coerce_cell(given)
    if problem:
        return row(REJECTED, detail=problem)
    absent = [gtin for gtin in assigned if gtin != SKIP and exported and gtin not in exported]
    if absent:
        return row(
            REJECTED,
            assigned,
            detail=f"{', '.join(absent)} not in the export — check it, or export it from MyGS1",
        )
    return row(_PENDING, assigned)


def addable(decided: SignoffPlan, missing: Mapping[str, Collection[str]]) -> dict[str, list[str]]:
    """The sheet's rejected videos that a new mapping row would make importable, per language.

    ``missing`` is what is in each language's video folder but not in the mapping
    (:func:`ui.video_map_edit.files_missing_from_map`). A rejected row qualifies only when its
    file is one of those: a filename the folder does not have is a typo, or a video not copied in
    yet, and a row for it would be a row for nothing. Sheet order, each file once.
    """
    found: dict[str, list[str]] = {}
    for row in decided.of(REJECTED):
        names = found.setdefault(row.language, [])
        if row.file in missing.get(row.language, ()) and row.file not in names:
            names.append(row.file)
    return {language: names for language, names in found.items() if names}


def _unaddressable(
    language: str, file: str, known: Mapping[str, set[str]], languages: Collection[str]
) -> str:
    """Why this row names no mapping entry, or ``""`` when it names one.

    Checked before the barcode, because a perfectly good GTIN against a filename the mapping has
    never heard of is still nothing this import can do — and "unknown file" is the message that
    gets it fixed, where "not in the export" would send somebody to MyGS1 for a typo in a filename.
    """
    if not file:
        return "no filename in this row"
    if language not in languages:
        return f"{language!r} is not a configured language ({', '.join(sorted(languages))})"
    if file not in known.get(language, set()):
        return f"the mapping has no {language} row for this file — add one to videos/mapping.yml"
    return ""


def _coerce_cell(given: str) -> tuple[Assignment, str]:
    """A barcode cell — one barcode, several separated by commas, or ``skip`` — as an assignment.

    Every barcode in the cell must be one; a cell with one bad barcode among good ones is refused
    whole, because applying the good ones would quietly drop the product the bad one was meant to
    name. ``skip`` stands alone: "no product, and also this one" is not an answer.
    """
    parts = [part.strip() for part in _SEPARATORS.split(given) if part.strip()]
    if len(parts) > 1 and any(len(re.sub(r"\D", "", part)) < _SHORTEST_GTIN for part in parts):
        # ``8,713,195,008,509`` is one barcode a spreadsheet formatted with thousands separators,
        # not four. Read as four, every piece pads to a valid-looking GTIN-14.
        return (), (
            f"{given!r} has a part too short to be a barcode — if it is one number with thousands "
            "separators, format that column as text and re-enter it; several barcodes are "
            "separated by commas, each one whole"
        )
    found: set[str] = set()
    for part in parts:
        gtin, problem = _coerce(part)
        if problem:
            return (), problem
        found.add(gtin)
    if SKIP in found and len(found) > 1:
        return (), f"{given!r} gives both {SKIP!r} and a barcode — keep one"
    return tuple(sorted(found)), ""


def _coerce(given: str) -> tuple[str, str]:
    """A barcode cell as ``(canonical GTIN or ``skip``, problem)``. One of the two is always empty.

    Refuses rather than normalises in the two cases where normalising would be a guess:

    * **Letters or other junk in the cell.** ``canon_gtin`` strips every non-digit, so ``"EAN 871"``
      and ``"see tab 2"`` both become plausible-looking GTIN-14s. Stripping *formatting* is fair —
      spaces, dots and hyphens are how people write barcodes down — so those pass and nothing else
      does.
    * **More than 14 digits.** ``zfill`` does not truncate, so such a value would simply never match
      a product, and the row would report as "not in the export" when the real fault is a
      concatenated cell.

    Scientific notation gets a message of its own because it is the commonest way a barcode arrives
    broken and the least obvious to the person who sent it: Excel renders a 13-digit number as
    ``8.7132E+12`` in a narrow column, and a CSV round trip bakes that in. Told only "expected
    digits", somebody re-types the same cell and sends the same file back.
    """
    value = given.strip()
    if value.lower() == SKIP:
        return SKIP, ""
    if re.fullmatch(r"\d(?:[.,]\d+)?[eE][+-]?\d+", value):
        return "", (
            f"{given!r} is scientific notation, not a barcode — Excel does this to a long number. "
            "Format that column as text, re-enter the barcode, and save again."
        )
    # Excel hands a 13-digit barcode back as a number, and a whole-number float is that same
    # barcode. Only a trailing ``.0`` — anything else that looks decimal is not a GTIN.
    if re.fullmatch(r"\d+\.0+", value):
        value = value.split(".", 1)[0]
    cleaned = value
    for char in _FORMATTING:
        cleaned = cleaned.replace(char, "")
    if not cleaned.isdigit():
        return "", f"{given!r} is not a barcode — expected digits, or the word {SKIP!r}"
    if len(cleaned) > _GTIN_WIDTH:
        return "", (
            f"{given!r} has {len(cleaned)} digits — a GTIN has at most {_GTIN_WIDTH}. A cell "
            "formatted as a number can come back like this; set the column to text and re-save."
        )
    return canon_gtin(cleaned), ""


def _cell(cells: Sequence[str], index: int) -> str:
    """One cell, stripped, or ``""`` when the row is shorter than the header."""
    return cells[index].strip() if 0 <= index < len(cells) else ""
