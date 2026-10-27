"""Tests for lib/gates.py — including the drift check that keeps it honest.

The gates are this project's safety mechanism and they are written twice: as prose a model reads
in ``.claude/skills/flow-orchestrator/SKILL.md``, and as structure a form-rendering UI reads in
``lib/gates.py``. Two implementations of one contract drift, and this one drifts *silently* — a
gate that quietly stops being shown raises nothing at all.

So the SKILL carries a **Gate index** table and these tests assert the two agree in both
directions. Adding a gate to either without the other fails CI.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Final

import pytest

from lib.gates import (
    GATES,
    PERMANENCE_WARNING,
    Gate,
    GateOutcome,
    Mode,
    gates_for,
    run_execute_argv,
)

_SKILL: Final = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude"
    / "skills"
    / "flow-orchestrator"
    / "SKILL.md"
)

#: The most a gate's one-line summary may run to. Two lines on a card at the width this
#: screen uses; past that it is a paragraph wearing a short field's name.
_SUMMARY_CHARS: Final = 110

#: One row of the Gate index table: | `id` | step | required | modes |
_ROW: Final = re.compile(
    r"^\|\s*`(?P<id>[a-z_]+)`\s*\|\s*(?P<step>[\d.]+)\s*\|(?P<required>[^|]*)\|(?P<modes>[^|]*)\|",
    re.MULTILINE,
)


def _indexed_gates() -> dict[str, str]:
    """The gate id → step mapping the SKILL's Gate index declares."""
    return {m.group("id"): m.group("step") for m in _ROW.finditer(_SKILL.read_text("utf-8"))}


def _indexed_modes() -> dict[str, str]:
    """The gate id → Modes-cell mapping the SKILL's Gate index declares."""
    return {m.group("id"): m.group("modes") for m in _ROW.finditer(_SKILL.read_text("utf-8"))}


#: Each applicability flag on :class:`~lib.gates.Gate`, and the word its Modes cell must contain
#: when the flag is set — and must not contain when it is not.
_MODE_CONDITIONS: Final = {
    "needs_generator": "generator",
    "needs_production": "production",
    "needs_missing_product_name": "product_name",
}


# --- Drift ---------------------------------------------------------------------


def test_every_gate_in_code_is_documented_in_the_skill() -> None:
    documented = _indexed_gates()
    assert {gate.id for gate in GATES} <= set(documented), (
        "a gate exists in lib/gates.py with no entry in the SKILL's Gate index — the model "
        "driving the flow would never present it"
    )


def test_every_gate_in_the_skill_exists_in_code() -> None:
    documented = _indexed_gates()
    assert set(documented) <= {gate.id for gate in GATES}, (
        "the SKILL's Gate index names a gate lib/gates.py does not define — a UI rendering the "
        "gates would silently skip it"
    )


def test_the_step_numbers_agree() -> None:
    """The numbering is load-bearing: cross-references to "step 8" must keep meaning step 8."""
    documented = _indexed_gates()
    assert {gate.id: gate.step for gate in GATES} == documented


def test_the_modes_column_names_every_condition_a_gate_carries() -> None:
    """The Modes cell is prose, and until now nothing compared it to the code.

    That is how the missing-field gate shipped: its cell said ``all`` while the gate was meant to
    fire only on a plan that dropped something, and the two could not be reconciled because only
    the id and the step were ever checked. A reader of the SKILL — which is to say the model
    driving the flow — had no way to learn a condition the table did not mention.

    Checked in both directions, like the id sets: a flag the cell does not name, and a cell naming
    a condition the gate does not carry, both fail.
    """
    modes = _indexed_modes()
    for gate in GATES:
        cell = modes[gate.id].lower()
        for attribute, word in _MODE_CONDITIONS.items():
            assert (word in cell) is getattr(gate, attribute), (
                f"the Gate index's Modes cell for {gate.id!r} and its {attribute} disagree: "
                f"cell is {modes[gate.id].strip()!r}, flag is {getattr(gate, attribute)}"
            )


