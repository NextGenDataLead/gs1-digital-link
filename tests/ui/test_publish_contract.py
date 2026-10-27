"""The contract between the Publish screen and the gates it renders.

``lib/gates.py`` declares, for each gate, the options an operator may choose. A test already
checks those ids against ``flow-orchestrator/SKILL.md`` in both directions — but nothing checked
that the *screen* offers what a gate declares, and one gate quietly stopped.

The dry run declared ``Proceed`` and ``Cancel`` and rendered neither. Its handler set
``session.answers["dry_run"] = "proceed"`` when the subprocess finished, so the gate was answered
by the run *completing* rather than by anyone approving what it printed, and Cancel was
unreachable at the one gate whose entire purpose is to be read before the real write. A gate that
answers itself is not a gate.

Both checks are AST-only, so they need no NiceGUI and run in CI, which installs ``.[dev]``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from lib.gates import BY_ID

_PUBLISH: Final = Path(__file__).resolve().parent.parent.parent / "ui" / "pages" / "publish.py"
_PREFIX: Final = "_gate_"


def _renderers() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every ``_gate_<id>`` renderer whose id is a real gate, keyed by gate id."""
    tree = ast.parse(_PUBLISH.read_text("utf-8"), filename=str(_PUBLISH))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            _PREFIX
        ):
            gate_id = node.name[len(_PREFIX) :]
            if gate_id in BY_ID:
                found[gate_id] = node
    return found


def _named_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The one function called ``name`` in the publish screen."""
    tree = ast.parse(_PUBLISH.read_text("utf-8"), filename=str(_PUBLISH))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(found) == 1, f"expected exactly one {name}; found {len(found)}"
    return found[0]


def test_the_renderers_are_where_we_think() -> None:
    """A guard on the guard: these checks are worthless if they match nothing.

    ``missing_field`` is in the set because a gate served by the ``_gate_default`` fallback is
    invisible to every check in this file — which is exactly where it sat while it rendered on
    every run, asking about nothing. A dedicated renderer is what brings it under them.
    """
    rendered = _renderers()
    assert {"intent", "plan_review", "dry_run", "production", "missing_field"} <= set(rendered), (
        f"expected renderers for the load-bearing gates; found {sorted(rendered)}"
    )


def test_the_screen_tells_the_session_what_the_plan_dropped() -> None:
    """The refresh belongs in ``_redraw``, and the location is the whole point.

    The plan is built at gate 5, in the middle of the walk, so this fact is read once per redraw
    rather than once per run. Moving it to ``__init__`` — the obvious tidier-looking place — reads
    it before there is a plan, and gate 4 would then never appear on the run that needs it.
    """
    assigned = {
        target.attr
        for node in ast.walk(_named_function("_redraw"))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    }
    assert "units_missing_product_name" in assigned, (
        "_redraw does not refresh session.units_missing_product_name, so gate 4's applicability "
        "is decided by whatever the plan held when the screen was built"
    )


def test_the_missing_field_renderer_names_the_units() -> None:
    """Naming them is the fix, not merely hiding the gate when there is nothing to say.

    A gate that appears only when something was dropped but still cannot say *what* leaves the
    operator with the same unanswerable question, one run later.
    """
    attributes = {
        node.attr
        for node in ast.walk(_renderers()["missing_field"])
        if isinstance(node, ast.Attribute)
    }
    assert {"gtin", "language", "detail"} <= attributes, (
        "_gate_missing_field never reads the dropped units' gtin, language and detail, so it "
        f"cannot be naming them; it reaches {sorted(attributes)}"
    )


def _calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every function or method name called anywhere inside ``node``."""
    return {
        name
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        for name in (getattr(inner.func, "id", None) or getattr(inner.func, "attr", None),)
        if name
    }


