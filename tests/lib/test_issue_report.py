"""The client's short issue report: failed products, issues by category, and why (2026-10-09)."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from lib import issue_report_files, ticked_snapshot
from lib.eligibility import NO_IMAGE, NO_TARGET, Eligibility, LinkIssue
from lib.issue_report import (
    LINK,
    MISSING_DATA,
    MISSING_VIDEO,
    NO_CONTRACT,
    NO_IMAGE_CATEGORY,
    NO_NAME,
    NO_VIDEO,
    NOT_IN_EXPORT,
    NOT_RUN,
    RUN_ERROR,
    TWO_VIDEOS,
    Issue,
    IssueReport,
    run_issues,
    selection_issues,
)
from lib.scope_report import HELD, IN_SCOPE, NOT_SELECTED, ScopeRow, UnitResult

A, B, C, D, E = (f"0298765300000{n}" for n in range(1, 6))


def _report(products: list[tuple[str, str]], issues: list[Issue]) -> IssueReport:
    return IssueReport(
        "Product issues - Acme", "Selection", tuple(g for g, _ in products), tuple(issues)
    )


def test_the_selection_report_says_what_the_data_screen_says() -> None:
    products = [(A, "Vergiet"), (B, "Deurmat"), (C, "Zaklamp"), (D, "Schuurspons"), (E, "Kam")]
    verdict = Eligibility(
        not_eligible={
            B: "missing data: dim_height (attr 3498), dim_width (attr 3520); two videos in nl",
            C: NO_IMAGE,
        },
        missing_video={D: "no confirmed video in fr"},
    )

    issues = selection_issues(products, exported={B, C, D, E}, verdict=verdict)

    assert [(i.gtin, i.category, i.reason) for i in issues] == [
        (A, NOT_IN_EXPORT, "the GS1 export has no row for this barcode"),
        (B, MISSING_DATA, "dim_height (attr 3498), dim_width (attr 3520)"),
        (C, NO_IMAGE_CATEGORY, NO_IMAGE),
        (B, TWO_VIDEOS, "two videos in nl"),
        (D, MISSING_VIDEO, "no confirmed video in fr"),
    ]
    report = _report(products, issues)
    assert report.failed == [(A, "Vergiet"), (B, "Deurmat"), (C, "Zaklamp")]
    assert report.with_warnings == 1, "a missing video is an issue, not a failure"
    assert [category for category, _ in report.by_category()] == [
        NOT_IN_EXPORT,
        MISSING_DATA,
        NO_IMAGE_CATEGORY,
        TWO_VIDEOS,
        MISSING_VIDEO,
    ]
    assert [i.category for i in report.reasons_of(B)] == [MISSING_DATA, TWO_VIDEOS]


def test_a_links_batch_reports_its_links_only() -> None:
    verdict = Eligibility(
        bad_link={
            A: LinkIssue("https://acme.test/st", "the page does not exist (404)"),
            B: LinkIssue(None, NO_TARGET),
        }
    )

    issues = selection_issues([(A, "x"), (B, "y"), (C, "z")], {A, B, C}, verdict)

    assert [(i.gtin, i.category, i.reason) for i in issues] == [
        (A, LINK, "the page does not exist (404) (https://acme.test/st)"),
        (B, LINK, NO_TARGET),
    ]


def test_a_video_hold_with_nothing_but_the_video_is_no_confirmed_video() -> None:
    issues = selection_issues(
        [(A, "x")], {A}, Eligibility(not_eligible={A: "no confirmed video in nl, fr"})
    )

    assert [(i.category, i.reason) for i in issues] == [(NO_VIDEO, "no confirmed video in nl, fr")]


def _scope(gtin: str, in_scope: str, **units: UnitResult) -> ScopeRow:
    return ScopeRow(cells=[gtin], gtin=gtin, in_scope=in_scope, units=units)


def test_the_run_report_covers_only_what_the_run_was_given() -> None:
    ok = UnitResult("ok", "https://acme.test/p/", "")
    rows = [
        _scope(A, IN_SCOPE, nl=ok, fr=ok),
        _scope(
            B,
            IN_SCOPE,
            nl=UnitResult("error", "", "PUT /links target URL for language nl does not serve: x"),
            fr=UnitResult(HELD, "", "missing_product_name: missing product_name.fr"),
        ),
        _scope(C, IN_SCOPE, nl=UnitResult("not run", "", ""), fr=ok),
        _scope(D, NOT_SELECTED, nl=UnitResult("not run", "", "")),
        _scope(A, IN_SCOPE, nl=ok, fr=ok),  # the same barcode on a second row is one product
    ]

    selected, issues = run_issues(
        rows, {A: "Vergiet", B: "Deurmat"}, writes_pages=True, without_video={A: "no video in fr"}
    )

    assert selected == [A, B, C], "an unticked row is not this run's"

    assert [(i.gtin, i.category, i.reason) for i in issues] == [
        (B, NO_NAME, "fr: missing product_name.fr"),
        (B, RUN_ERROR, "nl: the page did not load, so no GS1 link was written"),
        (C, NOT_RUN, "nl: the run stopped before this product, or it was not confirmed"),
        (A, MISSING_VIDEO, "no video in fr"),
    ]


def test_a_links_run_never_reports_a_missing_video() -> None:
    ok = UnitResult("ok", "", "")
    _, issues = run_issues(
        [_scope(A, IN_SCOPE, nl=ok)], {}, writes_pages=False, without_video={A: "no video in nl"}
    )

    assert issues == []


def test_the_pdf_and_the_workbook_carry_the_same_three_parts(tmp_path: Path) -> None:
    products = [(A, "Vergiet — groot"), (B, "Brosse à vaisselle")]
    issues = selection_issues(
        products, {B}, Eligibility(missing_video={B: "no confirmed video in fr"})
    )
    report = _report(products, issues)

    pdf, xlsx = issue_report_files.write(report, tmp_path, stem="issues")

    assert pdf.read_bytes().startswith(b"%PDF"), "a dash in a product name must not break it"
    book = openpyxl.load_workbook(xlsx)
    assert book.sheetnames == ["Total", "By category", "Failed products"]
    total = [row[0] for row in book["Total"].iter_rows(min_row=2, values_only=True)]
    assert "1 of 2 product(s) failed." in total
    failed = list(book["Failed products"].iter_rows(min_row=2, values_only=True))
    assert failed == [
        (A, "Vergiet — groot", "Not in the GS1 export: the GS1 export has no row for this barcode")
    ]
    by_category = list(book["By category"].iter_rows(min_row=2, values_only=True))
    assert [(row[0], row[1]) for row in by_category] == [
        ("Not in the GS1 export", "yes"),
        ("Live without a video", "no"),
    ]


def test_a_hold_in_every_language_is_said_once() -> None:
    held = UnitResult(HELD, "", "missing_mandatory_field: missing mandatory source data: image")
    _, issues = run_issues([_scope(A, IN_SCOPE, nl=held, fr=held)], {}, writes_pages=True)

    assert [(i.category, i.reason) for i in issues] == [(MISSING_DATA, "image")]


def test_a_dry_run_reports_the_videos_a_real_run_would_go_live_without() -> None:
    """Operator, 2026-10-09: a dry run wrote no page, so it is told what the real run would do."""
    rows = [_scope(A, IN_SCOPE, nl=UnitResult("dry-run", "", ""))]

    report = issue_report_files.for_run(
        "t",
        "20261009T000000Z",
        rows,
        [],
        [],
        mode="pages",
        dry_run=True,
        default_language="nl",
        expected_without_video={A: "no confirmed video in fr"},
    )

    assert [(i.category, i.reason) for i in report.issues] == [
        (MISSING_VIDEO, "no confirmed video in fr")
    ]
    assert report.failed == [], "still not a failure"


def test_the_screen_shows_the_same_three_parts_as_the_file() -> None:
    products = [(A, "Vergiet | groot"), (B, "Deurmat")]
    issues = selection_issues(products, {B}, Eligibility())

    text = issue_report_files.to_markdown(_report(products, issues))

    assert "#### 1. Total" in text and "1 of 2 product(s) failed." in text
    assert "#### 2. Issues by category" in text and "#### 3. Failed products" in text
    assert "Vergiet \\| groot" in text, "a pipe in a name must not split the table"
    issues = selection_issues(
        [(B, "x")], {B}, Eligibility(not_eligible={B: "missing data: dim_height, dim_width"})
    )
    text = issue_report_files.to_markdown(_report([(B, "x")], issues))
    assert "dim\\_height, dim\\_width" in text, "an underscore is not italics"


def test_a_product_held_in_every_language_is_not_also_live_without_a_video() -> None:
    held = UnitResult(HELD, "", "missing_mandatory_field: missing mandatory source data: image")
    _, issues = run_issues(
        [_scope(A, IN_SCOPE, nl=held, fr=held)],
        {},
        writes_pages=True,
        without_video={A: "no confirmed video in fr"},
    )

    assert [i.category for i in issues] == [MISSING_DATA]


def test_nothing_ticked_says_so() -> None:
    assert issue_report_files.summary_lines(_report([], [])) == ["No products selected."]


def test_a_snapshot_is_read_back_only_for_the_selection_it_was_saved_with(tmp_path: Path) -> None:
    selection = tmp_path / "selections.xlsx"
    selection.write_bytes(b"the saved batch")
    issues = selection_issues([(A, "Vergiet")], set(), Eligibility())

    ticked_snapshot.write(selection, [(A, "Vergiet"), (B, "Deurmat")], issues)
    back = ticked_snapshot.read(ticked_snapshot.path_for(selection), selection)

    assert back is not None
    assert back.products == ((A, "Vergiet"), (B, "Deurmat"))
    assert back.issues == tuple(issues)
    selection.write_bytes(b"saved again, by hand")
    assert ticked_snapshot.read(ticked_snapshot.path_for(selection), selection) is None


def test_the_run_report_covers_every_ticked_product_as_the_screen_did() -> None:
    """Operator, 2026-10-09: the report after a run is the Data screen's — the ticked products."""
    left_out = selection_issues([(C, "Zaklamp")], set(), Eligibility())
    ran = [
        _scope(A, IN_SCOPE, nl=UnitResult("ok", "", "")),
        _scope(B, IN_SCOPE, nl=UnitResult("error", "", "boom")),
    ]
    report = issue_report_files.for_run(
        "t",
        "20261009T000000Z",
        ran,
        [],
        [],
        mode="pages",
        dry_run=False,
        default_language="nl",
        ticked=ticked_snapshot.Ticked(((A, "a"), (C, "Zaklamp"), (B, "b")), tuple(left_out)),
    )

    assert report.selected == (A, C, B)
    assert [gtin for gtin, _ in report.failed] == [C, B]
    assert [(i.gtin, i.category) for i in report.issues] == [(C, NOT_IN_EXPORT), (B, RUN_ERROR)]
    assert "2 product(s) in this run, 1 more ticked that could not run." in report.subtitle


