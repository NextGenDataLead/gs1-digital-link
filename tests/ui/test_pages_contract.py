"""The screens, against the NiceGUI they will actually run on.

Every other test under ``tests/ui/`` either parses source with :mod:`ast` or imports one of the
NiceGUI-free modules (``ui.session``, ``ui.config_edit``, ``ui.runner`` …). That is deliberate —
it is what lets them run in the required CI job, which installs ``.[dev]`` to prove ``lib`` never
grows a UI dependency. The cost was that **no test imported a screen at all**, so ``ui/pages/``
was the one surface CI never touched, and an unbounded ``nicegui>=2.0`` broke two screens without
anything going red (#52, and the pattern behind #53 and #54).

This module is the other side of that split. It skips wholesale where NiceGUI is absent and runs
in the ``Operator shell (ui extra)`` job, where it asserts the two things an AST check cannot:
that every screen still *imports* against the installed NiceGUI, and that the routes and the left
rail still agree about which screens exist.

It is not a rendering test. It cannot tell you a screen looks right — only that a version bump
has not made one unreachable or unimportable, which is the failure that actually happened.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from nicegui import app as nicegui_app  # noqa: E402

from ui import theme  # noqa: E402

#: Every screen module. Listed by hand rather than globbed: a screen that stops being imported
#: here should be a deliberate deletion, not a file that quietly dropped out of a pattern.
PAGE_MODULES: Final = (
    "ui.pages.setup",
    "ui.pages.preflight",
    "ui.pages.data",
    "ui.pages.content",
    "ui.pages.publish",
    "ui.pages.runs",
    # ``ui.pages.video_map`` was deleted on purpose, not dropped from a pattern: the video mapping
    # moved onto the Data screen as two panels (see PANEL_MODULES), and a surviving route would be
    # a second live editor of one client-sign-off file with no knowledge of the first's session.
)

#: Every component module — a part of a screen that lives outside ``ui/pages/`` so that more than
#: one screen, or one long screen, can render it. Listed by hand for the same reason as the
#: screens; the first such module was never import-checked until this list existed.
PANEL_MODULES: Final = (
    "ui.video_map_panel",
    "ui.video_signoff_panel",
)

#: Routes that are registered but deliberately absent from the rail, with how they are reached.
#: A screen added without either an entry here or a rail entry is unreachable, which is the point
#: of checking both directions.
#:
#: ``/videos`` lived here for as long as the rail was a single numbered list of six — an entry
#: would have numbered a detour as a step of the run. Splitting the rail into the batch and the
#: tools gave it somewhere honest to sit, so it moved; later it folded into the Data screen and the
#: route went altogether. The next screen that is genuinely reachable only from another one still
#: needs a home, and it should be this rather than a quiet exemption.
UNLISTED_ROUTES: Final[dict[str, str]] = {
    "/": "where the window opens; redirects to /data, since step 1 is where a batch starts",
}

#: NiceGUI's own machinery, not ours. ``/favicon.ico`` is registered when the app starts, so it
#: appears once any test in the session has run the app's lifespan (the Data screen smoke test).
_INTERNAL_PREFIXES: Final = ("/_nicegui", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

#: A hardcoded step eyebrow — what every screen used to pass to ``theme.heading``.
_STEP_LITERAL: Final = re.compile(r"Step \d+")

#: Where each screen's source lives, for the AST check below.
_PAGES_DIR: Final = Path(__file__).resolve().parent.parent.parent / "ui" / "pages"
_MODULE_PATHS: Final = {name: _PAGES_DIR / f"{name.split('.')[-1]}.py" for name in PAGE_MODULES}


def _our_routes() -> set[str]:
    """Every path this application registers, minus NiceGUI's internals."""
    importlib.import_module("ui.app")
    paths = {path for route in nicegui_app.routes if (path := getattr(route, "path", None))}
    return {p for p in paths if not p.startswith(_INTERNAL_PREFIXES)}


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_every_screen_imports(module: str) -> None:
    """A screen that cannot be imported cannot be opened, and only the terminal would say so."""
    importlib.import_module(module)


@pytest.mark.parametrize("module", PANEL_MODULES)
def test_every_panel_imports(module: str) -> None:
    """A panel that cannot be imported takes every screen that renders it down with it."""
    importlib.import_module(module)


def test_the_application_module_imports() -> None:
    """``ui.app`` wires the routes; importing it is what registers them.

    Safe headless: ``pywebview`` is only reached inside ``main()``, which this never calls.
    """
    importlib.import_module("ui.app")


def test_the_theme_installs() -> None:
    """``theme.install()`` runs once at startup, so a broken call would surface only there."""
    theme.install()


def test_every_rail_entry_has_a_route() -> None:
    """A rail entry with no route is a dead link on the one navigation the shell has."""
    routes = _our_routes()
    for screen in theme.NAV:
        assert screen.route in routes, (
            f"rail entry {screen.label!r} points at {screen.route}, which has no route"
        )


