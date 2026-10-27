"""Tests for ui/context.py — reading the doctor's answer rather than re-deriving it.

Gate 0 used to lead with the length of ``products.json`` under the label "products in the
catalogue". Honest, and the wrong number: it read **127** on a run scoped to one product, at the
gate where the operator forms their picture of what they are about to do.

The fix is not to compute scope in the shell. ``lib.preflight.in_scope`` already composes the two
gates that decide it, and a second implementation of "what will this run touch" is the same class
of mistake as a second implementation of the operator gates. So the shell reads the doctor's
``scope`` check, and these tests cover the reading — including every way it can be unreadable,
because the one thing that must never happen is a catalogue total shown under a scope label.

No NiceGUI: ``ui.context`` imports only ``lib`` and ``ui.REPO_ROOT``, so this runs in the required
CI job rather than the optional one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ui.context import (
    Scope,
    copy_summary,
    doctor_check,
    group_results,
    live_checked_at,
    live_counts,
    live_gtins,
    scope_from,
    split_results,
    text_written_for,
)


def _payload(**overrides: Any) -> list[dict[str, Any]]:
    """A doctor payload shaped exactly like the real one, scope check included."""
    scope: dict[str, Any] = {
        "name": "scope",
        "title": "What a run would touch",
        "status": "ok",
        "detail": "15 of 127 product(s) in the export are in scope, after process list (x.xlsx).",
        "remedy": "",
        "data": {"in_scope": 15, "total": 127, "in_scope_gtins": ["02987653000001"]},
    }
    scope.update(overrides)
    return [{"name": "config", "status": "ok", "data": {}}, scope]


# --- doctor_check --------------------------------------------------------------


def test_finds_a_check_by_name() -> None:
    assert doctor_check(_payload(), "scope") is not None
    assert doctor_check(_payload(), "config") is not None


def test_a_check_that_did_not_run_is_none_rather_than_an_error() -> None:
    assert doctor_check(_payload(), "generation_results") is None


def test_a_payload_that_is_not_a_list_is_none() -> None:
    """A crashed command still printed something, and a caller would rather show that than raise."""
    for payload in (None, "", "Traceback (most recent call last):", {"error": "boom"}, 3):
        assert doctor_check(payload, "scope") is None


# --- scope_from ----------------------------------------------------------------


def test_reads_the_two_numbers_and_the_sentence() -> None:
    scope = scope_from(_payload())
    assert scope == Scope(
        in_scope=15,
        total=127,
        detail="15 of 127 product(s) in the export are in scope, after process list (x.xlsx).",
        empty=False,
        gtins=frozenset({"02987653000001"}),
    )


def test_an_empty_scope_is_flagged_so_gate_zero_can_say_so() -> None:
    """The doctor FAILs this check when nothing is in scope, and that is the loudest case.

    A run against an empty scope writes nothing and reports success — the one outcome
    indistinguishable from working, and the failure this project keeps designing against.
    """
    scope = scope_from(
        _payload(status="fail", data={"in_scope": 0, "total": 127, "in_scope_gtins": []})
    )
    assert scope is not None
    assert scope.empty
    assert scope.in_scope == 0


def test_no_scope_check_reads_as_unknown_not_as_zero() -> None:
    """``None`` and ``Scope(in_scope=0)`` mean opposite things and must not be conflated.

    Zero is "this run would touch nothing" — actionable and alarming. Absent is "the preflight did
    not say", which warrants no conclusion at all.
    """
    assert scope_from([{"name": "config", "status": "ok"}]) is None


def test_an_unreadable_payload_never_yields_a_number() -> None:
    """The one outcome worth ruling out explicitly: a figure appearing under a scope label.

    Falling back to the catalogue count here would reproduce the exact defect this replaces,
    wearing the right words — which is worse than the original, because the label would now
    vouch for it.
    """
    for payload in (None, "Traceback", {"error": "boom"}, []):
        assert scope_from(payload) is None


def test_counts_that_are_not_integers_are_refused() -> None:
    """A malformed `data` block must not become a figure on the gate that authorises the run."""
    assert scope_from(_payload(data={})) is None
    assert scope_from(_payload(data={"in_scope": 15})) is None
    assert scope_from(_payload(data={"in_scope": "15", "total": "127"})) is None


def test_a_missing_detail_sentence_costs_the_sentence_and_nothing_else() -> None:
    """The numbers are the point; the sentence explains them. Losing it must not lose them."""
    scope = scope_from(_payload(detail=None))
    assert scope is not None
    assert scope.in_scope == 15
    assert scope.detail == ""


# --- the in-scope GTIN list ----------------------------------------------------


def test_carries_the_gtins_so_a_screen_can_filter_by_them() -> None:
    """Counts let a screen *report* scope; the list lets it *filter* by it.

    The Content screen needs the second. Without it, showing this run's copy rather than every
    unit ever generated would mean re-deriving scope in the shell — a second implementation of
    the thing `lib.preflight.in_scope` exists to be the only one of.
    """
    scope = scope_from(_payload(data={"in_scope": 2, "total": 9, "in_scope_gtins": ["a", "b"]}))
    assert scope is not None
    assert scope.gtins == frozenset({"a", "b"})


def test_a_doctor_that_never_reported_gtins_yields_an_empty_set_not_a_crash() -> None:
    """Back-compat, and the caller's contract: empty means *unknown*, never *nothing in scope*.

    A screen that filtered to an empty set here would hide the whole cache and read as "there is
    no copy" — the opposite of the truth, and a worse failure than the one being fixed.
    """
    scope = scope_from(_payload(data={"in_scope": 15, "total": 127}))
    assert scope is not None
    assert scope.gtins == frozenset()
    assert scope.in_scope == 15


def test_junk_in_the_gtin_list_is_dropped_rather_than_carried() -> None:
    """It is used as a set-membership filter, so a non-string can only ever fail to match."""
    scope = scope_from(_payload(data={"in_scope": 1, "total": 1, "in_scope_gtins": ["a", 7, None]}))
    assert scope is not None
    assert scope.gtins == frozenset({"a"})


def test_a_gtin_list_that_is_not_a_list_is_treated_as_unknown() -> None:
    scope = scope_from(_payload(data={"in_scope": 1, "total": 1, "in_scope_gtins": "02987653"}))
    assert scope is not None
    assert scope.gtins == frozenset()


# --- splitting the cache into this run and everything else ---------------------


def _scope(*gtins: str) -> Scope:
    return Scope(in_scope=len(gtins), total=99, detail="", empty=False, gtins=frozenset(gtins))


_COPY: dict[str, dict[str, Any]] = {"a": {"nl": {}}, "b": {"nl": {}}, "c": {"nl": {}}}


def test_the_batch_is_separated_from_copy_written_for_another_scope() -> None:
    """The defect: the review listed every GTIN in the file, under a correctly scoped figure.

    It mattered most when the file was a cache that accumulated for the machine's lifetime. It
    still matters: a results file written against a longer process list carries GTINs this run
    will not touch, and the screen must not present them as the batch.
    """
    split = split_results(_COPY, _scope("a", "c"))

    assert set(split.in_scope) == {"a", "c"}
    assert set(split.others) == {"b"}
    assert split.scoped


def test_in_scope_gtins_with_no_copy_are_named() -> None:
    """The interesting case: it is the copy that still has to be written."""
    split = split_results(_COPY, _scope("a", "zz", "yy"))

    assert split.missing == ("yy", "zz")


def test_an_unknown_scope_shows_everything_rather_than_nothing() -> None:
    """Wrong in the safe direction, and the direction matters.

    Filtering to an empty set would hide the copy entirely and read as "there is none" — worse
    than the unscoped list being replaced, because it stops the operator looking. ``scoped`` is
    what lets the screen label it honestly instead.
    """
    for scope in (None, _scope()):
        split = split_results(_COPY, scope)
        assert split.in_scope == _COPY
        assert split.others == {}
        assert split.missing == ()
        assert not split.scoped


def test_a_batch_with_no_generated_copy_at_all_is_empty_not_unscoped() -> None:
    """Distinct from an unknown scope: here the answer is known, and the answer is none."""
    split = split_results(_COPY, _scope("zz"))

    assert split.in_scope == {}
    assert split.scoped
    assert split.missing == ("zz",)


def test_the_split_does_not_mutate_what_it_was_given() -> None:
    original = dict(_COPY)
    split_results(_COPY, _scope("a"))
    assert original == _COPY


def test_the_flat_results_list_is_grouped_by_gtin_and_language() -> None:
    """A producer writes one item at a time; a screen reads one product at a time."""
    grouped = group_results(
        [
            {"gtin": "a", "language": "nl", "usps": ["NL"]},
            {"gtin": "a", "language": "fr", "usps": ["FR"]},
            {"gtin": "b", "language": "nl", "usps": ["B"]},
        ]
    )

    assert set(grouped) == {"a", "b"}
    assert set(grouped["a"]) == {"nl", "fr"}
    assert grouped["a"]["fr"]["usps"] == ["FR"]


def test_grouping_drops_malformed_items_rather_than_raising() -> None:
    """This reads a file a human may have hand-edited, on a screen that must still render.

    A crash here takes out the copy review — the last place the text is read as text — over one
    bad line in a file the rest of which is fine.
    """
    grouped = group_results(["not an object", {"language": "nl"}, {"gtin": "a", "language": "nl"}])

    assert set(grouped) == {"a"}


# --- the coverage check, said to an operator ----------------------------------


def _coverage(**data: Any) -> dict[str, Any]:
    """A ``generation_results`` check entry carrying the figures a run produces."""
    return {"name": "generation_results", "status": "ok", "detail": "…", "data": data}


def test_a_run_with_nothing_to_write_says_so_and_says_why() -> None:
    """The case that reads as a failure and is not: 0 generated, because 0 were needed.

    In the console this run is indistinguishable from one that wrote nothing because something
    broke — ``generated 0 via API … 0/0 unit(s) to publish have copy`` — and an operator who
    cannot tell those apart either waits for a wave that already happened or re-runs it.
    """
    line = copy_summary(
        _coverage(
            total=0,
            pending=0,
            products_unchanged=8,
            products_held_video=14,
            products_held_data=9,
        )
    )

    assert line is not None
    assert line.startswith("Nothing to write")
    assert "8 product(s) in this batch are already up to date" in line
    assert "23 are blocked (14 need a confirmed video, 9 need data fixed in MyGS1)" in line


def test_pages_without_text_are_stated_as_pages_that_will_be_left_out() -> None:
    """``pending`` is the one figure that changes what the operator does next."""
    line = copy_summary(
        _coverage(
            total=10, pending=4, products_unchanged=0, products_held_video=0, products_held_data=0
        )
    )

    assert line is not None
    assert "4 of the 10 page(s)" in line
    assert "left out of the run" in line


def test_a_covered_run_reads_as_ready() -> None:
    line = copy_summary(
        _coverage(
            total=6, pending=0, products_unchanged=2, products_held_video=0, products_held_data=0
        )
    )

    assert line is not None
    assert line.startswith("Ready — all 6 page(s)")
    assert "2 product(s) in this batch are already up to date." in line
    # Nothing is blocked, so nobody is sent anywhere: a "0 blocked" clause reads as a category
    # the operator should go and look at.
    assert "blocked" not in line


def test_a_payload_without_figures_produces_no_sentence() -> None:
    """Better the check's own wording than a confident sentence built from nothing.

    ``units_needing_copy`` could not decide, or the subprocess said something unexpected — either
    way the caller falls back to ``detail``, and a fabricated "nothing to write" would be the one
    reading that stops a wave nobody meant to stop.
    """
    assert copy_summary(None) is None
    assert copy_summary({"name": "generation_results"}) is None
    assert copy_summary(_coverage(total="many", pending=0)) is None


def test_the_product_split_is_dropped_rather_than_guessed() -> None:
    """The lead clause still stands when the breakdown is absent — it is the actionable half."""
    line = copy_summary(_coverage(total=0, pending=0))

    assert line == "Nothing to write — no page in this batch needs new text."


# --- the live-site report -----------------------------------------------------


def _live(**counts: int) -> dict[str, Any]:
    return {
        "counts": {
            "in_scope": 0,
            "has_text": 0,
            "needs_text": 0,
            "no_inputs": 0,
            "held": 0,
            **counts,
        },
        "products": [
            {"gtin": "1", "name": "a", "bucket": "needs_text"},
            {"gtin": "2", "name": "b", "bucket": "has_text"},
        ],
    }


def test_a_failed_site_read_is_not_reported_as_nothing_to_do() -> None:
    """Zero products needing text and a subprocess that died look identical as numbers.

    They mean opposite things, and a green "nothing to do" built on a crashed command is the exact
    failure this project is arranged against — so the reader says it could not tell.
    """
    assert live_counts(None) is None
    assert live_counts({}) is None
    assert live_counts({"counts": {"has_text": "five"}}) is None
    assert live_counts(_live(has_text=5)) is not None


def test_gtins_come_back_only_for_the_bucket_asked_for() -> None:
    """The generate command is built from these, so a bucket leak writes for the wrong products."""
    assert live_gtins(_live(), "needs_text") == ["1"]
    assert live_gtins(_live(), "has_text") == ["2"]
    assert live_gtins(_live(), "no_inputs") == []
    assert live_gtins("not a payload", "needs_text") == []


def test_the_check_time_is_shown_as_an_age() -> None:
    """A stamp exists because the answer goes stale, and ISO-8601 cannot be subtracted at a glance.

    A check from this morning has to read differently from one taken a moment ago, or the screen
    quietly passes off a picture of a site that has since moved.
    """
    now = datetime.now(UTC)
    assert live_checked_at({"checked_at": now.isoformat()}) == "just now"
    assert live_checked_at({"checked_at": (now - timedelta(minutes=5)).isoformat()}) == (
        "5 minutes ago"
    )
    assert live_checked_at({"checked_at": (now - timedelta(hours=3)).isoformat()}) == "3 hours ago"
    # Unparseable is shown verbatim rather than as "just now": a wrong age is worse than a raw one.
    assert live_checked_at({"checked_at": "whenever"}) == "whenever"


# --- what unlocks Content's review ----------------------------------------------


def test_text_counts_as_written_only_in_every_language() -> None:
    entries = {"02987653004488": {"nl": {"usps": ["a"]}, "fr": {"usps": ["b"]}}}

    assert text_written_for(["2987653004488"], entries, ["nl", "fr"])
    assert not text_written_for(["2987653004488"], entries, ["nl", "fr", "de"])


def test_an_empty_tagline_list_is_not_text() -> None:
    entries = {"02987653004488": {"nl": {"usps": []}}}

    assert not text_written_for(["02987653004488"], entries, ["nl"])


def test_a_product_missing_from_the_file_is_not_written() -> None:
    assert not text_written_for(["02987653004488"], {}, ["nl"])
    assert text_written_for([], {}, ["nl"]), "nothing to write is nothing missing"
