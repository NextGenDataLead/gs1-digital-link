"""The contract between the Content screen and the scope it is supposed to respect.

The screen showed a *scoped* coverage figure directly above an *unscoped* list of copy, with
nothing to tell them apart. The figures came from the doctor; the list read
``generation_results.json`` off disk and rendered every GTIN in it. That file is written per run
now, but a results file produced against a longer process list carries the same trap.

AST-only, so this needs no NiceGUI and runs in the required CI job rather than the optional one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_CONTENT: Final = Path(__file__).resolve().parent.parent.parent / "ui" / "pages" / "content.py"


def _tree() -> ast.Module:
    return ast.parse(_CONTENT.read_text("utf-8"), filename=str(_CONTENT))


def _function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(found) == 1, f"expected exactly one {name}; found {len(found)}"
    return found[0]


def _calls(node: ast.AST) -> set[str]:
    return {
        name
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        for name in (getattr(inner.func, "id", None) or getattr(inner.func, "attr", None),)
        if name
    }


def _own_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The calls a function makes itself, not those made by functions nested inside it.

    ``ast.walk`` descends into nested ``def``s, so a closure's call is otherwise attributed to
    every function that encloses it — and this screen's shared refresh is a closure.
    """
    nested = {
        inner
        for child in node.body
        for inner in ast.walk(child)
        if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    owned = {inner for child in node.body for inner in ast.walk(child)} - {
        deep for fn in nested for deep in ast.walk(fn)
    }
    return {
        name
        for inner in owned
        if isinstance(inner, ast.Call)
        for name in (getattr(inner.func, "id", None) or getattr(inner.func, "attr", None),)
        if name
    }


def test_the_screen_asks_the_site_once_and_draws_everything_from_that() -> None:
    """One read, one renderer.

    The figures and the regenerate list are two views of the same answer. Fetched separately they
    would be two subprocesses per redraw — and the expensive half — one could move without the
    other, so a screen could offer to regenerate a product the counts above had just called
    textless. Both are required to come from the same ``show``.
    """
    fetchers = sorted(
        node.name
        for node in ast.walk(_tree())
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and "report_live_copy_argv" in _own_calls(node)
    )
    assert fetchers == ["refresh"], f"expected one fetcher; found {fetchers}"
    assert "show" in _calls(_function("refresh"))


def test_the_site_is_read_off_the_event_loop() -> None:
    """It is one request per page. Blocking the loop would freeze the whole window for it."""
    assert "run_json_off_the_loop" in _calls(_function("refresh"))
    assert "run_json" not in _own_calls(_function("refresh"))


def test_generating_re_asks_the_site_it_just_changed() -> None:
    """The counts and the list describe the site as it was before the write.

    A screen that keeps showing "11 need text" after writing text for all eleven is the silent
    staleness this project keeps designing against — and the handler is the one place that knows
    the site changed.
    """
    handler = next(
        node
        for node in ast.walk(_function("_generate_panel"))
        if isinstance(node, ast.AsyncFunctionDef)
    )
    assert "refresh" in _calls(handler)


def test_generation_reports_its_outcome_in_something_that_must_be_dismissed() -> None:
    """A toast decides for the operator how long the only account of a run stays on screen.

    Writing text for a batch takes minutes, which is long enough that nobody watches it, so the
    message lands on an empty chair. Both outcomes go through the modal, the failing one included.
    """
    handler = next(
        node
        for node in ast.walk(_function("_generate_panel"))
        if isinstance(node, ast.AsyncFunctionDef)
    )
    calls = _calls(handler)
    assert "announce" in calls
    assert not (calls & {"notify_ok", "notify_warning", "notify_problem"})


def test_generation_shows_no_console_block() -> None:
    """Raw output under a button reads as something the operator should understand.

    The lines are still captured, because a failure has to be explainable; they are shown only
    then, in the dialog, where there is a reason to read them.
    """
    assert "log" not in _calls(_function("_generate_panel"))
    assert "stream" in _calls(_function("_generate_panel")), (
        "dropping the console must not turn the write into a blocking call"
    )


def test_no_generate_button_without_a_key() -> None:
    """An action that can only fail is worse than an absence — you must run it to find out.

    The presence check comes from ``env_edit.describe``, which reads ``.env`` as text and returns
    presence and length only. Asserting the early ``return`` is what stops a later edit turning
    the guard into a band that merely sits above a live button.
    """
    button = _function("_generate_panel")
    assert "describe" in _own_calls(button)
    guard = next(
        (node for node in button.body if isinstance(node, ast.If)),
        None,
    )
    assert guard is not None
    assert any(isinstance(node, ast.Return) for node in ast.walk(guard))


def test_nothing_is_preselected_for_the_override() -> None:
    """Regenerating rewrites text that is live and, as far as anything here knows, correct.

    So the default has to be "do nothing". A screen that arrives with rows ticked is a screen
    that rewrites a batch because somebody pressed the obvious button, and the write is the one
    thing on this screen that costs money and changes a live page.
    """
    checkboxes = [
        node
        for node in ast.walk(_function("_override"))
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "checkbox"
    ]
    assert checkboxes, "the regenerate list no longer renders tick boxes"
    for call in checkboxes:
        value = next((kw.value for kw in call.keywords if kw.arg == "value"), None)
        assert isinstance(value, ast.Constant) and value.value is False, (
            "a regenerate row is pre-ticked"
        )


def test_the_screen_names_the_two_fields_it_writes() -> None:
    """ "Copy" is this codebase's word; the operator's words are on the page they publish."""
    source = _CONTENT.read_text("utf-8")
    assert "Eigenschappen" in source
    assert "tagline" in source


def test_each_figure_says_what_happens_to_its_products() -> None:
    """Three states, three verbs. The label is the decision, not the description.

    "have text / need text / cannot be written" names three conditions and leaves the operator to
    work out which one the button acts on — and on this screen two of the three are skipped for
    completely different reasons, one fixable here and one only in MyGS1. Labelling them
    generate/skip/skip is what makes the button's scope readable without reading the paragraph.
    """
    source = _CONTENT.read_text("utf-8")
    for label in ("no live text · generate", "no live text · skip", "live text already · skip"):
        assert label in source, f"the figures no longer say what happens to {label!r}"


def test_the_override_is_tied_to_the_bucket_it_overrides() -> None:
    """The manual selection exists to reverse one of the three figures, and says which.

    Detached from it, the regenerate list reads as a second, unrelated feature — and an operator
    who has just been told those products are skipped has no reason to look for the control that
    un-skips them.
    """
    source = _CONTENT.read_text("utf-8")
    assert "Override" in source
    assert "skipped by default" in source


def test_one_button_writes_both_halves_of_the_run() -> None:
    """Two buttons made one intention into two runs.

    Given two, where one is obviously primary, the second gets pressed some of the time — and a
    run that writes half of what the operator meant reports success either way. The automatic set
    and the ticked set go to one command, and the union is read when the button is pressed rather
    than captured when it was built, because the ticks keep moving until then.
    """
    panel = _function("_generate_panel")
    chosen = next(
        node
        for node in ast.walk(panel)
        if isinstance(node, ast.FunctionDef) and node.name == "chosen"
    )
    names = {
        node.id for node in ast.walk(chosen) if isinstance(node, ast.Name) and node.id != "sorted"
    }
    assert {"ready", "selection"} <= names, (
        "the Generate button no longer unions the automatic set with the ticked one"
    )
    handler = next(node for node in ast.walk(panel) if isinstance(node, ast.AsyncFunctionDef))
    assert "chosen" in _calls(handler), "the handler captured a set instead of re-reading it"

    buttons = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "action"
    ]
    # `theme.action` builds every button on this screen: one to ask the site, one to write.
    assert len(buttons) == 2, f"expected two buttons on this screen; found {len(buttons)}"
