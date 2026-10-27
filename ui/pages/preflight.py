"""Screen 4 — the preflight, as a list to work down.

Runs ``python -m scripts.doctor --json`` in a subprocess and renders what it says. The checks are
not reimplemented here; a second implementation would be a second thing to keep true.

Subprocessing is what makes the credential checks work at all: ``load_env()`` runs in the
script's ``__main__`` block, so this process holds no secrets and the child resolves them itself.

**The whole preflight runs by itself on arrival, with no buttons** (operator, 2026-10-07: "run
both automatically and present a loading animation … This also eliminates the need for buttons").
It used to run only the offline half on arrival and keep the credentials half behind a button,
on the argument that landing on a screen should not log in anywhere. With the steps opening only
through Next (:mod:`ui.progress`), arriving here *is* the deliberate act — and the offline result
on screen read as an old one, waiting to be replaced. Every check is read-only. Each visit runs it
afresh; nothing from an earlier run is shown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from nicegui import ui

from lib.gates import Mode
from ui import context, progress, runner, theme

#: The four statuses, only for tallying here — the rendering of a check lives in the theme, so
#: this screen and the Setup screen's Test buttons cannot start showing the same check differently.
_STATUSES = ("ok", "warn", "fail", "n/a")

#: The checks that only an ``--offline``-less run performs — see ``lib.preflight.run_checks``,
#: which returns before appending these three. They exist nowhere else in the batch:
#: ``run_execute`` sets ``resolved_gs1 = None`` for a dry run, so a dry run never mints a token.
_CREDENTIAL_CHECKS: Final = frozenset({"site_serves", "wordpress", "gs1"})


def render() -> None:
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Preflight",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Preflight"),
            "Preflight",
            "Everything that can be checked before anything is written — so a missing secret or "
            "a stale copy cache surfaces now, not after live pages exist.",
        )

        def show(payload: Any, result: runner.CommandResult) -> None:
            results.clear()
            with results:
                if payload is None:
                    theme.band("The preflight did not return readable results.", "danger")
                    ui.label(result.stderr or result.stdout or "(no output)").classes("console")
                    gate(None)
                    return
                _summary(payload)
                gate(payload)
                for check in payload:
                    theme.check_row(
                        str(check["status"]),
                        str(check["title"]),
                        str(check["detail"]),
                        str(check.get("remedy") or ""),
                    )
            status.text = f"{_finished_at()} · exit {result.returncode} · {result.display_command}"

        # Async, and the subprocess runs off the event loop: a blocking call holds the loop until
        # it has finished, so the animation below would never paint.
        async def go() -> None:
            busy.set_visibility(True)
            seconds = 0

            def tick() -> None:
                nonlocal seconds
                seconds += 1
                elapsed.text = f"{seconds}s"

            clock = ui.timer(1.0, tick)
            try:
                payload, result = await runner.run_json_off_the_loop(
                    runner.doctor_argv(cid, offline=False, mode=mode)
                )
            finally:
                clock.cancel()
                busy.set_visibility(False)
            show(payload, result)

        mode = context.batch_mode(cfg)
        if mode is Mode.LINKS:
            theme.band(
                "This batch publishes GS1 links only, so the checks that only a page needs — its "
                "text, categories, videos — are marked not applicable."
            )
        ui.label(
            "Checks the settings, this batch and its text, then logs in to WordPress and asks GS1 "
            "for a token. All of it is read-only — nothing is written, and the GS1 request is a "
            "GET against a barcode from your own catalogue."
        ).classes("note mt-4")

        busy, elapsed = _busy_row()

        status = ui.label("").classes("note mt-4")
        results = ui.column().classes("w-full gap-0")

        def onward() -> None:
            if cid is not None:
                progress.of(cid).advance("/preflight")
            ui.navigate.to("/publish")

        def gate(payload: list[dict[str, Any]] | None) -> None:
            """Next opens Publish only once the run came back with no failure."""
            failing = payload is None or any(check["status"] == "fail" for check in payload)
            next_button.set_enabled(not failing)
            caption.text = (
                "Next opens Publish once the checks above show no failure. Fix what failed, then "
                "open this screen again to re-run them."
                if failing
                else "Next goes on to Publish, where a dry run comes before anything is written."
            )

        next_button, caption = theme.onward("Next", onward)
        next_button.disable()
        caption.text = "Next opens Publish once the checks have run and show no failure."

        # On arrival, every time: each visit is a fresh run, and nothing earlier is shown.
        ui.timer(0, go, once=True)


def _busy_row() -> tuple[ui.row, ui.label]:
    """The loading state — a spinner, what is happening, and the seconds so far — hidden until a
    run starts. The counter is what tells a slow login apart from a screen that stopped."""
    with ui.row().classes("items-center gap-3 mt-6") as busy:
        ui.spinner(size="1.6em")
        ui.label("Running the checks…").classes("note")
        elapsed = ui.label("0s").classes("note mono")
    busy.set_visibility(False)
    return busy, elapsed


def _finished_at() -> str:
    """When this run finished, in UTC.

    The screen runs on load, so a re-run of a healthy machine repaints an identical list — which
    is indistinguishable from a button that did nothing. This is the part that always differs.
    """
    return datetime.now(UTC).strftime("%H:%M:%S UTC")


def _verdict(payload: list[dict[str, Any]]) -> tuple[str, str]:
    """The sentence at the top of the screen, and the band kind to render it as.

    **"Ready." used to be said having tested no credential.** The screen ran only the offline
    checks on arrival, and offline stops before WordPress, GS1 and the target URL — so the one state
    the operator most needs qualified was the one that read as an unqualified all-clear. The screen
    now runs the full set, so the caveat below fires only if the credential checks are missing from
    the payload anyway. A wrong
    password then survives every gate, because the dry run does not authenticate either, and
    surfaces at the first real write with some rows already live.

    The caveat rides on the verdict rather than in a band beneath it: the verdict is the line that
    gets read, and a second band is the one that gets skimmed.

    A *failure* is not qualified. When something offline is already broken the credential question
    is not yet the operator's problem, and diluting "Not ready" would cost more than it buys.
    """
    tally = {key: sum(1 for c in payload if c["status"] == key) for key in _STATUSES}
    if tally["fail"]:
        return "Not ready. Fix the failures below before publishing.", "danger"

    verdict, kind = (
        ("Ready, but read the warnings below first.", "warn")
        if tally["warn"]
        else ("Ready.", "quiet")
    )
    tested = _CREDENTIAL_CHECKS & {str(check.get("name", "")) for check in payload}
    if not tested:
        verdict += " Offline checks only — no credential was tested."
    return verdict, kind


def _summary(payload: list[dict[str, Any]]) -> None:
    """The verdict first, so the list below is read as detail rather than as news."""
    tally = {key: sum(1 for c in payload if c["status"] == key) for key in _STATUSES}
    with ui.row().classes("gap-12 mb-6"):
        theme.figure(str(tally["ok"]), "passed")
        if tally["warn"]:
            theme.figure(str(tally["warn"]), "warnings")
        if tally["fail"]:
            theme.figure(str(tally["fail"]), "failures")
        if tally["n/a"]:
            theme.figure(str(tally["n/a"]), "not applicable")

    theme.band(*_verdict(payload))
