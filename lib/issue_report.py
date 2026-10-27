"""The short issue report for the client: which products of a batch failed, and why.

Operator, 2026-10-09: the data-quality report "has gotten a bit out of hand" — the client needs a
one-to-two pager, about **the selected products only**, saying

1. how many products failed;
2. the issues by category, each with its products and the reason;
3. the failed products, each with its reasons — the same facts as (2), the other way round.

There are two moments it is asked for, and this module builds both:

* **before a run** — :func:`selection_issues`, from the Data screen's verdict over the *ticked*
  rows, so the report says exactly what the screen says (``lib.eligibility`` is the one producer);
* **after a run** — :func:`run_issues`, from the per-run result rows (``lib.scope_report``): what
  the plan held, what failed while publishing, and what went live without a video. Only the
  products the run was given; the rows the operator left unticked are not this report's business.

A **missing video** is listed as an issue but is not a failure: the product runs, and goes live
without the video (operator, 2026-10-09). Everything else here stops the product, or part of it.

Pure: no filesystem, no clock, no config. :mod:`lib.issue_report_files` writes the PDF and Excel.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from lib.eligibility import NO_IMAGE, Eligibility
from lib.records import SkipReason
from lib.scope_report import HELD, IN_SCOPE, ScopeRow
from lib.scope_report import NOT_RUN as UNIT_NOT_RUN


@dataclass(frozen=True)
class Category:
    """One kind of issue: its title, whether it stops the product, and who does what about it."""

    key: str
    title: str
    failed: bool
    action: str


NOT_IN_EXPORT: Final = Category(
    "not_in_export",
    "Not in the GS1 export",
    True,
    "Add the product in MyGS1 and export again, or correct the barcode on the product list.",
)
MISSING_DATA: Final = Category(
    "missing_data",
    "Missing mandatory data",
    True,
    "Fill in the values named in MyGS1 and export again.",
)
NO_NAME: Final = Category(
    "no_name",
    "No product name in a language",
    True,
    "Fill in the product name for that language in MyGS1 and export again.",
)
NO_IMAGE_CATEGORY: Final = Category(
    "no_image", "No product image", True, "Add a product image in MyGS1 and export again."
)
TWO_VIDEOS: Final = Category(
    "two_videos",
    "Two videos for one language",
    True,
    "Choose which of the two videos to keep.",
)
NO_VIDEO: Final = Category(
    "no_video",
    "No confirmed video",
    True,
    "Confirm a video for the language named.",
)
NO_CONTRACT: Final = Category(
    "no_contract",
    "Refused by GS1: no valid contract",
    True,
    "GS1 refused the link with 'No valid contract found'. Check with GS1 Nederland that the "
    "Digital Link contract on the account covers these barcodes, then run them again.",
)
LINK: Final = Category(
    "link",
    "Page link does not work",
    True,
    "Correct the page address on the product list, or put the page back online.",
)
NO_TEXT: Final = Category(
    "no_text",
    "No product text yet",
    True,
    "Nothing to do for you: we write the text and run the product again.",
)
RUN_ERROR: Final = Category(
    "run_error",
    "Failed while publishing",
    True,
    "Nothing to do for you unless we ask: we look into it and run the product again.",
)
NOT_RUN: Final = Category(
    "not_run",
    "Not reached by the run",
    True,
    "Nothing to do for you: we run the product again.",
)
CHECK_FAILED: Final = Category(
    "check_failed",
    "Could not be checked",
    True,
    "Nothing to do for you: we fix the check and look again.",
)
MISSING_VIDEO: Final = Category(
    "missing_video",
    "Live without a video",
    False,
    "Send or confirm a video for the language named; a later run adds it to the page.",
)

#: The order the report lists them in: the client's own fixes first, ours after, the one that does
#: not fail a product last.
CATEGORIES: Final = (
    NOT_IN_EXPORT,
    MISSING_DATA,
    NO_NAME,
    NO_IMAGE_CATEGORY,
    TWO_VIDEOS,
    NO_VIDEO,
    NO_CONTRACT,
    LINK,
    NO_TEXT,
    RUN_ERROR,
    NOT_RUN,
    CHECK_FAILED,
    MISSING_VIDEO,
)

_MISSING_DATA_PREFIX: Final = "missing data: "
_TWO_VIDEOS_PREFIX: Final = "two videos in "
#: How the plan words a missing-data hold; the category already says it.
_HELD_DATA_PREFIX: Final = "missing mandatory source data: "

#: A plan's hold, by :class:`lib.records.SkipReason`. The video hold is split on its detail.
_BY_SKIP: Final = {
    SkipReason.MISSING_PRODUCT_NAME.value: NO_NAME,
    SkipReason.NO_GENERATED_COPY.value: NO_TEXT,
    SkipReason.BLANK_HERO_IMAGE.value: NO_IMAGE_CATEGORY,
    SkipReason.MISSING_MANDATORY_FIELD.value: MISSING_DATA,
    SkipReason.NO_CONFIRMED_VIDEO.value: NO_VIDEO,
}

#: GS1's refusal when the account's Digital Link contract does not cover a barcode (21011). Tried
#: and reported, never predicted: a barcode can be on the client's GS1 account and still get it.
_NO_CONTRACT: Final = "No valid contract found"

#: Long technical errors are cut here; the full text is in the run log.
_MAX_ERROR: Final = 120


@dataclass(frozen=True)
class Issue:
    """One product's issue of one kind. Several languages of the same kind are one issue."""

    gtin: str
    name: str
    category: Category
    reason: str