def test_every_route_is_reachable() -> None:
    """The other direction: a screen with no way to reach it is a screen nobody will use."""
    listed = {screen.route for screen in theme.NAV} | set(UNLISTED_ROUTES)
    orphans = _our_routes() - listed
    assert not orphans, (
        f"registered but unreachable: {sorted(orphans)} — add a rail entry to WAVE or TOOLS in "
        "ui/theme.py, or record how it is reached in UNLISTED_ROUTES here"
    )


# --- The numbering is an assertion, so it has one source ----------------------


def test_the_rail_is_numbered_in_order_from_one() -> None:
    """The numbers are the workflow. A gap or a repeat makes them decoration."""
    assert [screen.eyebrow for screen in theme.WAVE] == [
        f"Step {n}" for n in range(1, len(theme.WAVE) + 1)
    ]


def test_the_batch_is_the_four_screens_an_operator_repeats() -> None:
    """Setup and Runs are not steps of a run, and numbering them alongside four that are said so.

    Setup is configured once and left alone; Runs is read afterwards. While all six were numbered
    1-6 the rail asserted they were one sequence, which buried the loop an operator actually
    repeats between machine configuration at one end and history at the other.

    Asserted rather than left to the reviewer, because the failure mode is a later screen being
    appended to ``WAVE`` because that is where the other entries are — which renumbers the batch
    silently and puts a "Step 5" eyebrow on something nobody runs per batch.
    """
    assert [screen.label for screen in theme.WAVE] == ["Data", "Content", "Preflight", "Publish"]
    assert [screen.label for screen in theme.TOOLS] == ["Setup", "Runs"]
    assert not any(screen.eyebrow.startswith("Step") for screen in theme.TOOLS)


def test_preflight_comes_after_the_screens_it_depends_on() -> None:
    """Four of the doctor's checks answer "Run `parse_export` first" — which is the Data screen.

    Preflight sat ahead of them for a long time, so it told an operator to go and do a later step
    and come back, and on a fresh machine most of the list could not answer its own questions
    yet. This is the ordering, asserted rather than remembered.
    """
    order = [screen.label for screen in theme.WAVE]

    assert order.index("Preflight") > order.index("Data")
    assert order.index("Preflight") > order.index("Content")
    assert order.index("Preflight") < order.index("Publish")


def test_no_screen_spells_its_own_step_number() -> None:
    """``theme.eyebrow`` reads the rail, so a reorder is one edit rather than seven.

    They *were* seven: the rail carried the numbers and every screen also hardcoded its own into
    ``theme.heading``. Two lists that had to be renumbered together, with nothing to notice when
    only one was.
    """
    for module in PAGE_MODULES:
        path = _MODULE_PATHS[module]
        for node in ast.walk(ast.parse(path.read_text("utf-8"), filename=str(path))):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            assert not _STEP_LITERAL.fullmatch(node.value), (
                f"{path.name}:{node.lineno} spells {node.value!r} — use "
                "theme.eyebrow('<rail label>') so the rail stays the only place the order "
                "is written"
            )


def _step_numbers(path: Path) -> list[int]:
    """Every literal ``step=N`` a screen passes — to ``theme.section`` or to a panel that renders
    its own section, as the Data screen's video sign-off does."""
    numbers = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "step" and isinstance(keyword.value, ast.Constant):
                numbers.append(int(keyword.value.value))
    return numbers


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_a_screens_numbered_steps_run_one_to_n_with_no_gap(module: str) -> None:
    """``step=4`` is an int, so the check above — which looks for "Step 3" strings — cannot see it.

    The Data screen was renumbered when the selection list became step 1 and the videos step 3;
    a renumber that skipped a number, or gave two sections the same one, would read to the
    operator as a missing step. Alternatives for one step (a client with no ``process_list``
    renders a different section under the same number) count once.
    """
    numbers = sorted(set(_step_numbers(_MODULE_PATHS[module])))
    assert numbers == list(range(1, len(numbers) + 1)), f"{module} numbers its steps {numbers}"


def test_the_operator_guide_names_the_fixed_settings_block_as_the_screen_does() -> None:
    """The guide tells the operator what that folded block is; it must use the heading on screen.

    The heading used to be "Settled elsewhere", which an operator read as "not in the handover
    pack". A guide that kept the old name after a rename would send them looking for it.
    """
    setup = importlib.import_module("ui.pages.setup")
    guide = (Path(__file__).parents[2] / "docs" / "operator-guide.md").read_text(encoding="utf-8")

    assert re.sub(r"\s+", " ", setup.FIXED_TITLE) in re.sub(r"\s+", " ", guide)
    assert "elsewhere" not in setup.FIXED_TITLE.lower()
