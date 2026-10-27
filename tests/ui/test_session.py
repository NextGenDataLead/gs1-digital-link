"""Tests for ui/session.py — the gate enforcement.

This is the safety-critical file in the shell, and the tests are written as the claims a reviewer
would want to check: that no command can be built past an unanswered gate, that cancel is never
read as consent, and that the production flag is a record of a decision rather than a restatement
of configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lib.gates import BY_ID, GATES, Mode
from lib.records import (
    LocalisedText,
    Plan,
    PlanClassification,
    PlanRow,
    ProductRecord,
    SkippedUnit,
    SkipReason,
)
from ui.session import GateNotAnsweredError, PublishSession

_CONFIRMED = "output/acme/plan.confirmed.json"

#: One unit `run_plan` dropped for E18, exactly as it would appear in ``plan.json``.
_DROPPED = SkippedUnit(
    gtin="02987653003276",
    language="fr",
    reason=SkipReason.MISSING_PRODUCT_NAME,
    detail="missing product_name.fr",
)


def _session(**overrides: object) -> PublishSession:
    params: dict[str, object] = {
        "client_id": "acme",
        "mode": Mode.BOTH,
        "has_generator": True,
        "is_production": True,
    }
    params.update(overrides)
    return PublishSession(**params)  # type: ignore[arg-type]


def _answer_everything(session: PublishSession) -> PublishSession:
    """Answer every required gate affirmatively — the only way to a real run."""
    session.answer("intent", "confirm")
    session.answer("content_review", "confirm")
    session.answer("plan_review", "all")
    session.answer("production", "confirm")
    session.answer("dry_run", "proceed")
    return session


# --- The refusal ---------------------------------------------------------------


def test_no_command_is_built_while_a_required_gate_is_outstanding() -> None:
    """The one rule. Not a warning, not a log line — a refusal.

    A shell that could be talked into building the command anyway would be prose again,
    wearing a form.
    """
    with pytest.raises(GateNotAnsweredError):
        _session().execute_argv(_CONFIRMED, dry_run=False)


def test_the_refusal_names_the_gate_so_a_screen_can_say_which() -> None:
    session = _session()
    session.answer("intent", "confirm")

    with pytest.raises(GateNotAnsweredError) as caught:
        session.execute_argv(_CONFIRMED, dry_run=False)

    assert "Plan review" in str(caught.value)
    assert "step 5" in str(caught.value)


def test_cancelling_one_gate_blocks_the_run_even_with_every_other_answered() -> None:
    session = _answer_everything(_session())
    session.answer("plan_review", "cancel")

    with pytest.raises(GateNotAnsweredError):
        session.execute_argv(_CONFIRMED, dry_run=False)
    assert session.cancelled


def test_switch_to_test_is_not_consent_to_run_against_production() -> None:
    """It reads like an answer. It is a refusal with a suggestion attached."""
    session = _answer_everything(_session())
    session.answer("production", "switch-to-test")

    with pytest.raises(GateNotAnsweredError):
        session.execute_argv(_CONFIRMED, dry_run=False)


def test_changed_review_proceeds_because_it_confirms_rather_than_aborts() -> None:
    session = _answer_everything(_session())
    session.answer("plan_review", "changed-review")

    assert session.execute_argv(_CONFIRMED, dry_run=False)


def test_show_full_diff_lifts_the_cap_without_cancelling_the_run() -> None:
    """Issue #76. The gate's only shell option was read as a refusal, and there was no way back.

    ``apply``/``skip`` are ``chat_only``, so ``show-full-diff`` is the *only* button the publish
    screen can render at gate 6 — reached by answering ``changed-review``, which is the most
    careful answer on offer. It lifts the row cap and means nothing else: the run must stay
    exactly as available as it was before the click.
    """
    session = _answer_everything(_session())
    session.answer("plan_review", "changed-review")
    session.answer("row_diff", "show-full-diff")

    assert not session.cancelled
    assert not session.refused("row_diff")
    assert session.execute_argv(_CONFIRMED, dry_run=False)


def test_change_mode_re_asks_gate_zero_rather_than_refusing_it() -> None:
    """A detour at a *required* gate: not a cancellation, but not consent either.

    Both halves are asserted because each alone is satisfied by a wrong answer. The run is still
    held — gate 0 is required and has not proceeded — but it is held as unanswered, which is what
    the screen then tells the operator, instead of claiming a cancel that never happened.
    """
    session = _answer_everything(_session())
    session.answer("intent", "change-mode")

    assert not session.cancelled
    assert "intent" in {gate.id for gate in session.outstanding}
    with pytest.raises(GateNotAnsweredError, match="not answered yet"):
        session.execute_argv(_CONFIRMED, dry_run=False)


def test_a_stop_the_run_answer_blocks_the_command_not_only_the_screen() -> None:
    """Gate 4's *Stop the run* is not ``required``, so nothing here used to enforce it.

    ``outstanding`` covers required gates only, and a refusal is *answered*, so this session built
    a command: the documented "abort before execute" was enforced by the publish screen returning
    early. Display logic is not the guard — this function is.
    """
    session = _answer_everything(_session(units_missing_product_name=(_DROPPED,)))
    session.answer("missing_field", "fail-run")

    assert not session.outstanding
    assert session.cancelled
    with pytest.raises(GateNotAnsweredError, match="refusal"):
        session.execute_argv(_CONFIRMED, dry_run=False)


def test_a_cancelled_dry_run_can_still_be_re_run_because_it_writes_nothing() -> None:
    """The dry-run gate is exempt from both checks, not just the outstanding one.

    Cancelling after reading the output ends the run — but the preview itself writes nothing, so
    refusing to rebuild the command that produces it would make the safe half of the flow the
    harder one to repeat.
    """
    session = _answer_everything(_session())
    session.answer("dry_run", "cancel")

    assert session.execute_argv(_CONFIRMED, dry_run=True)
    with pytest.raises(GateNotAnsweredError, match="refusal"):
        session.execute_argv(_CONFIRMED, dry_run=False)


# --- The dry run ---------------------------------------------------------------


def test_the_dry_run_does_not_need_its_own_answer_but_needs_every_other() -> None:
    """It is the thing being authorised at that point; requiring its answer first is a loop."""
    session = _session()
    session.answer("intent", "confirm")
    session.answer("content_review", "confirm")
    session.answer("plan_review", "all")
    session.answer("production", "confirm")

    argv = session.execute_argv(_CONFIRMED, dry_run=True)

    assert "--dry-run" in argv


def test_a_dry_run_still_needs_the_intent_gate() -> None:
    """A preview of a decision nobody made is not a preview, it is a suggestion."""
    with pytest.raises(GateNotAnsweredError):
        _session().execute_argv(_CONFIRMED, dry_run=True)


def test_the_dry_run_never_carries_the_production_flag() -> None:
    session = _answer_everything(_session())
    assert "--i-understand-production" not in session.execute_argv(_CONFIRMED, dry_run=True)


# --- The command --------------------------------------------------------------


def test_a_fully_answered_production_run_carries_the_flag() -> None:
    argv = _answer_everything(_session(mode=Mode.LINKS)).execute_argv(_CONFIRMED, dry_run=False)
    assert argv == [
        "-m",
        "scripts.run_execute",
        "acme",
        "--confirmed",
        _CONFIRMED,
        "--only",
        "links",
        "--i-understand-production",
    ]


def test_a_test_environment_run_never_carries_the_production_flag() -> None:
    """The flag is a record of a decision, and on test there is no such decision to record."""
    session = _session(is_production=False)
    session.answer("intent", "confirm")
    session.answer("content_review", "confirm")
    session.answer("plan_review", "all")
    session.answer("dry_run", "proceed")

    argv = session.execute_argv(_CONFIRMED, dry_run=False)

    assert "--i-understand-production" not in argv


def test_pages_mode_needs_no_production_gate_and_still_runs() -> None:
    """Gate 0 already named the environment, and nothing irreversible follows.

    The flag is still required. ``run_execute`` refuses **every** real run against a production
    client without ``--i-understand-production`` — its condition does not look at ``--only`` — so
    a pages command without it is refused at exit 2 and nothing publishes. This test previously
    asserted the flag was absent, and passed, because it checked the shape of the argv and never
    that the argv would be accepted.
    """
    session = _session(mode=Mode.PAGES)
    session.answer("intent", "confirm")
    session.answer("content_review", "confirm")
    session.answer("plan_review", "all")
    session.answer("dry_run", "proceed")

    argv = session.execute_argv(_CONFIRMED, dry_run=False)

    assert "--only" in argv and argv[argv.index("--only") + 1] == "pages"
    assert "--i-understand-production" in argv


def test_pages_mode_takes_the_acknowledgement_from_gate_zero() -> None:
    """Gate 0 is the substitute, so an unanswered gate 0 is not an acknowledgement."""
    session = _session(mode=Mode.PAGES)

    assert not session.production_acknowledged()

    session.answer("intent", "confirm")

    assert session.production_acknowledged()


def test_cancelling_gate_zero_is_not_an_acknowledgement() -> None:
    session = _session(mode=Mode.PAGES)
    session.answer("intent", "cancel")

    assert not session.production_acknowledged()


def test_links_mode_still_requires_its_own_production_gate() -> None:
    """The substitution applies only where the production gate is absent from the walk."""
    session = _session(mode=Mode.LINKS)
    session.answer("intent", "confirm")

    assert not session.production_acknowledged(), (
        "gate 0 must not stand in for the production gate when that gate is being asked"
    )

    session.answer("production", "confirm")

    assert session.production_acknowledged()


def test_a_client_without_a_generator_skips_the_copy_review() -> None:
    session = _session(has_generator=False, is_production=False)
    session.answer("intent", "confirm")
    session.answer("plan_review", "all")
    session.answer("dry_run", "proceed")

    assert session.execute_argv(_CONFIRMED, dry_run=False)
    assert "content_review" not in {gate.id for gate in session.gates}


def test_the_missing_field_gate_is_absent_until_the_plan_says_a_unit_was_dropped() -> None:
    """Gate 4 asks about units the plan dropped, so with no such units it is not in the walk.

    The session carries the units rather than a flag because the screen both hides the gate and
    names what it is about; one value cannot disagree with itself.
    """
    session = _session()
    assert "missing_field" not in {gate.id for gate in session.gates}

    session.units_missing_product_name = (_DROPPED,)
    assert "missing_field" in {gate.id for gate in session.gates}


def test_a_dropped_unit_never_blocks_the_run() -> None:
    """Gate 4 is not required, and must not become so.

    Its only non-proceeding option stops the run outright, so making it required would put every
    run with an E18 drop behind a gate whose sole way through is to accept the omission — a
    choice the operator should be free to make, not compelled to make.
    """
    session = _session(units_missing_product_name=(_DROPPED,))
    session.answer("intent", "confirm")

    assert "missing_field" not in {gate.id for gate in session.outstanding}


def test_a_stop_the_run_answer_stops_mattering_once_the_drops_are_gone() -> None:
    """Answering a gate that later stops applying is silently forgotten. Pinned, not endorsed.

    Defensible on its face — the reason for stopping is gone — but it is silent, and it is not
    specific to this gate: switching mode at gate 0 already discards a ``cancel`` answered at the
    production gate, and ``has_generator`` does the same to the copy review. Fixing it for one of
    the three would make three conditional gates behave three ways, so this states the current
    behaviour and the general fix is filed.
    """
    session = _session(units_missing_product_name=(_DROPPED,))
    session.answer("missing_field", "fail-run")
    assert session.cancelled

    session.units_missing_product_name = ()
    assert not session.cancelled


# --- Bookkeeping ---------------------------------------------------------------


def test_an_answer_that_is_not_an_option_is_rejected() -> None:
    """A session that looks answered and is not would be worse than one that raises."""
    with pytest.raises(KeyError):
        _session().answer("plan_review", "yes-please")


def test_next_gate_walks_the_flow_in_step_order() -> None:
    session = _session()
    assert session.next_gate is not None
    assert session.next_gate.id == "intent"
    session.answer("intent", "confirm")
    assert session.next_gate is not None
    assert session.next_gate.id == "languages"


def test_outstanding_lists_only_the_required_ones() -> None:
    session = _session()
    session.answer("intent", "confirm")
    outstanding = {gate.id for gate in session.outstanding}
    assert outstanding == {"content_review", "plan_review", "production", "dry_run"}
    assert "languages" not in outstanding  # answered or not, it never blocks a run


# --- Which rows a session confirms ---------------------------------------------
#
# The rule used to live half on the Publish screen and half in a module-level helper beside it,
# and the half on the screen — the language intersection — was the half no test could reach. It is
# one method now, so these are the claims a reviewer would want to check about what gets written.


def _product(gtin: str) -> ProductRecord:
    return ProductRecord(
        gtin=gtin, brand="Acme", product_name=LocalisedText(values={"nl": "Doek", "fr": "Chiffon"})
    )


def _row(gtin: str, language: str, classification: PlanClassification, **kwargs: object) -> PlanRow:
    return PlanRow(
        gtin=gtin,
        language=language,
        classification=classification,
        title="Doek",
        slug=f"p-{gtin}",
        content_hash="hash-" + gtin,
        target_url=f"https://wp.test/product/p-{gtin}/",
        product=_product(gtin),
        **kwargs,  # type: ignore[arg-type]
    )


#: A plan shaped like the real pilot one: a NEW row, and CHANGED rows of which only the first
#: carries a diff. That asymmetry is the whole reason this exists — see
#: ``test_a_changed_row_with_no_diff_is_still_confirmable``.
_NEW = _row("02987653000001", "nl", PlanClassification.NEW)
_CHANGED_WITH_DIFF = _row(
    "02987653000527",
    "fr",
    PlanClassification.CHANGED,
    diff={"title": ("Schoonmaakdoek", "Chiffon")},
)
_CHANGED_NO_DIFF = _row("02987653000002", "nl", PlanClassification.CHANGED)
_UNCHANGED = _row("02987653000003", "nl", PlanClassification.UNCHANGED)
_HELD = _row("02987653000004", "nl", PlanClassification.HELD)


def _plan(*rows: PlanRow) -> Plan:
    return Plan(
        client_id="acme",
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        total=len(rows),
        counts={},
        rows=list(rows),
    )


_FULL_PLAN = _plan(_NEW, _CHANGED_WITH_DIFF, _CHANGED_NO_DIFF, _UNCHANGED, _HELD)


def _reviewing(**overrides: object) -> PublishSession:
    """A session that has answered the plan review with *Review changed*."""
    session = _session(**overrides)
    session.answer("plan_review", "changed-review")
    return session


def test_all_confirms_every_new_and_changed_row() -> None:
    session = _session()
    session.answer("plan_review", "all")
    assert session.confirmed_pairs(_FULL_PLAN) == [
        [_NEW.gtin, "nl"],
        [_CHANGED_WITH_DIFF.gtin, "fr"],
        [_CHANGED_NO_DIFF.gtin, "nl"],
    ]


def test_new_only_leaves_the_changed_rows_alone() -> None:
    session = _session()
    session.answer("plan_review", "new-only")
    assert session.confirmed_pairs(_FULL_PLAN) == [[_NEW.gtin, "nl"]]


def test_unchanged_and_held_are_never_confirmed_whatever_was_answered() -> None:
    """``run_execute`` overrules both, so confirming them would offer a choice it ignores."""
    for answer in ("all", "new-only", "changed-review"):
        session = _session()
        session.answer("plan_review", answer)
        for row in (_UNCHANGED, _HELD):
            session.apply_row(row.gtin, row.language, applied=True)
        confirmed = {pair[0] for pair in session.confirmed_pairs(_FULL_PLAN)}
        assert _UNCHANGED.gtin not in confirmed, answer
        assert _HELD.gtin not in confirmed, answer


def test_review_changed_confirms_the_new_rows_and_nothing_else_until_a_row_is_applied() -> None:
    """**The defect.** This used to return exactly what ``all`` returns.

    Answering *Review changed* — the most careful answer on the menu — confirmed every CHANGED row
    without the operator having seen one of them. On the live plan that meant reviewing one row and
    publishing twenty.
    """
    assert _reviewing().confirmed_pairs(_FULL_PLAN) == [[_NEW.gtin, "nl"]]


def test_review_changed_confirms_the_rows_that_were_applied() -> None:
    session = _reviewing()
    session.apply_row(_CHANGED_NO_DIFF.gtin, "nl", applied=True)
    assert session.confirmed_pairs(_FULL_PLAN) == [
        [_NEW.gtin, "nl"],
        [_CHANGED_NO_DIFF.gtin, "nl"],
    ]


def test_apply_all_confirms_every_row_it_is_given_and_overrides_a_skip() -> None:
    session = _reviewing()
    session.apply_row(_CHANGED_NO_DIFF.gtin, "nl", applied=False)

    session.apply_rows([_CHANGED_NO_DIFF, _CHANGED_WITH_DIFF])

    confirmed = session.confirmed_pairs(_FULL_PLAN)
    assert [_CHANGED_NO_DIFF.gtin, "nl"] in confirmed
    assert [_CHANGED_WITH_DIFF.gtin, _CHANGED_WITH_DIFF.language] in confirmed


def test_apply_all_decides_only_the_rows_it_is_given() -> None:
    """The screen passes the rows it shows; one past the cap stays undecided, so unpublished."""
    session = _reviewing()

    session.apply_rows([_CHANGED_NO_DIFF])

    assert session.row_applied(_CHANGED_WITH_DIFF.gtin, _CHANGED_WITH_DIFF.language) is None


def test_a_skipped_row_is_not_confirmed() -> None:
    session = _reviewing()
    session.apply_row(_CHANGED_WITH_DIFF.gtin, "fr", applied=False)
    session.apply_row(_CHANGED_NO_DIFF.gtin, "nl", applied=True)
    assert [pair[0] for pair in session.confirmed_pairs(_FULL_PLAN)] == [
        _NEW.gtin,
        _CHANGED_NO_DIFF.gtin,
    ]


def test_an_undecided_row_is_not_confirmed_and_is_not_the_same_as_a_skipped_one() -> None:
    """Three states, not two. A row nobody has looked at is not a row anybody approved — and the
    screen still has to be able to tell the operator which ones they have not reached."""
    session = _reviewing()
    session.apply_row(_CHANGED_WITH_DIFF.gtin, "fr", applied=False)

    assert session.row_applied(_CHANGED_WITH_DIFF.gtin, "fr") is False
    assert session.row_applied(_CHANGED_NO_DIFF.gtin, "nl") is None
    assert not session.confirms(_CHANGED_NO_DIFF)


def test_a_changed_row_with_no_diff_is_still_confirmable() -> None:
    """The row the old gate could not even show.

    State records the prior ``title`` and ``wp_url`` and nothing else, so a change in the product
    body leaves ``diff`` empty. 19 of the pilot plan's 20 CHANGED rows look like this, and a walk
    keyed on the diff would offer a decision on one of them.
    """
    assert _CHANGED_NO_DIFF.diff is None

    session = _reviewing()
    session.apply_row(_CHANGED_NO_DIFF.gtin, "nl", applied=True)
    assert session.confirms(_CHANGED_NO_DIFF)


def test_the_language_subset_narrows_the_confirmed_rows() -> None:
    session = _session(languages=["nl"])
    session.answer("plan_review", "all")
    assert session.confirmed_pairs(_FULL_PLAN) == [
        [_NEW.gtin, "nl"],
        [_CHANGED_NO_DIFF.gtin, "nl"],
    ]


def test_no_language_chosen_means_every_language_rather_than_none() -> None:
    """A run scoped to no language would confirm nothing, publish nothing and report success."""
    session = _session(languages=[])
    session.answer("plan_review", "all")
    assert len(session.confirmed_pairs(_FULL_PLAN)) == 3


def test_rebuilding_the_plan_forgets_the_row_decisions() -> None:
    """Consent carried across a rebuild is consent to publish a row in a form nobody reviewed."""
    session = _reviewing()
    session.apply_row(_CHANGED_NO_DIFF.gtin, "nl", applied=True)
    session.clear_row_decisions()

    assert session.row_applied(_CHANGED_NO_DIFF.gtin, "nl") is None
    assert session.confirmed_pairs(_FULL_PLAN) == [[_NEW.gtin, "nl"]]


def test_the_gtins_are_passed_through_byte_for_byte() -> None:
    """``run_execute`` silently ignores a pair with no matching row, so a well-meant reformat here
    would drop rows from the run without saying anything."""
    odd = _row("2987653000527", "nl", PlanClassification.NEW)  # 13 digits, not zero-padded
    session = _session()
    session.answer("plan_review", "all")
    assert session.confirmed_pairs(_plan(odd)) == [["2987653000527", "nl"]]


# --- which gates a screen stands open ----------------------------------------


def _answered(**answers: str) -> PublishSession:
    """A session with these gate answers and nothing else. Named apart from ``_session`` above:
    a second ``def _session`` in this module silently replaced the first for every test in it."""
    session = _session()
    session.answers.update(answers)
    return session


def test_no_required_gate_folds_before_it_is_answered() -> None:
    """The line the fold must not cross.

    The gates are this project's safety mechanism, and one folded behind a chevron is one answered
    without being read. Folding removes the scroll around the question, never the question.
    """
    session = _session()
    for gate in GATES:
        if gate.required:
            assert not session.folds(gate), f"{gate.id} is required and would render folded"


def test_an_answered_gate_folds_whatever_it_is() -> None:
    """Including the required ones: a decision already made is not the one being made."""
    for gate in GATES:
        assert _answered(**{gate.id: "confirm"}).folds(gate), f"{gate.id} stays open once answered"


def test_the_row_walk_opens_exactly_when_the_operator_asked_for_it() -> None:
    """Gate 6 is optional only in the sense that a run can avoid reaching it.

    Choosing ``changed-review`` at gate 5 *is* asking to walk every changed row, and a walk that
    arrives collapsed is the scroll-saving rule doing harm: the rows nobody opens are the rows
    nobody decides, and an undecided row is not published.
    """
    row_diff = BY_ID["row_diff"]

    assert _answered().folds(row_diff)
    assert _answered(plan_review="all").folds(row_diff)
    assert not _answered(plan_review="changed-review").folds(row_diff)


def test_folding_does_not_depend_on_which_gate_is_next() -> None:
    """The rule this replaces did, and the languages gate broke it.

    That gate is answered only by *changing* the languages, so an operator who accepts the default
    never answers it — it stayed ``next_gate`` for the whole walk, and the row walk could
    therefore never open. Pinned here: the row walk opens on its own answer while the languages
    gate is still untouched, and is still the one ``next_gate`` names.
    """
    session = _answered(intent="confirm", plan_review="changed-review")

    # The walk is past gate 0 and the operator has taken the default languages, so this is where
    # ``next_gate`` sticks — for the rest of the run, whatever else they answer.
    assert session.next_gate is not None
    assert session.next_gate.id == "languages"
    assert not session.folds(BY_ID["row_diff"])
    assert session.folds(BY_ID["languages"])