@dataclass(frozen=True)
class IssueReport:
    """Everything the report says, in the order it says it.

    Attributes:
        title: The heading, naming the client.
        subtitle: What batch this is about — the selection, or which run — and when.
        selected: The products the report is about, by barcode.
        issues: Every issue, in category order then the batch's order.
    """

    title: str
    subtitle: str
    selected: tuple[str, ...]
    issues: tuple[Issue, ...]

    @property
    def failed(self) -> list[tuple[str, str]]:
        """``(barcode, name)`` of every failed product, in the batch's order."""
        bad = {issue.gtin: issue.name for issue in self.issues if issue.category.failed}
        return [(gtin, bad[gtin]) for gtin in self.selected if gtin in bad]

    @property
    def with_warnings(self) -> int:
        """Products that run, but with an issue that does not fail them (a missing video)."""
        failed = {gtin for gtin, _ in self.failed}
        return len(
            {
                issue.gtin
                for issue in self.issues
                if not issue.category.failed and issue.gtin not in failed
            }
        )

    def by_category(self) -> list[tuple[Category, list[Issue]]]:
        """Part 2: each category that has issues, with them."""
        return [
            (category, found)
            for category in CATEGORIES
            if (found := [issue for issue in self.issues if issue.category == category])
        ]

    def reasons_of(self, gtin: str) -> list[Issue]:
        """Part 3: one failed product's issues, failing ones first."""
        mine = [issue for issue in self.issues if issue.gtin == gtin]
        return sorted(mine, key=lambda issue: not issue.category.failed)


def selection_issues(
    products: Sequence[tuple[str, str]],
    exported: Collection[str],
    verdict: Eligibility,
) -> list[Issue]:
    """The ticked products' issues, as the Data screen judges them.

    Args:
        products: ``(gtin14, name)`` of each ticked product, once each, in list order.
        exported: GTIN-14s the parsed export carries.
        verdict: :func:`lib.eligibility.eligibility_for` — the screen's own verdict, link checks
            included for a links batch.
    """
    issues: list[Issue] = []
    for gtin, name in products:
        if gtin not in exported:
            reason = "the GS1 export has no row for this barcode"
            issues.append(Issue(gtin, name, NOT_IN_EXPORT, reason))
        elif verdict.problem:
            issues.append(Issue(gtin, name, CHECK_FAILED, verdict.problem))
        elif gtin in verdict.bad_link:
            link = verdict.bad_link[gtin]
            reason = f"{link.problem} ({link.url})" if link.url else link.problem
            issues.append(Issue(gtin, name, LINK, reason))
        elif gtin in verdict.not_eligible:
            issues.extend(
                Issue(gtin, name, *_categorise(part))
                for part in verdict.not_eligible[gtin].split("; ")
            )
        elif gtin in verdict.missing_video:
            issues.append(Issue(gtin, name, MISSING_VIDEO, verdict.missing_video[gtin]))
    return _ordered(_merged(issues))


def run_issues(
    rows: Sequence[ScopeRow],
    names: Mapping[str, str],
    *,
    writes_pages: bool,
    without_video: Mapping[str, str] | None = None,
) -> tuple[list[str], list[Issue]]:
    """The products a run was given, and their issues.

    Args:
        rows: :func:`lib.scope_report.build_rows` for the run — the uploaded list with each unit's
            outcome. Only the rows the run was given (``in_scope == yes``) count.
        names: ``{gtin14: product name}``.
        writes_pages: Whether the run wrote pages — only then can a page lack a video.
        without_video: ``{gtin14: what is missing}`` for pages that go live with no video — from
            the run log after a real run, from the video verdict after a dry run.

    Returns:
        The run's products by barcode, in list order, and their issues.
    """
    selected: list[str] = []
    issues: list[Issue] = []
    for row in rows:
        if row.in_scope != IN_SCOPE or row.gtin is None or row.gtin in selected:
            continue
        gtin = row.gtin
        selected.append(gtin)
        name = names.get(gtin, "")
        per_language: dict[Category, dict[str, str]] = {}
        for language, unit in row.units.items():
            category, reason = _unit_issue(unit.status, unit.detail)
            if category is not None:
                per_language.setdefault(category, {})[language] = reason
        issues.extend(
            Issue(gtin, name, category, _languages(found, len(row.units)))
            for category, found in per_language.items()
        )
        bare = (without_video or {}).get(gtin) if writes_pages else None
        # A product that fails in every language goes live nowhere, so it lacks no video on a page.
        live_somewhere = any(
            _unit_issue(unit.status, unit.detail)[0] is None for unit in row.units.values()
        )
        if bare and live_somewhere:
            issues.append(Issue(gtin, name, MISSING_VIDEO, bare))
    return selected, _ordered(_merged(issues))


