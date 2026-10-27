"""Emit the per-run result sheet: the operator's scope list with what the run did to each row.

Usage:
    python -m scripts.report_scope_result [CLIENT_ID] [--run PATH] [--plan PATH] [--no-plan]
                                          [--list PATH] [--products PATH] [--out PATH]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

Read-only, and written **after** a run rather than during one. It is a report, not a control file:
nothing here decides what a later run does, which is the whole difference from the design where the
scope list grows a status column and the run reads it back. See ``lib/process_list.py`` for what
that cost the last time it was tried.

**Every run now writes this sheet itself**, through :func:`lib.result_sheet.build` — which is why
the assembly lives in ``lib`` and this module is the flags, the exit codes and the summary on
stderr. This remains the way to rebuild one for any run on demand, or to point it at a different
list, plan or products file.

Three sheets, one workbook:

* ``scope``   — one row per SKU: the operator's own columns, then ``in_scope``, ``result``, and
                ``status``/``page``/``detail`` per language.
* ``units``   — one row per (GTIN, language) from the run log and the plan's holds, uninterpreted.
                Where "nl published, fr failed" survives.
* ``legend``  — what each value means, so the file can be forwarded without a covering email.

``--run`` defaults to the newest log in ``output/{client_id}/runs`` **by modification time, not by
name**: a same-second second run is written as ``{ts}-1.jsonl``, which sorts *before* ``{ts}.jsonl``
because ``-`` precedes ``.``.

Rows come from the list the run kept beside its own log — ``selection-uploaded.xlsx`` for the
superset and ``selection-used.xlsx`` for the ticks — so a row the operator deselected is reported as
deselected rather than being missing from their own report. A legacy run kept neither; that falls
back to the files in ``input/`` and **says so**, because those may belong to a later batch.

Emits: output/{client_id}/runs/{stamp}/result.xlsx
Exit codes:
    0  report written
    1  the run log, the scope list or the products file could not be read
    2  config/usage error (bad client id, no process_list block, no run log to report on)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from lib.config import get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, ProcessListError
from lib.result_sheet import Sheets, build
from lib.run_files import newest_log

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_USAGE = 2


def _newest_run(client_id: str) -> Path | None:
    """The most recent run log, by mtime. See the module docstring for why not by name."""
    try:
        return newest_log(client_id)
    except OSError:
        return None


def _report(rows: list[Any], unreadable: int, sheets: Sheets, plan_used: bool) -> None:
    """Say what was counted, on stderr, naming every reason a number is lower than it looks."""
    results = Counter(row.result for row in rows)
    tally = ", ".join(f"{count} {name}" for name, count in sorted(results.items()))
    print(f"{len(rows)} SKU(s): {tally}", file=sys.stderr)
    if unreadable:
        print(
            f"  {unreadable} line(s) of the run log did not parse and are not in this report — "
            "usually a run killed mid-write",
            file=sys.stderr,
        )
    if not sheets.from_run:
        # Named separately from ``archived`` because they are different problems with the same
        # symptom: one report is about the wrong rows, the other is about too few of them.
        print(
            "  this run kept no copy of the list it consumed, so the rows come from the files in "
            "input/ as they are now — which a later batch may have replaced",
            file=sys.stderr,
        )
    if not sheets.archived:
        print(
            "  no uploaded list to compare against, so the rows reported are the ones that ran; "
            "any row deselected before the run cannot be named",
            file=sys.stderr,
        )
    if not plan_used:
        print(
            "  no plan read, so a unit the plan held reads `not run` rather than `held` — pass "
            "--plan to point at the plan this run was executed from",
            file=sys.stderr,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="report_scope_result",
        description="Emit the per-run result sheet for a client's scope list.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument("--run", help="Run log (default: the newest output/{id}/runs/*.jsonl)")
    parser.add_argument("--plan", help="Plan to read holds from (default: output/{id}/plan.json)")
    parser.add_argument(
        "--no-plan",
        action="store_true",
        help="Do not read a plan; held units then read `not run`",
    )
    parser.add_argument("--list", help="Scope list to report on (default: the client's, uploaded)")
    parser.add_argument(
        "--products", help="Parsed products JSON (default: output/{id}/data/products.json)"
    )
    parser.add_argument(
        "--out", help="Output path (default: beside the run log, {stem}-scope.xlsx)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_USAGE

    if cfg.process_list is None:
        print(
            f"client {cfg.client_id!r} has no process_list block — there is no scope list to "
            f"report on",
            file=sys.stderr,
        )
        return _EXIT_USAGE

    run_path = Path(args.run) if args.run else _newest_run(cfg.client_id)
    if run_path is None:
        print(
            f"no run log under output/{cfg.client_id}/runs — there is nothing to report on yet",
            file=sys.stderr,
        )
        return _EXIT_USAGE

    try:
        built = build(
            cfg,
            run_path,
            listed=args.list,
            plan_path=args.plan,
            no_plan=args.no_plan,
            products_path=args.products,
            out=args.out,
            warn=lambda line: print(line, file=sys.stderr),
        )
    except (OSError, json.JSONDecodeError, ValueError, ProcessListError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    print(f"Wrote {built.out} for {run_path.name}", file=sys.stderr)
    _report(built.rows, built.unreadable, built.sheets, built.plan_used)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