def test_products_left_out_of_this_run_are_not_reported_as_failed() -> None:
    """Operator, 2026-10-10: a re-try of 6 of 31 reported the other 25 — live — as failed."""
    refused = UnitResult("error", "", "GS1 21011 No valid contract found.")
    rows = [
        _scope(A, IN_SCOPE, nl=refused, fr=refused),
        _scope(B, IN_SCOPE, nl=UnitResult("not run", "", ""), fr=UnitResult("not run", "", "")),
        _scope(C, IN_SCOPE, nl=UnitResult("not run", "", ""), fr=UnitResult("not run", "", "")),
    ]

    report = issue_report_files.for_run(
        "t",
        "20261010T000000Z",
        rows,
        [],
        [],
        mode="links",
        dry_run=False,
        default_language="nl",
        ticked=ticked_snapshot.Ticked(((A, "a"), (B, "b"), (C, "c")), ()),
        confirmed={A, C},
    )

    assert report.selected == (A, C), "B was not confirmed for this run, so it is not in it"
    assert [(gtin, i.category) for gtin, _ in report.failed for i in report.reasons_of(gtin)] == [
        (A, NO_CONTRACT),
        (C, NOT_RUN),
    ], "C was confirmed and never reached — that one is a failure"
    assert "1 other(s) in the batch were not part of this run" in report.subtitle
