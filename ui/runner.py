"""Run the project's CLIs as subprocesses and stream what they say.

The single seam between the shell and the pipeline. Everything the UI causes to happen goes
through here, which is what makes the claim "the UI runs exactly the command a human would"
checkable rather than aspirational: there is one place to look.

Why a subprocess rather than an import — see :mod:`ui`. The short version is that ``load_env()``
lives in each script's ``__main__`` block on purpose, so an in-process call would have no
credentials, and calling ``load_env()`` here would arm the staging guards inside a long-lived
process. Subprocessing also inherits the production guard, the ``state.json`` writes, the run
JSONL and the ``--only links`` refusal, all of which live in ``scripts/``.

``sys.executable`` is the interpreter, so the child runs in the same virtualenv. This is also why
PyInstaller is ruled out for packaging: bundling makes ``sys.executable`` the bundled app rather
than an interpreter, and ``-m scripts.…`` stops working.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lib.gates import Mode
from ui import DATA_ROOT

#: Exit codes are uniform across all the scripts (docs/troubleshooting.md).
EXIT_OK: Final = 0
EXIT_HAD_ERRORS: Final = 1
EXIT_CONFIG_ERROR: Final = 2

#: How long to let a single CLI run before giving up. Generous: a real publish of a few dozen
#: products makes hundreds of HTTP calls against someone else's WordPress, with retries.
DEFAULT_TIMEOUT_SECONDS: Final = 60 * 30


@dataclass(frozen=True)
class CommandResult:
    """What a finished subprocess reported.

    ``stderr`` matters more than ``stdout`` here: every script in this project writes its human
    summary to stderr and reserves stdout for machine output. A caller that reads only stdout
    gets silence from a run that said plenty.
    """

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether every row succeeded. Exit 1 means the run happened and some rows failed."""
        return self.returncode == EXIT_OK

    @property
    def config_error(self) -> bool:
        """Whether the run never started — bad config, missing credentials, refused production."""
        return self.returncode == EXIT_CONFIG_ERROR

    @property
    def display_command(self) -> str:
        """The command as a person would type it, for showing beside the output.

        Shown on every screen that runs something. An operator who can see the command can
        reproduce it in a terminal, ask someone about it, or check it against the docs — none of
        which is possible if the UI only reports what it decided to say about the result.
        """
        return " ".join(["python", *self.argv])


def _child_env() -> dict[str, str]:
    """The environment a CLI child gets.

    Passed through unchanged but for two variables. ``PYTHONUNBUFFERED``, so streamed output
    arrives line by line instead of in a block at the end — the whole point of streaming it.
    ``PYTHONUTF8``, so the child writes UTF-8 to its pipes on every platform: on Windows a pipe
    otherwise gets the ANSI code page, and the first "→" a report prints is an
    ``UnicodeEncodeError`` in the child rather than a line on the screen.

    Deliberately **not** loaded from ``.env`` here. The child does that itself, in its own
    ``__main__`` block, which is the arrangement ``tests/lib/test_env.py`` enforces.
    """
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}