def test_the_skill_offers_every_option_including_the_chat_only_ones() -> None:
    """A ``chat_only`` option is one this surface *keeps*, not one being retired.

    The distinction is the whole point of the flag. Marking ``detail`` chat-only rather than
    deleting it was a choice to let each surface offer what it can honour — the shell has no
    model to read a run log and explain it, and the chat flow does. If a later edit "tidied" a
    chat-only option out of ``lib/gates.py``, the shell would not notice, because the shell never
    rendered it. The skill would, and this is what says so.
    """
    skill = _SKILL.read_text("utf-8")
    missing = [
        f"{gate.id}.{option.value}"
        for gate in GATES
        for option in gate.options
        if option.value not in skill
    ]
    assert not missing, f"options the SKILL never offers the operator: {missing}"


def test_a_chat_only_option_is_dropped_from_the_shell_but_kept_in_the_contract() -> None:
    """Both halves matter: absent from ``shell_options``, present in ``options``."""
    chat_only = [(g, o) for g in GATES for o in g.options if o.chat_only]
    assert chat_only, "nothing is marked chat_only — this check would pass vacuously"
    for gate, option in chat_only:
        assert option in gate.options
        assert option not in gate.shell_options
        assert not option.in_shell


def test_every_gate_keeps_at_least_one_option_for_each_surface_it_can_serve() -> None:
    """A gate whose every option is chat-only renders as information in the shell, not a dead end.

    ``post_run`` is the case: it keeps ``yes``/``no`` for both surfaces and marks only ``detail``.
    A gate that lost *all* its shell options while still being required would stop the run.
    """
    for gate in GATES:
        if gate.required and gate.options:
            assert gate.shell_options, f"required gate {gate.id!r} has nothing the shell can offer"


def test_the_skill_still_carries_the_permanence_warning_verbatim() -> None:
    """The one fact that makes the flow's caution proportionate. It must not be paraphrased.

    Compared with whitespace collapsed, since the SKILL wraps it inside a fenced block and where
    the line breaks fall is not part of the claim.
    """
    assert _collapse(PERMANENCE_WARNING) in _collapse(_SKILL.read_text("utf-8"))


def _collapse(text: str) -> str:
    return " ".join(text.split())


# --- Applicability -------------------------------------------------------------


def test_the_production_gate_never_fires_in_pages_mode() -> None:
    """Not laxity: gate 0 already named the environment and nothing irreversible follows.

    A second production prompt for a page you can delete only trains the operator to click
    through them, which costs at the gate that matters.
    """
    fired = {
        gate.id
        for gate in gates_for(
            mode=Mode.PAGES,
            has_generator=True,
            is_production=True,
            has_missing_product_name=True,
        )
    }
    assert "production" not in fired


@pytest.mark.parametrize("mode", [Mode.LINKS, Mode.BOTH])
def test_the_production_gate_always_fires_on_a_permanent_production_run(mode: Mode) -> None:
    fired = {
        gate.id
        for gate in gates_for(
            mode=mode,
            has_generator=False,
            is_production=True,
            has_missing_product_name=False,
        )
    }
    assert "production" in fired


@pytest.mark.parametrize("mode", list(Mode))
def test_the_dry_run_gate_fires_in_every_mode(mode: Mode) -> None:
    """Mandatory, and the cheapest thing in the flow."""
    for production in (True, False):
        fired = gates_for(
            mode=mode,
            has_generator=False,
            is_production=production,
            has_missing_product_name=False,
        )
        assert any(gate.id == "dry_run" and gate.required for gate in fired)


def test_content_review_does_not_fire_in_links_mode() -> None:
    """A links run renders no page and plans with `--links-only`, which does not apply E21.

    The gate used to fire here because an unfilled cache emptied a links plan too. Asking the
    operator to review copy that will never reach a page is a gate answered without reading.
    """
    fired = {
        gate.id
        for gate in gates_for(
            mode=Mode.LINKS,
            has_generator=True,
            is_production=False,
            has_missing_product_name=False,
        )
    }
    assert "content_review" not in fired


