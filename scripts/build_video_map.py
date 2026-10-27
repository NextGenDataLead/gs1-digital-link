"""Draft a video name→GTIN mapping, or check its coverage, for a client (Phase 9.5 media).

Usage:
    python -m scripts.build_video_map CLIENT_ID [--products PATH]   # draft skeleton
    python -m scripts.build_video_map CLIENT_ID --check             # coverage gate

Two modes, both read-only except that ``--check`` writes the issues report:

* **draft** (default): scans the operator's per-language video folders
  (``media.video_folders``), and for every file prints a ``mapping.yml`` skeleton row with a
  blank ``gtin`` and a few ranked fuzzy *hints* from the feed. The video filenames are English
  marketing names that mostly do not appear in the feed, so the hints are only a starting point —
  a human fills the GTIN and the client signs the mapping off.

* **--check** (DoD box 1 gate): loads the client-confirmed ``media.video_map_path`` and the actual
  folder contents, reports every gap (unconfirmed / ambiguous / file-not-in-map / map-names-missing
  file), and writes ``output/{client_id}/data/video_map_issues.json`` unconditionally (empty when
  clean — a report whose absence you cannot trust is no report). Exits 1 while any gap remains.

Exit codes:
    0  draft printed, or --check found the mapping complete
    1  read error, or --check found gaps
    2  usage/config error (bad client id, no media config, --check without media.video_map_path)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, VideoMapError
from lib.media_video import (
    check_video_map,
    files_by_language,
    list_video_files,
    load_video_map,
    normalize_video_name,
    rank_candidates,
    summarize_video_map,
)
from lib.records import ProductRecord, SourceIssue

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_USAGE = 2

_HINT_COUNT = 3


def _default_products_path(client_id: str) -> Path:
    """The default parsed-products location written by ``scripts/parse_export.py``."""
    return Path("output") / client_id / "data" / "products.json"


def _issues_path(client_id: str) -> Path:
    """Where ``--check`` writes its coverage report."""
    return Path("output") / client_id / "data" / "video_map_issues.json"


def _load_products(path: Path) -> list[ProductRecord]:
    """Read the parsed-products JSON array into ``ProductRecord``s."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(item) for item in data]


def _folder_files(cfg: ClientConfig) -> dict[str, list[str]]:
    """Return ``{language: [filename]}`` for each configured video folder."""
    assert cfg.media is not None  # guarded by the caller
    return files_by_language(cfg.media.video_folders)


def _print_draft(cfg: ClientConfig, products: list[ProductRecord]) -> None:
    """Print a ``mapping.yml`` skeleton with every video file present and its GTIN unset."""
    assert cfg.media is not None
    print("# Draft video mapping — fill each gtin, then have the client sign it off.")
    print('# gtin "" is UNSET; use "skip" for a video that maps to no product.')
    print("# Hints are fuzzy feed matches only — the filenames rarely appear in the feed.")
    for language, folder in cfg.media.video_folders.items():
        print(f"{language}:")
        for path in list_video_files(Path(folder)):
            hints = rank_candidates(normalize_video_name(path.name), products, top_n=_HINT_COUNT)
            hint_text = "; ".join(f"{c.gtin} {c.name!r} ({c.field} {c.score:.2f})" for c in hints)
            print(f'  - {{file: "{path.name}", gtin: ""}}   # hints: {hint_text or "none"}')


def _run_check(cfg: ClientConfig) -> int:
    """Run the coverage gate against the confirmed mapping; write the issues report."""
    assert cfg.media is not None
    if not cfg.media.video_map_path:
        print(f"client {cfg.client_id!r} has no media.video_map_path to check", file=sys.stderr)
        return _EXIT_USAGE

    try:
        vmap = load_video_map(Path(cfg.media.video_map_path))
    except VideoMapError as exc:
        # The docstring has always promised exit 1 on a read error; until now a missing file
        # raised FileNotFoundError here and a hand-edited one a yaml traceback, both uncaught.
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    files = _folder_files(cfg)
    issues = check_video_map(vmap, files)
    _write_issues(_issues_path(cfg.client_id), issues)

    summary = summarize_video_map(vmap, files, cfg.wordpress.languages)
    print(
        f"video map: {summary.entries} row(s), {summary.files} file(s) on disk, "
        f"{summary.confirmed_gtins} GTIN(s) confirmed in every language, {summary.gaps} gap(s)",
        file=sys.stderr,
    )
    if summary.no_files_found:
        print(
            "  no video files found — the folders under media.video_folders are empty or not on "
            "this machine, so every row below reports its file as missing",
            file=sys.stderr,
        )
    for issue in issues:
        print(f"  {issue.issue} [{issue.field}] {issue.value}: {issue.detail}", file=sys.stderr)
    return _EXIT_OK if not issues else _EXIT_ERROR


def _write_issues(path: Path, issues: list[SourceIssue]) -> None:
    """Write the coverage report unconditionally (empty list when the mapping is complete)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [issue.model_dump(mode="json") for issue in issues]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_video_map",
        description="Draft or check a client's video name→GTIN mapping.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument(
        "--products", help="Parsed products JSON (default: output/{id}/data/products.json)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Coverage gate: exit 1 if any file is unmapped"
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

    if cfg.media is None:
        print(f"client {cfg.client_id!r} has no media config", file=sys.stderr)
        return _EXIT_USAGE

    if args.check:
        return _run_check(cfg)

    products_path = Path(args.products) if args.products else _default_products_path(cfg.client_id)
    try:
        products = _load_products(products_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    _print_draft(cfg, products)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
