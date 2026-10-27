"""The chrome every screen inherits: how it speaks, and what it may cost to draw.

Two rules that are cheap to hold and expensive to notice the loss of.

**One helper per outcome, not twenty-one calls.** The screens reached for ``ui.notify`` directly
for a long time, and drifted: four different timeouts, two spellings of the ``type`` argument, and
no dismiss button anywhere — so the failure message, the one that is the only account of why
nothing happened, was as likely as any other to have already vanished when the operator looked up.

**The rail must stay cheap.** ``theme.page`` runs on every render of every screen, so anything it
reads is read seven times a walk. ``context.rail_facts`` exists to give each step a fact, and the
tempting next fact — units with copy, checks passing — costs a ``scripts.doctor`` subprocess of
about a quarter-second. Adding one there would slow every screen in the shell to buy a number that
is already on the screen that owns it.

**The shell is the screens and the component modules, not ``ui/pages/`` alone.**
The video panels live outside their page, as the batch card did before it. A rule that stopped at
``pages/`` would be one an extraction escapes without anybody editing it, so the toast rule reads
both folders. ``ui/theme.py`` is the one exemption: it
is where the helpers that wrap ``ui.notify`` live.

AST-only, so this needs no NiceGUI and runs in the required CI job rather than the optional one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_ROOT: Final = Path(__file__).resolve().parent.parent.parent
_UI_DIR: Final = _ROOT / "ui"
_PAGES_DIR: Final = _UI_DIR / "pages"
_CONTEXT: Final = _ROOT / "ui" / "context.py"
_THEME: Final = _ROOT / "ui" / "theme.py"

#: What a screen says instead. Each owns its own duration, so how long a message stays up is a
#: property of the outcome rather than of whoever wrote the call.
_NOTIFY_HELPERS: Final = frozenset({"notify_ok", "notify_warning", "notify_problem"})

#: The screens allowed a red button. Publish writes permanent GS1 records; Setup's two saves decide
#: where and with what credentials every later run writes. Everything else has a way back.
_RED_SCREENS: Final = frozenset({"publish.py", "setup.py"})


def _shell_modules() -> list[Path]:
    """Every module that draws part of a screen: the pages and the components beside them."""
    return sorted([*_UI_DIR.glob("*.py"), *_PAGES_DIR.glob("*.py")])


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _calls(node: ast.AST) -> list[ast.Call]:
    return [inner for inner in ast.walk(node) if isinstance(inner, ast.Call)]


def _attribute_call(call: ast.Call) -> tuple[str, str] | None:
    """``theme.notify_ok(...)`` → ``("theme", "notify_ok")``; anything else → ``None``."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def test_no_screen_calls_ui_notify_directly() -> None:
    """A toast an operator can miss is a toast that did not happen."""
    offenders = [
        f"{path.relative_to(_UI_DIR)}:{call.lineno}"
        for path in _shell_modules()
        if path != _THEME
        for call in _calls(_tree(path))
        if _attribute_call(call) == ("ui", "notify")
    ]
    assert not offenders, (
        f"raw ui.notify at {offenders} — use theme.notify_ok / notify_warning / notify_problem, "
        "which own the duration and the dismiss button"
    )


