"""Draft a ``categories`` block, or check its coverage, for a client (Phase 7.5).

Usage:
    python -m scripts.build_brick_map CLIENT_ID --datamodel PATH \
        --code-column COL --category-column COL [--products PATH]
    python -m scripts.build_brick_map CLIENT_ID --check [--products PATH]

Two modes, both read-only (they print; they never write files):

* **draft** (default): reads the parsed products, lists every distinct GPC brick in the
  export, and prints a ``categories:`` skeleton with each brick's term left UNSET for a
  person to fill. With ``--datamodel`` it annotates each brick with its GS1 DIY sector label;
  the datamodel supplies the *sector*, not the client's site term, so mapping label → term
  and the client sign-off stay a human step. The operator pastes the filled block into
  ``clients.yml`` and has the client review it.

* **--check**: the coverage gate (DoD #2). Reads the client's committed ``categories`` config
  and reports which export bricks resolve to a term. Exits 1 when any brick is unmapped.

    --products:   default output/{client_id}/data/products.json

Exit codes:
    0  draft printed, or --check found every brick mapped
    1  read error, or --check found unmapped bricks
    2  usage/config error (bad client id, missing --datamodel columns, no categories config)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.categories import (
    coverage_report,
    distinct_bricks,
    draft_brick_map,
    load_diy_datamodel,
)
from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, ExportParseError
from lib.records import ProductRecord

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_USAGE = 2


def _default_products_path(client_id: str) -> Path:
    """The default parsed-products location written by ``scripts/parse_export.py``."""
    return Path("output") / client_id / "data" / "products.json"


def _load_products(path: Path) -> list[ProductRecord]:
    """Read the parsed-products JSON array into ``ProductRecord``s."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(item) for item in data]


def _print_draft(
    products: list[ProductRecord], datamodel: dict[str, str] | None, terms: list[str]
) -> None:
    """Print a ``categories:`` skeleton with every brick present and UNSET."""
    bricks = distinct_bricks(products)
    draft = draft_brick_map(bricks, products, datamodel)

    terms_line = ", ".join(terms) if terms else "# FILL IN your category terms"
    print("# Draft categories block — fill each term, then have the client sign it off.")
    print('# Terms below are UNSET (""): a brick can span categories, so never guess.')
    print("categories:")
    print(f"  terms: [{terms_line}]")
    print("  brick_category_map:")
    for brick, term in draft.entries.items():
        print(f'    "{brick}": "{term}"   # {draft.annotations[brick]}')
    print("  overrides: {}   # per-GTIN, for bricks that span categories")

    print(f"\n# {len(bricks)} distinct bricks across {len(products)} products.", file=sys.stderr)
    if datamodel is not None and draft.unannotated:
        print(
            f"# {len(draft.unannotated)} brick(s) absent from the DIY datamodel "
            f"(no sector label): {', '.join(draft.unannotated)}",
            file=sys.stderr,
        )


def _run_check(cfg: ClientConfig, products: list[ProductRecord]) -> int:
    """Run the coverage gate against the client's committed categories config."""
    if cfg.categories is None:
        print(f"client {cfg.client_id!r} has no categories config to check", file=sys.stderr)
        return _EXIT_USAGE
    report = coverage_report(products, cfg.categories)
    print(
        f"{report.total_bricks} bricks: {len(report.mapped)} mapped, "
        f"{len(report.override_only)} override-only, {len(report.unmapped)} unmapped",
        file=sys.stderr,
    )
    for brick, gtins in report.unmapped.items():
        print(f"  UNMAPPED {brick}: {len(gtins)} product(s) — {', '.join(gtins)}", file=sys.stderr)
    return _EXIT_OK if report.is_complete else _EXIT_ERROR


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_brick_map", description="Draft or check a client's GPC brick → category map."
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument("--datamodel", help="Path to the GS1 DIY sector datamodel (.xlsx)")
    parser.add_argument("--code-column", help="Datamodel column holding the GPC brick code")
    parser.add_argument("--category-column", help="Datamodel column holding the DIY sector label")
    parser.add_argument("--sheet", help="Datamodel worksheet to read (default: scan all sheets)")
    parser.add_argument(
        "--products", help="Parsed products JSON (default: output/{id}/data/products.json)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Coverage gate: exit 1 if any brick is unmapped"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
        products_path = (
            Path(args.products) if args.products else _default_products_path(cfg.client_id)
        )
        products = _load_products(products_path)
    except (ConfigError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_USAGE if isinstance(exc, ConfigError) else _EXIT_ERROR

    if args.check:
        return _run_check(cfg, products)

    datamodel: dict[str, str] | None = None
    if args.datamodel:
        if not (args.code_column and args.category_column):
            print("--datamodel requires --code-column and --category-column", file=sys.stderr)
            return _EXIT_USAGE
        try:
            datamodel = load_diy_datamodel(
                args.datamodel,
                code_column=args.code_column,
                category_column=args.category_column,
                sheet=args.sheet,
            )
        except ExportParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_ERROR

    terms = cfg.categories.terms if cfg.categories else []
    _print_draft(products, datamodel, terms)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