def test_gate_zero_is_the_mode_and_its_buttons_only() -> None:
    """Operator, 2026-10-07: "remove all info but the buttons from step 0".

    Gate 0 carried the scope figures, the export path and age, the environment and three notes,
    and the one decision it asks for was the thing missed — two dry runs went out in ``pages``
    after ``both`` was meant. It once showed the catalogue total as its headline figure (127 on a
    one-product run); it now shows no figure at all, so neither that nor any other can creep back
    in without this failing.
    """
    calls = _calls(_renderers()["intent"])
    facts = {"figure", "figures", "scope_from", "product_count", "file_fact"}
    assert facts.isdisjoint(calls), f"_gate_intent shows facts again: {sorted(calls & facts)}"
    assert {"toggle", "_options"} <= calls, "gate 0 lost its mode choice or its buttons"


def test_no_gate_renderer_runs_its_own_preflight() -> None:
    """One doctor call per redraw, hoisted into ``_redraw`` and shared.

    Two gates need it. Fetched per renderer it would be two ~250 ms blocking subprocesses on
    every answer, in a function that is already holding the event loop — and the two gates could
    report different numbers for the same run, which is the more expensive half.
    """
    offenders = sorted(
        gate_id for gate_id, node in _renderers().items() if "run_json" in _calls(node)
    )
    assert not offenders, (
        f"these gate renderers run their own preflight: {offenders}. Read `self.doctor`, which "
        "_redraw refreshes once per pass"
    )


def test_no_gate_with_options_answers_itself() -> None:
    """Writing into ``session.answers`` is the screen deciding on the operator's behalf.

    ``languages`` is exempt by construction rather than by exception: it declares no options, so
    there is nothing for an operator to pick and the selection *is* the answer.
    """
    for gate_id, node in _renderers().items():
        if not BY_ID[gate_id].options:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Subscript)
                and isinstance(inner.ctx, ast.Store)
                and isinstance(inner.value, ast.Attribute)
                and inner.value.attr == "answers"
            ):
                raise AssertionError(
                    f"_gate_{gate_id} (line {inner.lineno}) writes into session.answers, so the "
                    f"gate answers itself — gate {gate_id!r} declares "
                    f"{[o.value for o in BY_ID[gate_id].options]} for the operator to choose"
                )


