"""A button must show that it is working, and it must not freeze the page while it does.

Both halves of one incident. Publishing twenty rows from the shell produced **two identical
complete runs**: ``run_execute`` prints one line when it starts and one when it finishes, so the
console sat silent for about ninety seconds, nothing disabled the button, and the operator
reasonably concluded it had not worked and clicked again. Twenty live pages were rewritten twice.
The same second click in ``links`` or ``both`` mode is aimed at records that can never be deleted.

The fix is in two places because the defect was:

* ``ui/theme.py`` disables the button and shows a spinner and the elapsed seconds — there rather
  than at the two dozen call sites, so a screen written later inherits it without an edit;
* the screens run their subprocess **off the event loop**, because a blocking ``subprocess.run``
  in a click handler holds the loop until the command has already finished. Every UI change queued
  before it — including the one saying the command is running — then reaches the browser with
  nothing left to report, and the screen looks identical from click to result.

Neither half works alone, which is why both are checked here. The handler rule reads the component
modules beside ``ui/pages/`` as well as the pages themselves: a panel's button is a screen's button,
and a rule that stopped at ``pages/`` would be escaped by the first extraction that moved a handler
out of a page — the video panels are the case.

AST-only, so this needs no NiceGUI and runs in the required CI job rather than the optional one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_ROOT: Final = Path(__file__).resolve().parent.parent.parent
_THEME: Final = _ROOT / "ui" / "theme.py"
_UI_DIR: Final = _ROOT / "ui"
_PAGES_DIR: Final = _UI_DIR / "pages"

#: The two helpers every screen builds its buttons with.
_BUTTONS: Final = frozenset({"action", "quiet_action"})

#: The runner calls that hold the event loop. Their off-the-loop twins are
#: ``run_off_the_loop`` and ``run_json_off_the_loop``.
_BLOCKING: Final = frozenset({"run", "run_json"})


def _shell_modules() -> list[Path]:
    """Every module that draws part of a screen: the pages and the components beside them."""
    return sorted([*_UI_DIR.glob("*.py"), *_PAGES_DIR.glob("*.py")])


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _functions(tree: ast.AST) -> dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every ``def`` in a module by name, nested ones included — handlers are closures."""
    found: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.setdefault(node.name, []).append(node)
    return found


def _called_names(node: ast.AST) -> set[str]:
    """Every function or method name called anywhere inside ``node``."""
    return {
        name
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        for name in (getattr(inner.func, "id", None) or getattr(inner.func, "attr", None),)
        if name
    }


def _handler_names(argument: ast.expr) -> set[str]:
    """The function names a click-handler argument could resolve to.

    Three shapes are in use: a plain name (``theme.action("…", save)``), a lambda that forwards to
    one (``lambda: parse(dry_run=True)``, ``lambda: self._decide(row, applied=True)``), and a
    dotted callable from elsewhere (``ui.navigate.reload``), which resolves to nothing local and
    is simply not checked.
    """
    if isinstance(argument, ast.Name):
        return {argument.id}
    if isinstance(argument, ast.Lambda):
        return _called_names(argument.body)
    return set()


def _click_handlers(tree: ast.Module) -> set[str]:
    """Every handler named as the second argument to ``theme.action``/``theme.quiet_action``."""
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) in _BUTTONS
        and len(node.args) > 1
        for name in _handler_names(node.args[1])
    }


def test_no_click_handler_blocks_the_event_loop() -> None:
    """A sync handler that runs a subprocess freezes the page it is meant to be reporting on.

    ``run_json_off_the_loop`` was written for this and adopted on exactly one screen; six buttons
    across five others went on calling the blocking form straight from a click. Derived from the
    code rather than kept as a list of known offenders: a list goes stale in both directions, and
    a button added next month is precisely the case it would miss.
    """
    offenders = []
    for path in _shell_modules():
        tree = _tree(path)
        functions = _functions(tree)
        for name in sorted(_click_handlers(tree)):
            for handler in functions.get(name, []):
                if _BLOCKING & _called_names(handler) and not isinstance(
                    handler, ast.AsyncFunctionDef
                ):
                    offenders.append(f"{path.relative_to(_UI_DIR)}:{handler.lineno} {name}")

    assert not offenders, (
        f"these click handlers block the event loop: {offenders}. Make each one `async def` and "
        "await `runner.run_off_the_loop` / `runner.run_json_off_the_loop`, or the spinner "
        "ui/theme.py adds is queued behind the very command it is reporting on and never paints"
    )


def test_both_button_helpers_route_through_the_guard() -> None:
    """One guard, two helpers. A helper that skipped it would be a whole class of unguarded button.

    ``quiet_action`` matters as much as ``action`` here: *Check the parse*, *Run offline checks*
    and *Test WordPress* are all outlined, and all of them run a subprocess.
    """
    functions = _functions(_tree(_THEME))
    for name in sorted(_BUTTONS):
        assert "_while_running" in _called_names(functions[name][0]), (
            f"theme.{name} does not guard its button, so every screen's {name} buttons can be "
            "clicked a second time while the first click is still running"
        )


def test_the_guard_disables_the_button_and_restores_it_whatever_happened() -> None:
    """Restored in a ``finally``: a handler that raises must not leave the button dead.

    The screens raise for ordinary reasons — a refused gate, an unreadable file — and a button
    that stays disabled after one of those takes the whole screen with it, on a shell whose only
    recovery is reloading the page and answering every gate again.
    """
    guard = _functions(_tree(_THEME))["_while_running"][0]
    assert "disable" in _called_names(guard)

    finallys = [node.finalbody for node in ast.walk(guard) if isinstance(node, ast.Try)]
    assert finallys, "_while_running has no `finally`, so a raising handler leaves a dead button"
    restored = {name for body in finallys for stmt in body for name in _called_names(stmt)}
    assert {"enable", "deactivate"} <= restored, (
        f"the guard's `finally` does not restore the button and stop the clock; it calls {restored}"
    )