def run(argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> CommandResult:
    """Run a CLI to completion and return everything it said. Blocking.

    For the quick, offline commands — ``doctor``, ``run_plan``, ``report_quality`` — where there
    is nothing to watch and the answer is the point.
    """
    completed = subprocess.run(  # noqa: S603 — argv is built in code, never from operator text
        [sys.executable, *argv],
        cwd=DATA_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        argv=list(argv),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


async def stream(
    argv: Sequence[str],
    on_line: Callable[[str], None],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run a CLI, calling ``on_line`` for each line as it arrives, and return the result.

    For ``run_execute``, which is the only command long enough to need watching. Both streams are
    merged in arrival order rather than kept apart, because the interleaving is the information:
    a warning that lands between two rows belongs between them.

    The child is killed if it outlives ``timeout``, and what it had already said is returned — a
    hung run that reports nothing is worse than a hung run that reports how far it got.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *argv,
        cwd=DATA_ROOT,
        env=_child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []

    async def pump() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            # "\r\n" from a Windows child: a bare "\n" strip leaves every line ending in "\r".
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(line)
            on_line(line)

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        returncode = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        message = f"[shell] timed out after {timeout:.0f}s and was stopped"
        lines.append(message)
        on_line(message)
        returncode = EXIT_HAD_ERRORS

    return CommandResult(argv=list(argv), returncode=returncode, stdout="\n".join(lines), stderr="")


def run_json(argv: Sequence[str]) -> tuple[Any, CommandResult]:
    """Run a CLI that emits JSON on stdout and parse it.

    Returns ``(None, result)`` rather than raising when the output does not parse, so a caller can
    show the raw text. A crashed command is still a command that said something, and hiding what
    it said behind a parse error is how a UI becomes less useful than a terminal.
    """
    result = run(argv)
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


async def run_json_off_the_loop(argv: Sequence[str]) -> tuple[Any, CommandResult]:
    """:func:`run_json`, in a worker thread, so the page can repaint while it runs.

    :func:`run` is a blocking ``subprocess.run``. Called straight from a click handler it holds
    the event loop for the whole command, so **every UI change queued before it never reaches the
    browser** — including the one that says the command is running. The screen then looks
    identical from click to result, which reads as a button that does nothing. On the Preflight
    screen, whose whole purpose is "click this and work down the list", that was the worst place
    for it.

    A thread rather than an async subprocess because the blocking call is already written, tested
    and shared with the synchronous callers; ``to_thread`` releases the loop without a second
    implementation of the same thing.
    """
    return await asyncio.to_thread(run_json, argv)


async def run_off_the_loop(
    argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> CommandResult:
    """:func:`run`, in a worker thread, for the same reason :func:`run_json_off_the_loop` exists.

    That one was written for the Preflight screen and adopted nowhere else, so six buttons across
    five screens went on calling :func:`run` straight from a click handler — which holds the event
    loop for the whole command. The screen then looks identical from click to result, and a button
    that shows nothing is a button an operator clicks again. This is the other half of the fix in
    :func:`ui.theme._while_running`: without it the spinner that guard adds is queued behind the
    very command it is meant to be reporting on, and never paints.

    :func:`stream` is still the right choice where the output is worth watching line by line. This
    is for the commands that only have an answer.
    """
    return await asyncio.to_thread(run, argv, timeout=timeout)


# --- The commands, named ------------------------------------------------------
#
# One function per pipeline step, so no screen builds an argv inline. The publish argv is the
# exception: it comes from lib.gates.run_execute_argv, because it is part of the gate contract
# rather than a convenience.


def doctor_argv(
    client_id: str | None, *, offline: bool = False, mode: Mode | None = None
) -> list[str]:
    """The preflight, as JSON — for what the batch publishes, when that is known."""
    argv = ["-m", "scripts.doctor", "--json"]
    if client_id:
        argv.append(client_id)
    if offline:
        argv.append("--offline")
    if mode is not None:
        argv += ["--mode", mode.value]
    return argv


def parse_export_argv(client_id: str | None, *, dry_run: bool = False) -> list[str]:
    """Workbook → ``products.json``."""
    argv = ["-m", "scripts.parse_export"]
    if client_id:
        argv.append(client_id)
    if dry_run:
        argv.append("--dry-run")
    return argv


def run_plan_argv(
    client_id: str | None, *, include_published: bool = False, links_only: bool = False
) -> list[str]:
    """Classify every unit against prior state.

    ``include_published`` re-admits GTINs that are already published *and* resolvable, which
    ``run_plan`` otherwise drops as finished. It defaults to off here for the same reason it does
    on the CLI: with it on, a CHANGED row rewrites a live page, and that has to be chosen.

    ``links_only`` builds the plan for a ``links`` run: products with no generated copy stay in
    it, since no page is rendered. ``run_execute`` refuses such a plan in any other mode.
    """
    argv = ["-m", "scripts.run_plan", *([client_id] if client_id else [])]
    if include_published:
        argv.append("--include-published")
    if links_only:
        argv.append("--links-only")
    return argv


def report_live_copy_argv(client_id: str | None) -> list[str]:
    """Ask the **site** which products have no tagline or Eigenschappen yet.

    The one read in this shell that does not come from a local file. It is slow by nature — the
    field values are only truthful when each page is fetched by id, so it is one request per page
    plus a listing per language — which is exactly why it is a button the operator presses rather
    than something that runs on every page load.
    """
    return ["-m", "scripts.report_live_copy", *([client_id] if client_id else []), "--json"]


def run_generate_argv(client_id: str | None, gtins: list[str] | None = None) -> list[str]:
    """Write this run's copy through the Anthropic Messages API.

    The only command here that reaches a third party, and the only one that needs an API key. It
    is a subprocess like every other, so the key is read from ``.env`` by the child's ``__main__``
    block and never enters this process — the arrangement ``tests/lib/test_env.py`` enforces, and
    the reason the shell can offer generation without holding a credential.

    Only ``--backend api`` is offered. ``--emit``/``--validate`` are the in-session producer's
    half of the seam, answered by a Claude Code session rather than by this screen, and the API
    backend writes and re-reads the results file itself — so there is no separate validate step to
    forget.

    ``gtins`` names the products to write for, which is how this screen drives it: the selection
    comes from what the *site* is missing, or from rows the operator ticked, and neither is a
    question ``state.json`` can answer. Omitted, the command falls back to its ledger-derived
    selection. An **empty list is not omission** — it would build a command that regenerates the
    whole batch — so it is refused here rather than sent.
    """
    if gtins is not None and not gtins:
        raise ValueError("refusing to build a generate command for an empty GTIN selection")
    return [
        "-m",
        "scripts.run_generate",
        *([client_id] if client_id else []),
        "--backend",
        "api",
        *(["--gtins", ",".join(gtins)] if gtins else []),
    ]


def report_quality_argv(client_id: str | None, *, complete: bool = False) -> list[str]:
    """Render every issue file into one worklist — for the batch in progress, or with ``complete``
    for every product on the client's list and everything live, checked against the site."""
    return [
        "-m",
        "scripts.report_quality",
        *([client_id] if client_id else []),
        *(["--complete"] if complete else []),
    ]


def report_scope_argv(client_id: str | None, *, run: str | None = None) -> list[str]:
    """The per-run result sheet: the operator's scope list with what the run did to each row.

    ``run`` is passed explicitly rather than left to default, because the default is "the newest
    log" and the button that calls this sits on a *particular* run's card. Two runs a second apart
    are ``{ts}.jsonl`` and ``{ts}-1.jsonl``, and a report silently written for the other one would
    be indistinguishable from the right one until somebody read a page URL that was never visited.
    """
    argv = ["-m", "scripts.report_scope_result", *([client_id] if client_id else [])]
    if run:
        argv += ["--run", run]
    return argv


def build_video_map_argv(client_id: str | None, *, check: bool = True) -> list[str]:
    """The video-mapping coverage gate.

    Only the ``--check`` half is offered here. Draft mode prints a fresh skeleton to stdout, and
    a screen that could re-draft the mapping would be a screen that can discard client sign-off —
    so drafting stays a terminal job, where redirecting the output is a deliberate act.
    """
    argv = ["-m", "scripts.build_video_map", *([client_id] if client_id else [])]
    if check:
        argv.append("--check")
    return argv


def reconcile_argv(client_id: str | None) -> list[str]:
    """Compare the live site against ``state.json``.

    Always ``--json``: the screen renders the findings itself, and a report an operator has to
    read out of a console is a report they will not read. Read-only either way — the script only
    issues GETs, and reads state without quarantining a corrupt one.
    """
    return ["-m", "scripts.reconcile", *([client_id] if client_id else []), "--json"]


def hand_over(path: Path, *, downloads: Path | None = None, open_it: bool = True) -> Path:
    """Copy a report into the operator's Downloads folder and open it in their default app.

    Operator, 2026-10-09: a downloaded PDF opened in the Python application. The shell runs in a
    native window, where a browser download has no browser to land in — so the file is put where
    people look for downloads and handed to the operating system, which opens a PDF in the PDF
    reader and a workbook in the spreadsheet program, as a double-click would.

    Returns where the copy is. Raises ``OSError`` when it cannot be copied; opening is best-effort
    — the file is there either way, and the caller says where.
    """
    folder = downloads or Path.home() / "Downloads"
    if not folder.is_dir():
        folder = Path.home()
    target = folder / path.name
    counter = 1
    while target.exists():
        target = folder / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    shutil.copyfile(path, target)
    # A handover pack is not opened: the operating system would unpack the zip into Downloads,
    # passwords and all, which is the one thing the person was not asked to do.
    if open_it:
        open_in_default_app(target)
    return target


def open_in_default_app(path: Path) -> None:
    """Ask the operating system to open ``path`` with whatever it opens that kind of file with."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 — a file we just wrote
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603, S607 — fixed argv, our own file
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607 — as above
    except OSError:
        return
