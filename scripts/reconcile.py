"""Compare the live site against ``state.json`` and report where they disagree.

Usage:
    python -m scripts.reconcile [CLIENT_ID] [--json]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

``state.json`` is what every run classifies from, and nothing has ever checked it against the
site. The divergence is not hypothetical and does not need anyone to have edited anything by
hand: **a run that fails part-way creates it.** One product published in Dutch and failed on
French; sibling-blocking correctly held the product, so the row was logged as an error and
nothing was written to state — while the Dutch page was live and publicly reachable. Ten entries
in the ledger, eleven tool-made pages on the site, and no way to find out.

This lists every page carrying a ``meta.gtin`` per configured language, diffs it against the
ledger in both directions, and prints what it finds.

**Read-only, and deliberately so.** Only ``GET`` requests, and it reads state through
:func:`lib.state.peek_state` rather than ``load_state`` — the latter *quarantines* a corrupt
file (E19), which would make an idle diagnostic change what the next run does. It reports and
never repairs: each divergence has more than one correct resolution, and choosing needs someone
who knows which machine published last.

Exit codes:
    0  the site and the ledger agree
    1  at least one divergence — a finding, not a crash; the report is on stdout
    2  usage/config error, or the site could not be read
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, MissingCredentialError, StateError, WordPressAPIError
from lib.reconcile import LivePage, Report, reconcile
from lib.state import peek_state
from lib.wp_client import WordPressClient

_EXIT_OK = 0
_EXIT_FINDINGS = 1
_EXIT_ERROR = 2

#: Leading marker per divergence kind. ASCII, so it survives a Windows console and a log file.
_MARKER = "[diff]"
_INDENT = " " * 7


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reconcile",
        description="Compare live WordPress pages against state.json. Read-only.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Client id from clients.yml; optional when exactly one is defined",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the report as JSON, for the operator shell"
    )
    return parser.parse_args(argv)


def _fetch(cfg: ClientConfig) -> list[LivePage]:
    """Every tool-made page on the site, per configured language.

    Each language is asked for explicitly. On a WPML site an unscoped query answers with the
    default language only, so a reconciliation that skipped this would report every translated
    page as missing from the site — the loudest possible false alarm.
    """
    found: list[LivePage] = []
    with WordPressClient(cfg.wordpress) as client:
        for language in cfg.wordpress.languages:
            for page in client.list_pages_with_gtin(cfg.wordpress.post_type, language):
                meta = page.get("meta")
                gtin = str(meta.get("gtin", "")) if isinstance(meta, dict) else ""
                found.append(
                    LivePage(
                        gtin=gtin,
                        language=language,
                        page_id=int(page.get("id", 0)),
                        slug=str(page.get("slug", "")),
                        status=str(page.get("status", "")),
                        url=str(page.get("link", "")),
                    )
                )
    return found


def _render(report: Report) -> None:
    """Print the report as a list to work down, findings first."""
    print(report.summary, file=sys.stderr)
    if report.agrees:
        return
    print(file=sys.stderr)
    for finding in report.findings:
        print(
            f"{_MARKER} {finding.gtin} {finding.language} — {finding.kind.value}", file=sys.stderr
        )
        print(f"{_INDENT}{finding.detail}", file=sys.stderr)
        print(f"{_INDENT}→ {finding.explanation}", file=sys.stderr)


def _as_json(report: Report) -> dict[str, Any]:
    """The report as JSON, for the operator shell to render."""
    return {
        "summary": report.summary,
        "agrees": report.agrees,
        "languages": report.languages,
        "live_pages": report.live_pages,
        "state_entries": report.state_entries,
        "findings": [
            {**dataclasses.asdict(finding), "explanation": finding.explanation}
            for finding in report.findings
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
        state = peek_state(cfg.client_id)
        live = _fetch(cfg)
    except (ConfigError, StateError, MissingCredentialError, WordPressAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    report = reconcile(live, state, cfg.wordpress.languages)
    if args.json:
        print(json.dumps(_as_json(report), indent=2))
    else:
        _render(report)
    return _EXIT_OK if report.agrees else _EXIT_FINDINGS


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