def test_content_review_does_not_fire_without_a_generator() -> None:
    fired = {
        gate.id
        for gate in gates_for(
            mode=Mode.BOTH,
            has_generator=False,
            is_production=False,
            has_missing_product_name=False,
        )
    }
    assert "content_review" not in fired


def _fired(*, has_missing_product_name: bool) -> set[str]:
    """Every gate id for a maximal run — every other applicability input switched on."""
    return {
        gate.id
        for gate in gates_for(
            mode=Mode.BOTH,
            has_generator=True,
            is_production=True,
            has_missing_product_name=has_missing_product_name,
        )
    }


def test_the_missing_field_gate_does_not_fire_when_nothing_was_dropped() -> None:
    """The defect this gate's applicability flag exists to fix.

    It used to render on every run: a card headed "one per unit dropped for a missing
    product_name" above a button reading "Skip this unit", with no unit — because ``gates_for``
    filtered on mode, generator and environment and never on the plan. Of its three answers only
    ``fail-run`` did anything, so the sole live control on a question about nothing was the one
    that stops the run. A gate that asks about nothing teaches answering without reading, and
    that habit is spent at the gates that matter.
    """
    assert "missing_field" not in _fired(has_missing_product_name=False)


def test_the_missing_field_gate_fires_once_a_unit_was_dropped() -> None:
    """The other half, and not a formality: without it the check above passes by deletion."""
    assert "missing_field" in _fired(has_missing_product_name=True)


def test_the_plan_fact_changes_exactly_one_gate() -> None:
    """Whether the plan dropped a unit decides the missing-field gate and nothing else.

    Stated as a set difference rather than as two membership checks, so a later flag put on a
    second gate — silently widening what a plan fact can hide — fails here.
    """
    with_drops = _fired(has_missing_product_name=True)
    without = _fired(has_missing_product_name=False)
    assert with_drops - without == {"missing_field"}
    assert without - with_drops == set()


def test_every_applicability_input_must_be_supplied() -> None:
    """No applicability input may acquire a default. This is the module's whole premise.

    A defaulted one means a caller that forgets it gets a walk quietly missing a gate — and, in
    this module's own words, a gate that quietly stops being shown raises nothing at all. Without
    this check, adding ``= False`` to any of them is an edit no test would notice.
    """
    parameters = inspect.signature(gates_for).parameters
    assert set(parameters) == {
        "mode",
        "has_generator",
        "is_production",
        "has_missing_product_name",
    }
    for name, parameter in parameters.items():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name


def test_gates_come_back_in_step_order() -> None:
    """A UI walks them in the order returned; out of order it would ask to confirm a plan
    it has not built yet."""
    steps = [
        float(gate.step)
        for gate in gates_for(
            mode=Mode.BOTH,
            has_generator=True,
            is_production=True,
            has_missing_product_name=True,
        )
    ]
    assert steps == sorted(steps)


def test_every_required_gate_says_why_it_exists() -> None:
    """A UI that shows only the question trains an operator to answer without reading."""
    for gate in GATES:
        assert gate.purpose, gate.id
        if gate.required:
            assert len(gate.purpose) > _MIN_PURPOSE_CHARS, gate.id


_MIN_PURPOSE_CHARS: Final = 80


# --- The command the gates authorise -------------------------------------------


def test_both_mode_omits_only_because_omitting_it_is_what_both_means() -> None:
    argv = run_execute_argv("acme", mode=Mode.BOTH, confirmed_path="p.json", dry_run=False)
    assert "--only" not in argv


@pytest.mark.parametrize("mode", [Mode.PAGES, Mode.LINKS])
def test_single_leg_modes_pass_only(mode: Mode) -> None:
    argv = run_execute_argv("acme", mode=mode, confirmed_path="p.json", dry_run=False)
    assert argv[argv.index("--only") + 1] == mode.value


