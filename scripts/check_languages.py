"""Compare each in-scope product's languages, value by value — the input to report §4e.

Usage:
    python -m scripts.check_languages [CLIENT_ID] [--products PATH] [--dry-run]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

For every product in scope (``lib.preflight.in_scope`` — the process list, as the doctor counts
it) this sends every GS1 attribute the client maps with ``localised: true`` and that carries two
or more languages to the headless API backend, which says where the languages disagree or a value
is in the wrong language (:mod:`lib.language_check`). A product whose values are unchanged since
it was last judged is not sent again.

``scripts.run_generate --backend api`` runs the same step after it writes the copy, so the operator
shell's Generate button keeps the comparison current; this command is for running it alone.

``--dry-run`` says how many products would be judged and how many kept, and calls nothing.

Writes:  output/{client_id}/data/language_check.json
Exit codes:
    0  success
    1  some products could not be compared (each named; the rest were written)
    2  config error (bad client id, no products file, generator not enabled, missing key, API error)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from lib.config import get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, GeneratorError, LLMAPIError, MissingCredentialError
from lib.language_check import (
    load_checks,
    pending_checks,
    requests_for,
    results_path,
)
from lib.language_check_api import run_check
from lib.preflight import in_scope
from lib.records import ProductRecord

_EXIT_OK = 0
_EXIT_PARTIAL = 1
_EXIT_CONFIG_ERROR = 2


def _data_dir(client_id: str) -> Path:
    """``output/{client_id}/data`` — beside ``products.json``."""
    return Path("output") / client_id / "data"


def _load_products(path: Path) -> list[ProductRecord]:
    """Read the parsed-products JSON array into ``ProductRecord``s."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(item) for item in data]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.check_languages",
        description="Compare each in-scope product's languages for the data-quality report.",
    )
    parser.add_argument("client_id", nargs="?", default=None)
    parser.add_argument("--products", help="products.json to read (default: the parsed one)")
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would be judged; call nothing"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every product in the export, not only the product list (for the complete report)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
        data_dir = _data_dir(cfg.client_id)
        products = _load_products(
            Path(args.products) if args.products else data_dir / "products.json"
        )
        path = results_path(data_dir)
        if args.dry_run:
            scope = products if args.all else in_scope(cfg, products)
            pending = pending_checks(requests_for(cfg, scope), load_checks(path))
            print(
                f"would compare {len(pending.requests)} product(s), keep {len(pending.kept)}",
                file=sys.stderr,
            )
            return _EXIT_OK
        run = run_check(cfg, products, path, whole_export=args.all)
    except (
        ConfigError,
        GeneratorError,
        MissingCredentialError,
        LLMAPIError,
        FileNotFoundError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    print(f"compared {run.asked} product(s), kept {run.kept}; wrote {path}", file=sys.stderr)
    for gtin, reason in run.failed:
        print(f"NOT compared {gtin}: {reason}", file=sys.stderr)
    return _EXIT_PARTIAL if run.failed else _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
