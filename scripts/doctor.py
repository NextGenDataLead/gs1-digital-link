"""Check that a client is ready to publish, before anything irreversible is attempted.

Usage:
    python -m scripts.doctor [CLIENT_ID] [--offline] [--json] [--config PATH]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

Runs every check in :mod:`lib.preflight` and prints them as a list an operator can work
down. The checks themselves are pure and live there; this script is the argument parsing,
the ``.env`` load, and the rendering.

The point is *when* it runs. Credentials were previously resolved lazily, so a
:class:`~lib.errors.MissingCredentialError` fired at the first API call — an operator could
complete parse, plan and a clean dry-run before discovering a secret was missing. Coverage gaps
were worse: a generated-copy cache that no longer matched the export produced no error at all,
just units quietly absent from the plan (E21).

``--offline`` stops before any check that reads a credential or opens a socket, which is also
what makes it safe to run anywhere. Everything it skips is reported as skipped rather than
silently dropped — a report that shows fewer lines without saying so is the failure mode this
tool exists to prevent.

Nothing here writes anything the pipeline reads, and nothing calls ``load_state``: an idle peek
at a corrupt state file *quarantines* it (E19), and a diagnostic must not change what the next
run does.

Exit codes:
    0  every applicable check passed (warnings do not fail the run)
    1  at least one check failed — this is a finding, not a crash; the report is on stdout
    2  usage error (unreadable --config path is a *finding*, not a usage error)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.gates import Mode
from lib.preflight import CheckResult, Status, run_checks, worst_status

_EXIT_OK = 0
_EXIT_FINDINGS = 1

#: Leading marker per status. ASCII, so it survives a Windows console and a log file.
_MARKER = {
    Status.OK: "[ ok ]",
    Status.WARN: "[warn]",
    Status.FAIL: "[FAIL]",
    Status.NA: "[ -- ]",
}

#: Indent for the continuation lines under a check, aligned past the marker.
_INDENT = " " * 7


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doctor",
        description="Check that a client is ready to publish, before anything is written.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run only the checks that need no credentials and no network",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the results as JSON instead of prose"
    )
    parser.add_argument("--config", help="Path to clients.yml (default: the data folder's own)")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in Mode],
        help="What the batch publishes; 'links' skips the checks only a page needs",
    )
    return parser.parse_args(argv)


def _render(results: list[CheckResult]) -> str:
    """Render the report an operator reads: one block per check, remedies included."""
    lines: list[str] = []
    for result in results:
        lines.append(f"{_MARKER[result.status]} {result.title}")
        lines.append(f"{_INDENT}{result.detail}")
        if result.remedy:
            lines.append(f"{_INDENT}→ {result.remedy}")
        lines.append("")
    lines.append(_verdict(results))
    return "\n".join(lines)


def _verdict(results: list[CheckResult]) -> str:
    """The closing line: what passed, what did not, and whether it is safe to proceed.

    Skipped checks are counted out loud. A report that quietly shows fewer lines reads as a
    clean bill of health for things nobody looked at.
    """
    tally = {status: sum(1 for r in results if r.status is status) for status in Status}
    parts = [f"{tally[Status.OK]} ok"]
    if tally[Status.WARN]:
        parts.append(f"{tally[Status.WARN]} warning(s)")
    if tally[Status.FAIL]:
        parts.append(f"{tally[Status.FAIL]} failure(s)")
    if tally[Status.NA]:
        parts.append(f"{tally[Status.NA]} not applicable")
    summary = ", ".join(parts)

    if tally[Status.FAIL]:
        return f"{summary} — not ready. Fix the failures above before publishing."
    if tally[Status.WARN]:
        return f"{summary} — ready, but read the warnings first."
    return f"{summary} — ready."


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    kwargs: dict[str, object] = {"offline": args.offline}
    if args.config:
        kwargs["config_path"] = args.config
    if args.mode:
        kwargs["mode"] = Mode(args.mode)
    results = run_checks(args.client_id, **kwargs)  # type: ignore[arg-type]

    if args.json:
        payload = [dataclasses.asdict(result) for result in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(_render(results))
        if args.offline:
            print(
                "\n(--offline: the WordPress, GS1 and site-reachability checks did not run.)",
                file=sys.stderr,
            )

    return _EXIT_FINDINGS if worst_status(results) is Status.FAIL else _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
