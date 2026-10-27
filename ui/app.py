"""Routing and startup for the operator shell.

Bound to ``127.0.0.1`` and opened in a native window rather than a browser tab. The window is not
cosmetic: this process reaches a live WordPress site and the GS1 production resolver, and a
listening socket on a workstation is a thing IT will ask about. One that answers only to loopback
and has no visible URL to share is a much shorter conversation.

``reload=False`` because the reloader re-executes the module, and a shell that restarts itself
mid-run would orphan a subprocess that is writing permanent records.
"""

from __future__ import annotations

from typing import Final

from fastapi.responses import RedirectResponse
from nicegui import app, ui

from ui import context, handover, progress, theme
from ui.pages import content, data, preflight, publish, runs, setup

#: Loopback only, and a port unlikely to collide with a dev server the operator also runs.
HOST: Final = "127.0.0.1"
PORT: Final = 8477

#: Inside a container, loopback is the container's own and unreachable from the machine, so the
#: shell listens on the container's interfaces. Loopback-only moves to the port mapping instead:
#: ``compose.yml`` publishes ``127.0.0.1:8477``, and ``tests/test_container.py`` holds it there.
CONTAINER_HOST: Final = "0.0.0.0"  # noqa: S104 — reachable only through the loopback port mapping

TITLE: Final = "GS1 Digital Link — operator shell"


@ui.page("/")
def _start() -> RedirectResponse:
    """The window opens here. Step 1 is where a batch starts, so that is where it lands —
    Setup is set up once and rarely touched, and opening on it put a form in front of every
    session (operator feedback, 2026-10-07)."""
    return RedirectResponse("/data")


@ui.page("/setup")
def _setup() -> None:
    setup.render()


def _step(route: str) -> RedirectResponse | None:
    """Where to send a request for a step this session has not reached — see :mod:`ui.progress`.

    Not applied when the config will not load: every screen then says so and points at the fix,
    and a lock would bounce the operator away from the one message that helps.
    """
    cid = context.client_id()
    if context.client_config(cid) is None:
        return None
    elsewhere = progress.of(cid).arrive(route)
    return RedirectResponse(elsewhere) if elsewhere else None


@ui.page("/preflight")
def _preflight() -> RedirectResponse | None:
    if redirect := _step("/preflight"):
        return redirect
    preflight.render()
    return None


@ui.page("/data")
def _data() -> RedirectResponse | None:
    if redirect := _step("/data"):
        return redirect
    data.render()
    return None


@ui.page("/content")
def _content() -> RedirectResponse | None:
    if redirect := _step("/content"):
        return redirect
    content.render()
    return None


@ui.page("/publish")
def _publish() -> RedirectResponse | None:
    if redirect := _step("/publish"):
        return redirect
    publish.render()
    return None


@ui.page("/runs")
def _runs() -> None:
    runs.render()


def main(*, native: bool = True, container: bool = False) -> None:
    """Start the shell.

    ``native=False`` serves it in a browser instead, for a machine with no webview available —
    still on loopback, and still the same pages.

    ``container=True`` is browser mode inside the container image: it listens on
    :data:`CONTAINER_HOST` and opens no browser, because there is none in there — the operator
    opens ``http://127.0.0.1:8477`` on their own machine.
    """
    native = native and not container
    handover.deliver_through_browser(not native)
    theme.install()
    # pywebview disables text selection by default; the operator copies barcodes, URLs and
    # error text out of this window.
    app.native.window_args["text_select"] = True
    ui.run(
        host=CONTAINER_HOST if container else HOST,
        port=PORT,
        title=TITLE,
        native=native,
        reload=False,
        show=not native and not container,
        favicon="🔗",
        dark=None,  # follow the operating system rather than impose a mood
    )
