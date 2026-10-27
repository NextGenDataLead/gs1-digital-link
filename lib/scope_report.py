"""The per-run result sheet: the operator's own scope list, with what the run did to each row.

The operator hands over a list of barcodes and gets back a run log keyed by ``(gtin, language)``.
Those are different documents in different vocabularies, and joining them by hand is the job this
exists to remove. Here the sheet **is** their list — their columns, their order, their words — with
what happened appended to the right of it, so the file that comes back is recognisable to the
person who sent it.

**One row per SKU, not per unit.** Going per unit would duplicate seven columns of theirs across
76 rows and stop the file being theirs. The per-unit record is not lost: it is the ``units`` sheet
beside this one, straight from :class:`~lib.records.RunOutcome`, which is where "nl published, fr
failed" lives losslessly — a thing that has actually happened here and that no single column can
say.

**Two status columns, not one.** ``in_scope`` is the *decision* — was this row in the batch at all
— and ``status_{lang}`` is *what the run did*. That is the same lesson this project learned from
the status columns that used to live in the control file: one cell answering both "should this
run?" and "what happened?" is a cell whose meaning depends on when you read it, and whose next
re-run has undefined semantics. See :mod:`lib.process_list` for the full account.

**A hold is not a failure.** A unit the plan held — no confirmed video, no generated copy, a blank
mandatory field — reads ``held``, never ``error``. :func:`lib.preflight.check_video_coverage` puts
it best: calling a handled condition a failure is how a report earns the right to be ignored.

Statuses are :attr:`lib.records.RunOutcome.status` **verbatim** — ``ok``, ``error``, ``dry-run`` —
rather than a prettier vocabulary of this module's own. A second set of words for the same fact is
how the shell's Runs screen and this sheet would come to disagree about one run. Only the two
values the run log cannot supply are added: ``held`` and ``not run``. English throughout, matching
the log and ``plan.json``; the ``legend`` sheet is where a plain sentence per value goes, and it
can be written in any language without changing what the data means.

Pure and deterministic — no filesystem, no config, no clock. ``scripts/report_scope_result.py``
does the I/O.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from lib.media_video import canon_gtin
from lib.process_list import ProcessListSheet
from lib.records import RunOutcome, SkippedUnit

#: The row was in the batch: on the control file and carried by the export.
IN_SCOPE: Final = "yes"
#: On the uploaded list, taken off the control file before the run. The operator's own decision.
NOT_SELECTED: Final = "not selected"
#: On the uploaded list, and the Data screen held it — missing data, two videos, no image. It had
#: no tick box there, so it is the data's doing and not the operator's; the reason is in ``detail``.
NOT_ELIGIBLE: Final = "not eligible"
#: On the list, and the export has no row for it. Nothing else in the tool reports this:
#: it produces no error, no plan row and no count, only a total one smaller than expected.
NOT_IN_EXPORT: Final = "not in export"

#: The plan dropped this unit for a handled reason — see :class:`lib.records.SkipReason`. The
#: reason itself goes in ``detail_{lang}``; this is never ``error``.
HELD: Final = "held"
#: In scope, and the run log says nothing about it. Never blank: a blank cell reads as "fine".
NOT_RUN: Final = "not run"

#: Worst-first. ``result`` is the worst of a SKU's languages so one column is filterable, and
#: ``not run`` outranks ``held`` because a hold is explained and a silence is not.
_SEVERITY: Final = {"error": 0, NOT_RUN: 1, HELD: 2, "dry-run": 3, "ok": 4}

#: What each value means, in one sentence, so the file needs no covering email.
LEGEND: Final[tuple[tuple[str, str, str], ...]] = (
    ("in_scope", IN_SCOPE, "This row was in the batch: on the scope list and in the GS1 export."),
    (
        "in_scope",
        NOT_SELECTED,
        "You uploaded this row but took it off the list before the run, so the run never "
        "considered it. Nothing was published and nothing failed.",
    ),
    (
        "in_scope",
        NOT_ELIGIBLE,
        "The export carries this product, but it could not be chosen: a mandatory value is blank "
        "in MyGS1, the client confirmed two videos for one language, or there is no product "
        "image. The detail column says which. Fix that and it can be chosen next time.",
    ),
    (
        "in_scope",
        NOT_IN_EXPORT,
        "This barcode is on the list and the GS1 Data Source export has no row for it. Either "
        "the product is missing from the export — fix it in MyGS1 and export again — or the "
        "barcode is wrong. This is not a failure of the run; there was nothing to publish.",
    ),
    ("status", "ok", "Published. The page URL is in the page column beside this one."),
    (
        "status",
        "error",
        "The run tried and failed. The reason is in the detail column; the same text is in the "
        "run log.",
    ),
    (
        "status",
        "dry-run",
        "A rehearsal. The run worked out what it would do and wrote nothing — no page, no GS1 "
        "record. Nothing here is live.",
    ),
    (
        "status",
        HELD,
        "Deliberately not published, for the reason in the detail column: usually no confirmed "
        "video, no generated text, or a mandatory field left blank in MyGS1. Fix the reason and "
        "the next run picks it up. This is not an error.",
    ),
    (
        "status",
        NOT_RUN,
        "In scope, and this run says nothing about it — it stopped before reaching this row, or "
        "the row was not in the confirmed plan. Worth asking about.",
    ),
)


@dataclass(frozen=True)
class UnitResult:
    """What happened to one ``(gtin, language)``: the status, where it landed, and why not."""

    status: str
    page: str
    detail: str


@dataclass(frozen=True)
class ScopeRow:
    """One row of the operator's list, with the run's answer appended.

    ``cells`` is their row verbatim — the report does not re-order, re-format or drop a column of
    a document it did not write.
    """

    cells: list[str]
    gtin: str | None
    in_scope: str
    units: dict[str, UnitResult]

    @property
    def result(self) -> str:
        """The worst of this SKU's languages, so one column can be filtered on."""
        return min(
            (unit.status for unit in self.units.values()),
            key=lambda status: _SEVERITY.get(status, 0),
            default=NOT_RUN,
        )