def test_the_production_flag_is_absent_until_the_gate_is_answered() -> None:
    """The flag records a decision, not a fact about the environment.

    Deriving it from `gs1.environment` would make it a description of where the run points
    rather than a record that someone confirmed it — which is the whole point of the guard.
    """
    argv = run_execute_argv(
        "acme",
        mode=Mode.BOTH,
        confirmed_path="p.json",
        dry_run=False,
        production_acknowledged=False,
    )
    assert "--i-understand-production" not in argv


def test_the_dry_run_never_carries_the_production_flag() -> None:
    """It writes nothing, so it needs no authorisation — and must not consume one."""
    argv = run_execute_argv(
        "acme", mode=Mode.BOTH, confirmed_path="p.json", dry_run=True, production_acknowledged=True
    )
    assert "--dry-run" in argv
    assert "--i-understand-production" not in argv


def test_the_real_run_carries_the_flag_once_the_gate_is_answered() -> None:
    argv = run_execute_argv(
        "acme",
        mode=Mode.LINKS,
        confirmed_path="output/acme/plan.confirmed.json",
        dry_run=False,
        production_acknowledged=True,
    )
    assert argv == [
        "-m",
        "scripts.run_execute",
        "acme",
        "--confirmed",
        "output/acme/plan.confirmed.json",
        "--only",
        "links",
        "--i-understand-production",
    ]


def test_the_dry_run_argv_matches_the_real_one_but_for_the_two_flags() -> None:
    """SKILL step 8.5: "the *same* command with --dry-run added and every other flag identical"."""
    common = {"mode": Mode.PAGES, "confirmed_path": "p.json", "production_acknowledged": True}
    dry = run_execute_argv("acme", dry_run=True, **common)  # type: ignore[arg-type]
    real = run_execute_argv("acme", dry_run=False, **common)  # type: ignore[arg-type]

    assert [a for a in dry if a != "--dry-run"] == [
        a for a in real if a != "--i-understand-production"
    ]


def test_every_gate_offers_a_way_out(  # noqa: D401 — the name is the assertion
) -> None:
    """A required gate with no way to stop the run is not a gate, it is a notification.

    ``refuses``, not ``not proceeds``: a way out has to be an actual stop. An option that
    re-presents the same gate satisfies "does not proceed" while leaving the run exactly as
    available as it was, so the weaker assertion would accept a gate whose only alternative to
    consent is being asked again.
    """
    for gate in GATES:
        if gate.required and gate.options:
            assert any(option.refuses for option in gate.options), gate.id


def test_cancel_never_reads_as_consent() -> None:
    """The one wrong answer that would be catastrophic, so it is asserted rather than assumed.

    Asserts ``refuses`` rather than ``not proceeds`` for the reason above, and here it is
    load-bearing: since :class:`~lib.gates.GateOutcome` gained a third state, ``not proceeds`` is
    also true of every detour. An edit that made ``cancel`` a ``REDISPLAYS`` would pass the weaker
    form of this test while making ``PublishSession.cancelled`` return ``False`` on a cancel.
    """
    for gate in GATES:
        for option in gate.options:
            if option.value in {"cancel", "fail-run", "switch-to-test"}:
                assert option.refuses, f"{gate.id}/{option.value}"


def test_a_redisplay_option_is_never_read_as_a_refusal() -> None:
    """The mirror of ``test_cancel_never_reads_as_consent``, and the one that was missing.

    That test pins the answers which must never be read as consent. Nothing pinned the converse —
    that an answer which only changes what is on screen is never read as a refusal — and the gap
    is what shipped issue #76: ``show-full-diff`` is the only option the shell can render at gate
    6, and answering it cancelled the run.

    Named options rather than "everything already marked ``REDISPLAYS``", mirroring that test down
    to its shape: a check derived from the flag it is checking passes the moment someone changes
    the flag, which is precisely the edit that must fail here.
    """
    detours = {"show-full-diff", "change-mode", "regenerate", "detail"}
    seen = set()
    for gate in GATES:
        for option in gate.options:
            if option.value in detours:
                seen.add(option.value)
                assert option.outcome is GateOutcome.REDISPLAYS, f"{gate.id}/{option.value}"
                assert not option.refuses, f"{gate.id}/{option.value} reads as a refusal"
                assert not option.proceeds, f"{gate.id}/{option.value} reads as consent"
    assert seen == detours, f"never checked: {sorted(detours - seen)}"