def test_the_theme_offers_a_helper_for_every_outcome() -> None:
    """The rule above is only enforceable while there is somewhere to go instead."""
    defined = {
        node.name
        for node in ast.walk(_tree(_THEME))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert defined >= _NOTIFY_HELPERS, (
        f"missing from ui/theme.py: {sorted(_NOTIFY_HELPERS - defined)}"
    )


def test_the_rail_facts_run_no_subprocess() -> None:
    """Seven screens pay for whatever this function does. Keep it to ``stat``.

    Named by call rather than by import: ``ui.context`` importing ``ui.runner`` would be the
    obvious tell, but the function only has to *call* one to cost the quarter-second, and a later
    edit that adds ``subprocess`` or ``asyncio.to_thread`` directly would slip past an import
    check entirely.
    """
    functions = {
        node.name: node
        for node in ast.walk(_tree(_CONTEXT))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "rail_facts" in functions, "ui/context.py no longer defines rail_facts"

    forbidden = {
        "run",
        "run_json",
        "run_off_the_loop",
        "run_json_off_the_loop",
        "Popen",
        "to_thread",
    }
    called = set()
    for call in _calls(functions["rail_facts"]):
        pair = _attribute_call(call)
        if pair:
            called.add(pair[1])
        elif isinstance(call.func, ast.Name):
            called.add(call.func.id)

    assert not called & forbidden, (
        f"rail_facts calls {sorted(called & forbidden)} — it runs on every render of every "
        "screen, so a subprocess here is a subprocess seven times a walk. The counts that need "
        "one belong on the screen that owns them."
    )


def test_the_theme_imports_nothing_but_nicegui() -> None:
    """The facts are passed in, not fetched.

    ``theme.page`` taking a ``facts`` mapping rather than calling ``ui.context`` itself is what
    makes the check above meaningful: with the import absent there is no path from the chrome to
    a subprocess at all, whatever a later edit to ``rail_facts`` does.
    """
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(_tree(_THEME))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(_tree(_THEME))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not (imported & {"lib", "ui"}), (
        f"ui/theme.py imports {sorted(imported & {'lib', 'ui'})} — the chrome renders what it is "
        "given, so that it cannot acquire a way to read files or run commands on every page load"
    )


def test_the_theme_offers_one_way_to_hide_a_sentence_and_it_says_what_may_be_hidden() -> None:
    """``theme.explanation`` is a decision, not a wrapper, and the docstring is where it lives.

    An inlined tooltip at each call site would render the same and lose two things. First, that it
    is **both** a tooltip and a toggle: hover answers it for the operator already reaching past it,
    and the press is what makes it reachable at all on a touch screen and by keyboard, where there
    is no hover — a tooltip alone would put a screen's only account of what a file is somewhere a
    keyboard cannot go. Second, and more expensive, **what may go behind it**: text that is true
    every time and needed once. Never a warning, never a count, never anything true only today.
    The staleness band on the Data screen is exactly the thing that must not be tidied in here.
    """
    fold = next(
        (
            node
            for node in ast.walk(_tree(_THEME))
            if isinstance(node, ast.FunctionDef) and node.name == "explanation"
        ),
        None,
    )
    assert fold is not None, "ui/theme.py no longer defines explanation()"

    doc = ast.get_docstring(fold) or ""
    assert "hover" in doc and "keyboard" in doc, (
        "explanation()'s docstring must keep saying why it is both a tooltip and a toggle — "
        "hover for the operator reaching past it, the press for touch and keyboard"
    )
    assert "warning" in doc, (
        "explanation()'s docstring must keep saying what may NOT be hidden behind it. An ⓘ is "
        "discoverable at the operator's leisure, so anything they must not miss belongs in a band "
        "on the page — the Data screen's staleness warning is the case this rule is about"
    )


def test_the_info_dot_reveals_on_press_only() -> None:
    """No tooltip: text must not appear because a pointer crossed something.

    It did both for a while, and hover was the wrong half. These sit beside figures, so a hover
    reveal is a paragraph flickering over the number the operator is reading — and it appears when
    nobody asked and goes before it is read. The press works on touch, on a keyboard and with a
    pointer, so it is the only half left; the ⓘ is a real ``<button>``, so Enter and Space open it.
    """
    dot = next(
        node
        for node in ast.walk(ast.parse(_THEME.read_text("utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "_info_dot"
    )
    called = {
        node.func.attr
        for node in ast.walk(dot)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "tooltip" not in called, "the ⓘ grew a tooltip again — that is the hover half"


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def test_red_buttons_are_only_on_the_screens_that_write_what_cannot_be_undone() -> None:
    """``theme.action(..., danger=True)`` outside Publish and Setup is a colour that stops meaning.

    The docstring on :func:`ui.theme.action` said "nothing else, ever" while three other buttons
    were red, and the video panels were about to bring two more onto the Data screen — whose own
    save was deliberately taken *out* of red. A rule kept only in prose had already drifted once.
    Any ``danger=`` that is not literally ``False`` counts: Publish passes an expression, and an
    expression can be true.
    """
    offenders = [
        f"{path.relative_to(_UI_DIR)}:{call.lineno}"
        for path in _shell_modules()
        if path.name not in _RED_SCREENS
        for call in _calls(_tree(path))
        if _attribute_call(call) == ("theme", "action")
        for keyword in call.keywords
        if keyword.arg == "danger" and not _is_false(keyword.value)
    ]
    assert not offenders, (
        f"red button at {offenders} — red is for writes that are hard or impossible to undo, which "
        f"only {sorted(_RED_SCREENS)} make. A local file with a .bak or an undo stays blue; see "
        "the docstring on ui.theme.action"
    )