def build_rows(  # noqa: PLR0913 — five named inputs read better than a context object
    sheet: ProcessListSheet,
    *,
    selected: Collection[str],
    exported: Collection[str],
    outcomes: Sequence[RunOutcome],
    skipped: Sequence[SkippedUnit],
    languages: Sequence[str],
    not_eligible: Mapping[str, str] | None = None,
) -> list[ScopeRow]:
    """Join the uploaded list against what the run did, one row per SKU.

    Args:
        sheet: The **uploaded** list — ``runs/{stamp}/selection-uploaded.xlsx`` — not the
            ticked one. It is
            the superset, so a row the operator deselected still appears, named as deselected
            rather than silently absent.
        selected: GTIN-14s in the control file the run actually read. A row of ``sheet`` not in
            here was deselected.
        exported: GTIN-14s the parsed export carries.
        outcomes: The run log, as read.
        skipped: ``Plan.skipped`` — units the plan dropped before classification.
        languages: The client's configured languages, in order.
        not_eligible: ``{gtin14: why}`` from :func:`lib.eligibility.eligibility` — the rows the Data
            screen offered no tick box for. Never in the batch file, so the plan never held them;
            this is where their reason comes from.

    Returns:
        One :class:`ScopeRow` per data row of ``sheet``, in the sheet's own order.
    """
    by_unit = {(canon_gtin(o.gtin), o.language): o for o in outcomes}
    held = {(canon_gtin(s.gtin), s.language): s for s in skipped}
    why_not = not_eligible or {}

    rows = []
    for index in range(len(sheet.rows)):
        gtin = sheet.gtin14_at(index)
        rows.append(
            ScopeRow(
                cells=list(sheet.rows[index]),
                gtin=gtin,
                in_scope=_in_scope(gtin, selected, exported, why_not),
                # A row with a blank barcode matches no unit, which is right: it is a row of
                # the operator's file that names no product.
                units={
                    language: (
                        UnitResult(status=HELD, page="", detail=why_not[gtin])
                        if gtin and gtin not in selected and gtin in why_not
                        else _unit(
                            by_unit.get((gtin, language)) if gtin else None,
                            held.get((gtin, language)) if gtin else None,
                        )
                    )
                    for language in languages
                },
            )
        )
    return rows