def test_no_gate_is_a_dead_end_in_the_shell() -> None:
    """Every gate the shell can render leaves at least one way on from it.

    The general form of #76, and the assertion that would have caught it: at gate 6 the only
    option not marked ``chat_only`` was one the shell read as a refusal, so the single button on
    the card ended the run with nothing on the screen to undo it. Written over ``shell_options``
    because that is what the screen renders — a gate can offer nothing but refusals in the chat
    flow and still be answerable there, where the operator can simply say something else.
    """
    for gate in GATES:
        if not gate.shell_options:
            continue
        assert any(not option.refuses for option in gate.shell_options), (
            f"every option gate {gate.id!r} can render in the shell stops the run: "
            f"{[o.value for o in gate.shell_options]} — answering it would be a dead end with no "
            "way back short of reloading the page and re-answering every gate"
        )


def test_changed_review_proceeds_even_though_it_reads_like_a_detour() -> None:
    """It confirms rows one at a time; it does not abort. Inferring from the word gets it wrong."""
    plan_review = next(gate for gate in GATES if gate.id == "plan_review")
    assert next(o for o in plan_review.options if o.value == "changed-review").proceeds


def test_gate_ids_are_unique() -> None:
    ids = [gate.id for gate in GATES]
    assert len(ids) == len(set(ids))


def test_a_gate_knows_its_own_applicability() -> None:
    gate = Gate(
        id="x",
        step="1",
        title="X",
        purpose="why",
        summary="what",
        options=(),
        required=False,
        modes=frozenset({Mode.LINKS}),
        needs_production=True,
    )
    assert gate.applies(
        mode=Mode.LINKS, has_generator=False, is_production=True, has_missing_product_name=False
    )
    assert not gate.applies(
        mode=Mode.LINKS, has_generator=False, is_production=False, has_missing_product_name=False
    )
    assert not gate.applies(
        mode=Mode.PAGES, has_generator=False, is_production=True, has_missing_product_name=False
    )


def test_a_gate_can_depend_on_what_the_plan_dropped() -> None:
    """Applicability can turn on a fact about the plan, not only on configuration.

    The distinction is operational rather than academic: mode, generator and environment are
    settled before the walk begins, and this one is not — the plan is built at step 5, halfway
    through — so a consumer must re-ask it rather than resolve it once.
    """
    gate = Gate(
        id="x",
        step="1",
        title="X",
        purpose="why",
        summary="what",
        options=(),
        required=False,
        modes=frozenset({Mode.PAGES}),
        needs_missing_product_name=True,
    )
    assert gate.applies(
        mode=Mode.PAGES, has_generator=False, is_production=False, has_missing_product_name=True
    )
    assert not gate.applies(
        mode=Mode.PAGES, has_generator=False, is_production=False, has_missing_product_name=False
    )


def test_every_gate_carries_a_short_line_as_well_as_its_why() -> None:
    """``summary`` is required, and it is required to be short.

    A form leads with it and keeps ``purpose`` one press away, so a gate whose summary is another
    paragraph reproduces the slab it was added to remove — and one with no summary at all would
    render a card with a title and no idea what it is for. Both failures are silent on screen.
    """
    for gate in GATES:
        assert gate.summary.strip(), f"{gate.id} has no summary"
        assert len(gate.summary) <= _SUMMARY_CHARS, (
            f"{gate.id}'s summary is {len(gate.summary)} characters; it leads a card, so it has "
            f"to fit on a line or two. The long version belongs in `purpose`."
        )
        assert len(gate.summary) < len(gate.purpose), (
            f"{gate.id}'s summary is not shorter than its purpose — one of them is in the wrong "
            f"field"
        )
