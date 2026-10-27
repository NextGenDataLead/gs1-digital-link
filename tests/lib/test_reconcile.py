"""Does the ledger say what the site says?

Four ways the answer is no, and the one that matters most is not an outside edit — **the tool
causes it**. A run that publishes one language and fails the next records the row as an error
and writes nothing to state, leaving a live, correct, publicly reachable page that the ledger
has never heard of. That happened on the first real publish through the shell.

The comparison is pure, so every awkward case is testable without a site.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lib.reconcile import Divergence, LivePage, reconcile
from lib.records import State, StateEntry

GTIN_A = "02987653000001"
GTIN_B = "02987653000002"


def _entry(page_id: int, *, status: str = "publish") -> StateEntry:
    return StateEntry(
        wp_page_id=page_id,
        wp_url=f"https://example.test/p-{page_id}/",
        wp_featured_media_id=None,
        content_hash="c" * 64,
        gs1_link_set_hash="g" * 64,
        last_run=datetime.now(UTC),
        wp_status=status,
    )


def _state(**entries: dict[str, StateEntry]) -> State:
    return State(client_id="acme", entries=dict(entries))


def _page(gtin: str, language: str, page_id: int, *, status: str = "publish") -> LivePage:
    return LivePage(
        gtin=gtin,
        language=language,
        page_id=page_id,
        slug=f"p-{gtin}",
        status=status,
        url=f"https://example.test/p-{gtin}/",
    )


# --- Agreement ----------------------------------------------------------------


def test_a_site_that_matches_the_ledger_reports_nothing() -> None:
    report = reconcile(
        [_page(GTIN_A, "nl", 11), _page(GTIN_A, "fr", 12)],
        _state(**{GTIN_A: {"nl": _entry(11), "fr": _entry(12)}}),
        ["nl", "fr"],
    )

    assert report.agrees
    assert report.findings == []
    assert "they agree" in report.summary


def test_the_summary_says_what_was_compared_even_when_nothing_diverged() -> None:
    """Zero findings over zero pages is what a wrong post type looks like. Show the denominator."""
    report = reconcile([], _state(), ["nl"])

    assert report.agrees
    assert "0 live page(s)" in report.summary
    assert "0 state entr" in report.summary


# --- The divergence the tool makes itself --------------------------------------


def test_a_page_the_ledger_never_heard_of_is_reported() -> None:
    """The partial-failure case: nl published, fr failed, the row logged as an error.

    Nothing was written to state, so the live Dutch page is invisible to every later run — which
    will classify the product NEW, and only the client's slug lookup prevents a duplicate.
    """
    report = reconcile([_page(GTIN_A, "nl", 11)], _state(), ["nl"])

    assert [f.kind for f in report.findings] == [Divergence.LIVE_NOT_RECORDED]
    assert report.findings[0].gtin == GTIN_A
    assert "11" in report.findings[0].detail
    assert "classify this product as NEW" in report.findings[0].explanation


def test_an_entry_with_no_page_is_reported() -> None:
    report = reconcile([], _state(**{GTIN_A: {"nl": _entry(11)}}), ["nl"])

    assert [f.kind for f in report.findings] == [Divergence.RECORDED_NOT_LIVE]
    assert "11" in report.findings[0].detail


def test_a_page_id_that_does_not_match_is_reported() -> None:
    report = reconcile([_page(GTIN_A, "nl", 99)], _state(**{GTIN_A: {"nl": _entry(11)}}), ["nl"])

    assert [f.kind for f in report.findings] == [Divergence.DIFFERENT_PAGE]
    assert "11" in report.findings[0].detail
    assert "99" in report.findings[0].detail


def test_a_drafted_page_is_reported_even_though_both_sides_know_it() -> None:
    """The ids agree, so a naive diff sees nothing — and the page is not reachable."""
    report = reconcile(
        [_page(GTIN_A, "nl", 11, status="draft")],
        _state(**{GTIN_A: {"nl": _entry(11)}}),
        ["nl"],
    )

    assert [f.kind for f in report.findings] == [Divergence.NOT_PUBLISHED]
    assert "draft" in report.findings[0].detail


# --- Not confusing "not checked" with "not there" ------------------------------


def test_a_language_that_was_not_checked_is_not_reported_as_missing() -> None:
    """Reporting an unchecked language as a gap would make the whole report noise."""
    report = reconcile(
        [_page(GTIN_A, "nl", 11)],
        _state(**{GTIN_A: {"nl": _entry(11), "fr": _entry(12)}}),
        ["nl"],
    )

    assert report.agrees
    assert report.state_entries == 1


def test_gtins_compare_across_digit_forms() -> None:
    """The mapping and the feed disagree about leading zeros; the site carries whatever it was
    given. Comparing raw strings would report every product twice."""
    report = reconcile(
        [_page("2987653000001", "nl", 11)],
        _state(**{"02987653000001": {"nl": _entry(11)}}),
        ["nl"],
    )

    assert report.agrees


# --- Shape of the report -------------------------------------------------------


def test_findings_are_ordered_so_two_reports_can_be_diffed() -> None:
    report = reconcile(
        [_page(GTIN_B, "nl", 21), _page(GTIN_A, "fr", 12)],
        _state(),
        ["nl", "fr"],
    )

    assert [(f.gtin, f.language) for f in report.findings] == [
        (GTIN_A, "fr"),
        (GTIN_B, "nl"),
    ]


def test_the_summary_counts_each_kind() -> None:
    report = reconcile(
        [_page(GTIN_A, "nl", 11)],
        _state(**{GTIN_B: {"nl": _entry(21)}}),
        ["nl"],
    )

    assert "live_not_recorded" in report.summary
    assert "recorded_not_live" in report.summary
    assert len(report.of_kind(Divergence.LIVE_NOT_RECORDED)) == 1
    assert len(report.of_kind(Divergence.RECORDED_NOT_LIVE)) == 1