def _in_scope(
    gtin: str | None,
    selected: Collection[str],
    exported: Collection[str],
    not_eligible: Mapping[str, str],
) -> str:
    """Why a row is or is not in the batch — the data fact where there was no choice to make.

    The Data screen offers a tick box only on an eligible row, so a row the export lacks or the
    plan would hold was never the operator's to choose, and calling it ``not selected`` would send
    them looking for a decision they did not make. ``not selected`` is left for the rows they
    unticked.
    """
    if gtin is None:
        return NOT_SELECTED
    if gtin not in exported:
        return NOT_IN_EXPORT
    if gtin in selected:
        return IN_SCOPE
    return NOT_ELIGIBLE if gtin in not_eligible else NOT_SELECTED


def _unit(outcome: RunOutcome | None, skip: SkippedUnit | None) -> UnitResult:
    """One language's answer. The run log wins, then the plan's holds, then silence."""
    if outcome is not None:
        return UnitResult(
            # Verbatim. A run log that starts carrying a status this module has never heard of
            # should show it, not fall back on a guess that reads like a fact.
            status=outcome.status,
            page=outcome.wp_url or "",
            detail=_failure(outcome),
        )
    if skip is not None:
        return UnitResult(status=HELD, page="", detail=f"{skip.reason.value}: {skip.detail}")
    return UnitResult(status=NOT_RUN, page="", detail="")


def _failure(outcome: RunOutcome) -> str:
    """The error with the call that produced it, which is the difference between two re-runs.

    A live ``403`` reported as "failed: 403" took a re-run with the output captured before anyone
    knew it was a video upload rather than the page — see :class:`lib.records.RunOutcome`.
    """
    if not outcome.error:
        return ""
    return f"{outcome.failed_call} {outcome.error}" if outcome.failed_call else outcome.error


def scope_grid(
    sheet: ProcessListSheet, rows: Sequence[ScopeRow], languages: Sequence[str]
) -> tuple[list[str], list[list[str]]]:
    """The ``scope`` sheet: their columns verbatim, then ours."""
    columns = [
        # A blank header cell keeps its column — it is still theirs — and is labelled by position
        # so the sheet has no two columns with the same empty name.
        name or f"column {index + 1}"
        for index, name in enumerate(sheet.header)
    ]
    columns += ["in_scope", "result"]
    for language in languages:
        columns += [f"status_{language}", f"page_{language}", f"detail_{language}"]

    width = len(sheet.header)
    grid = []
    for row in rows:
        cells = [*row.cells[:width], *[""] * (width - len(row.cells))]
        cells += [row.in_scope, row.result]
        for language in languages:
            unit = row.units.get(language, UnitResult(NOT_RUN, "", ""))
            cells += [unit.status, unit.page, unit.detail]
        grid.append(cells)
    return columns, grid


def units_grid(
    outcomes: Sequence[RunOutcome], skipped: Sequence[SkippedUnit]
) -> tuple[list[str], list[list[str]]]:
    """The ``units`` sheet: one row per ``(gtin, language)``, with no interpretation at all.

    This is where "nl published, fr failed" survives. The ``scope`` sheet reduces a SKU's
    languages to one ``result``, which is what makes it filterable and also what makes it lossy;
    everything the reduction dropped is here, in the log's own words.
    """
    columns = [
        "gtin",
        "language",
        "source",
        "status",
        "ts",
        "wp_page_id",
        "wp_url",
        "gs1_set",
        "detail",
    ]
    grid = [
        [
            outcome.gtin,
            outcome.language,
            "run log",
            outcome.status,
            outcome.ts.isoformat(),
            str(outcome.wp_page_id or ""),
            outcome.wp_url or "",
            "yes" if outcome.gs1_set else "",
            _failure(outcome),
        ]
        for outcome in outcomes
    ]
    grid += [
        [
            skip.gtin,
            skip.language,
            "plan",
            HELD,
            "",
            "",
            "",
            "",
            f"{skip.reason.value}: {skip.detail}",
        ]
        for skip in skipped
    ]
    return columns, grid


def legend_grid() -> tuple[list[str], list[list[str]]]:
    """The ``legend`` sheet: every value that appears, in a sentence.

    So the workbook can be forwarded on its own. A status column whose values need explaining in
    the covering email is a status column that arrives without one.
    """
    return ["column", "value", "what it means"], [list(entry) for entry in LEGEND]