def _answerable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a renderer offers the shared option buttons, or bespoke ones that answer."""
    calls = {
        inner.func.attr
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
    }
    return bool({"_options", "_answer", "answer"} & calls)


def test_every_required_gate_with_options_offers_a_way_to_answer_them() -> None:
    """A required gate the screen cannot answer stops the run dead, or answers itself."""
    for gate_id, node in _renderers().items():
        gate = BY_ID[gate_id]
        if not gate.options or not gate.required:
            continue
        assert _answerable(node), (
            f"_gate_{gate_id} renders no way to answer it: gate {gate_id!r} is REQUIRED and "
            f"declares {[o.value for o in gate.options]}, so the run cannot proceed without the "
            "screen either offering them or answering on the operator's behalf"
        )


def test_every_gate_offers_the_options_it_declares_for_this_surface() -> None:
    """No exception list: what a screen must render is derived from the gates themselves.

    This used to be a hand-maintained set of two "known unrendered" gates, which is a note about
    debt rather than a check — it goes stale in both directions, and it cannot tell an option
    that is *missing* from one that could never have been here. ``chat_only`` says which is which
    in the data, so a gate with nothing for this surface is exempt by construction, and anything
    else must offer what it declares.
    """
    unrendered = {
        gate_id
        for gate_id, node in _renderers().items()
        if BY_ID[gate_id].shell_options and not _answerable(node)
    }
    assert not unrendered, (
        f"gates declaring options this screen never offers: {sorted(unrendered)}. Either render "
        "them, or mark the ones only the chat flow can honour `chat_only=True` in lib/gates.py "
        "with the reason in the gate's purpose"
    )


def test_a_chat_only_option_is_never_rendered_as_a_button() -> None:
    """The screen renders ``shell_options``, so this holds by construction — asserted anyway.

    A button reading "Explain each error" that explains nothing is worse than no button: it
    teaches an operator that the controls on this screen are decorative, on the one screen where
    reading before clicking is the entire safety mechanism.
    """
    source = _PUBLISH.read_text("utf-8")
    assert "gate.options" not in source, (
        "the publish screen iterates `gate.options`; it must render `gate.shell_options` so a "
        "chat-only option cannot become a button"
    )
    chat_only = [o for gate in BY_ID.values() for o in gate.options if o.chat_only]
    assert chat_only, "nothing is marked chat_only — this check would pass vacuously"
    for option in chat_only:
        assert option not in [o for gate in BY_ID.values() for o in gate.shell_options]


# --- re-planning published products ------------------------------------------


def test_the_plan_gate_offers_include_published_and_defaults_it_off() -> None:
    """It rewrites live pages, so it must be chosen — never inherited from a bare redraw."""
    source = _PUBLISH.read_text("utf-8")
    assert "include_published" in source
    assert "self.include_published = False" in source, "must default off"


def test_the_displayed_command_carries_the_flag() -> None:
    """A shown command that does not match the one the button sends is worse than none.

    Both the ``theme.command`` line and the click handler must build their argv with the same
    ``include_published`` value, or the screen tells the operator it is running one thing while
    running another.
    """
    tree = ast.parse(_PUBLISH.read_text("utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) == "run_plan_argv")
    ]
    assert len(calls) == 2, "expected exactly the displayed command and the executed one"
    for call in calls:
        for flag in ("include_published", "links_only"):
            assert any(kw.arg == flag for kw in call.keywords), (
                f"every run_plan_argv call on this screen must pass {flag} explicitly"
            )


def test_the_plan_gate_warns_when_the_plan_re_admits_published_products() -> None:
    """Mirrors the E19 band: it changes what a CHANGED row means, so it sits above the counts.

    Read from the summary rather than the checkbox, so the warning describes the plan on screen
    rather than the state of a control that may have been re-ticked since it was built.
    """
    source = _PUBLISH.read_text("utf-8")
    assert "summary.included_published" in source
    assert "rewrites a LIVE page" in source


# --- the per-row walk ----------------------------------------------------------


def _diff_truthiness_filters(node: ast.AST) -> list[int]:
    """Lines where a comprehension filters rows on a bare ``….diff``."""
    return [
        test.lineno
        for inner in ast.walk(node)
        if isinstance(inner, ast.comprehension)
        for test in inner.ifs
        if isinstance(test, ast.Attribute) and test.attr == "diff"
    ]


def test_the_row_gate_walks_every_changed_row_not_only_the_ones_carrying_a_diff() -> None:
    """The defect, asserted where it lived.

    ``state`` records the prior ``title`` and ``wp_url`` and nothing else, so a CHANGED row whose
    change is in the product body has no diff at all. This gate selected
    ``[row for row in plan.rows if row.diff]``, which on the real 24-row pilot plan displayed
    **one** row of the twenty it was confirming.

    Both halves are asserted, because the trap is that the obvious fix keeps the bug: adding
    per-row controls while still keying them on the diff reproduces the same blindness, one
    control at a time.
    """
    renderer = _renderers()["row_diff"]
    assert not _diff_truthiness_filters(renderer), (
        "_gate_row_diff still filters rows on `row.diff` being truthy — that is the selection "
        "which showed 1 row of 20"
    )
    names = {node.id for node in ast.walk(renderer) if isinstance(node, ast.Name)}
    assert "PlanClassification" in names, (
        "_gate_row_diff does not read the classification, so whatever it lists is not 'every "
        "CHANGED row'"
    )


def test_the_row_gate_offers_a_decision_per_row_and_not_one_for_the_gate() -> None:
    """``apply``/``skip`` are per-row on both surfaces now.

    Rendered through the shared ``_options`` they would become one gate-wide answer to a question
    asked once per row, which is the shape of the bug rather than the fix — so the gate excludes
    them there and the row renderer places them itself.
    """
    assert "_decide" in _calls(_named_function("_row_decision"))
    assert "apply_row" in _calls(_named_function("_decide"))

    options = _named_function("_options")
    assert any(arg.arg == "exclude" for arg in options.args.kwonlyargs), (
        "_options takes no `exclude`, so gate 6 cannot render apply/skip on the rows instead of "
        "on the gate"
    )
    assert "exclude" in {
        keyword.arg
        for node in ast.walk(_renderers()["row_diff"])
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }


def test_rebuilding_the_plan_forgets_the_per_row_decisions() -> None:
    """These rows are not the rows those decisions were made about.

    Carrying an *apply* across a rebuild is consent to publish a row in a form the operator may
    never have seen — the remembered-consent failure the production gate is enforced per run to
    avoid, one zoom level down.
    """
    assert "clear_row_decisions" in _calls(_named_function("build_plan"))


def test_the_screen_does_not_re_derive_which_rows_are_confirmed() -> None:
    """One rule, in the module that is tested without a browser.

    It used to be split: a module-level helper decided the classifications and ``_write_confirmed``
    decided the languages, and the half on the screen was the half no test could reach.
    """
    source = _PUBLISH.read_text("utf-8")
    assert "_confirmed_classifications" not in source, (
        "the classification rule is back on the screen; it belongs to PublishSession, which the "
        "session tests can exercise without NiceGUI"
    )
    confirmed = _named_function("_write_confirmed")
    assert "confirmed_pairs" in _calls(confirmed)
    assert not [node for node in ast.walk(confirmed) if isinstance(node, ast.ListComp)], (
        "_write_confirmed builds its own row list again — ask the session instead"
    )


def test_the_copy_gate_has_an_empty_state() -> None:
    """A required gate with nothing to approve must say so, not render an empty form.

    On a run that writes no pages the figures are three zeroes and the link goes to an empty list,
    so the gate asks the operator to approve text that does not exist. Answering it teaches them
    that answering it means nothing — which is the habit every gate here is built against.

    Asserted on the renderer rather than through a browser: the check is that the zero case is
    branched on at all, and an AST test runs in CI's required job, which has no NiceGUI.
    """
    renderer = _renderers()["content_review"]
    compared = [
        node
        for node in ast.walk(renderer)
        if isinstance(node, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == 0 for c in node.comparators)
    ]
    assert compared, "the copy gate no longer distinguishes 'nothing to review' from a shortfall"


def test_the_copy_gate_names_the_two_fields_it_is_about() -> None:
    """ "Copy" is this codebase's word. The operator's words are on the page they publish.

    The tagline and the Eigenschappen block are the only two things on a product page a machine
    wrote, and a gate that calls them "the copy" leaves the operator to work out which part of the
    page they are being asked to vouch for.
    """
    source = ast.get_source_segment(_PUBLISH.read_text("utf-8"), _renderers()["content_review"])
    assert source is not None
    assert "Eigenschappen" in source
    assert "tagline" in source
    assert "Eigenschappen" in BY_ID["content_review"].summary


def test_gate_zero_confirms_the_mode_the_batch_was_saved_with() -> None:
    """Operator, 2026-10-09: the mode is chosen on Data. Gate 0 shows it, and changing it goes
    back there — a second chooser here would be a second place the same answer can differ."""

    def text(name: str) -> str:
        return ast.unparse(_named_function(name))

    flow = ast.unparse(
        next(
            n
            for n in ast.walk(ast.parse(_PUBLISH.read_text("utf-8")))
            if isinstance(n, ast.ClassDef) and n.name == "_Flow"
        )
    )
    assert "context.batch_mode(cfg)" in flow, "Publish no longer takes the mode from the batch"
    assert "Change on the Data screen" in text("_gate_intent")
    assert "context.batch_mode(cfg) is None" in text("render"), (
        "a batch with no mode must stop Publish, not default it"
    )