def with_ticked(
    ticked_products: Sequence[tuple[str, str]],
    ticked_issues: Sequence[Issue],
    ran: Sequence[str],
    issues: Sequence[Issue],
    not_in_run: Collection[str] = (),
) -> tuple[list[str], list[Issue]]:
    """A run's report widened to every product the operator **ticked** — the Data screen's report.

    The ticked products the run never received (Next saves only those that can run) keep the reason
    the screen gave them; the ones it ran are reported on what the run did. Returns the products by
    barcode — ticked order, then any the run had that were not ticked — and their issues.

    ``not_in_run`` are ticked products the operator left out of *this* run when confirming the plan
    — say the 25 already live when only 6 were re-tried. They are not this run's, and not failures.
    """
    ran_set = set(ran)
    skip = set(not_in_run)
    order = [gtin for gtin, _ in ticked_products if gtin not in skip]
    order += [gtin for gtin in ran if gtin not in set(order)]
    left_out = [i for i in ticked_issues if i.gtin not in ran_set and i.gtin not in skip]
    return order, _ordered([*left_out, *issues])


def _languages(found: Mapping[str, str], languages: int) -> str:
    """One reason when every language has the same one — a product-wide hold says it once."""
    reasons = set(found.values())
    if len(found) == languages and len(reasons) == 1:
        return reasons.pop()
    return "; ".join(f"{language}: {reason}" for language, reason in found.items())


def _categorise(part: str) -> tuple[Category, str]:
    """One clause of the screen's *not eligible* reason, as ``(category, reason)``."""
    if part.startswith(_MISSING_DATA_PREFIX):
        return MISSING_DATA, part[len(_MISSING_DATA_PREFIX) :]
    if part == NO_IMAGE:
        return NO_IMAGE_CATEGORY, part
    if part.startswith(_TWO_VIDEOS_PREFIX):
        return TWO_VIDEOS, part
    return NO_VIDEO, part


def _unit_issue(status: str, detail: str) -> tuple[Category | None, str]:
    """What one language of a run's product says about it, or ``(None, "")`` when it is fine."""
    if status == "error" and _NO_CONTRACT in detail:
        return NO_CONTRACT, "GS1 refused the link: 'No valid contract found' (code 21011)"
    if status == "error":
        return RUN_ERROR, _plain_error(detail)
    if status == UNIT_NOT_RUN:
        return NOT_RUN, "the run stopped before this product, or it was not confirmed"
    if status == HELD:
        reason, _, said = detail.partition(": ")
        category = _BY_SKIP.get(reason, CHECK_FAILED)
        if category is NO_VIDEO and "two confirmed videos" in said:
            category = TWO_VIDEOS
        return category, said.removeprefix(_HELD_DATA_PREFIX) or reason
    return None, ""


def _plain_error(detail: str) -> str:
    """A publishing error in words the client can read; the full text stays in the run log."""
    if "does not serve" in detail:
        return "the page did not load, so no GS1 link was written"
    if "already published its own" in detail:
        return "the product has two pages; one must be chosen on the product list"
    short = detail if len(detail) <= _MAX_ERROR else detail[: _MAX_ERROR - 1] + "…"
    return f"publishing failed ({short})"


def _merged(issues: list[Issue]) -> list[Issue]:
    """One issue per product and category, its reasons joined — "nl: …; fr: …"."""
    reasons: dict[tuple[str, str], list[str]] = {}
    first: dict[tuple[str, str], Issue] = {}
    for issue in issues:
        key = (issue.gtin, issue.category.key)
        first.setdefault(key, issue)
        if issue.reason not in reasons.setdefault(key, []):
            reasons[key].append(issue.reason)
    return [
        Issue(issue.gtin, issue.name, issue.category, "; ".join(reasons[key]))
        for key, issue in first.items()
    ]


def _ordered(issues: list[Issue]) -> list[Issue]:
    """Category order, and the batch's order within a category (``sorted`` is stable)."""
    rank = {category.key: n for n, category in enumerate(CATEGORIES)}
    return sorted(issues, key=lambda issue: rank[issue.category.key])
