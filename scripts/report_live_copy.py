"""Ask the live site which products still have no tagline or Eigenschappen text.

Usage:
    python -m scripts.report_live_copy [CLIENT_ID] [--json]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

Every other "does this need copy?" in this pipeline is answered from ``state.json``. That ledger
records what *this machine* wrote, and it does not travel: two machines publish, two ledgers
diverge, and neither can say what is on the site. This asks the site.

For each product on the process list it reports one of four states:

* **has text** — live in every configured language, with every generated field populated;
* **needs text** — a page missing, or live with a blank generated field, and the export carries
  something to write copy from;
* **no inputs** — the same, but the export carries neither attr 1083 nor attr 1067, so nothing on
  this machine can supply it. That is a MyGS1 worklist, not a Generate button;
* **held** — the plan will hold it (missing mandatory data, the video gate, no image), so the site
  is not consulted for it. These are the rows the Data screen shows without a tick box; the saved
  selection keeps them only so a run can name them, and they must not read as chosen here.

What it deliberately does **not** answer is whether the inputs *changed* since the live text was
written. That needs a fingerprint of the published unit, which is exactly the ledger this exists
to stop depending on — so it is the operator's call, made by picking products to regenerate.

**Two requests per language to list, then one per page to read.** The listing is language-scoped
and its ``acf`` payload is empty on a WPML site (measured: all thirteen French pages read as
textless while rendering their text correctly), so every page is re-read by id with no ``lang``
parameter. That is the only read that tells the truth — see :meth:`lib.wp_client.read_page`.

**Read-only.** GETs and nothing else, and it reads no state at all.

Emits (--json): the report on stdout, for the operator shell to parse.
Exit codes:
    0  the site was read
    2  config error, or the site could not be read
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from lib.config import ClientConfig, get_client, resolve_client_id
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import (
    ConfigError,
    ExportParseError,
    MissingCredentialError,
    VideoMapError,
    WordPressAPIError,
)
from lib.holds import held_products
from lib.live_copy import LiveCopyReport, LiveText, classify
from lib.preflight import in_scope
from lib.records import ProductRecord
from lib.site_read import read_tool_pages
from lib.wp_client import WordPressClient

_EXIT_OK = 0
_EXIT_ERROR = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="report_live_copy",
        description="Which products have no tagline or Eigenschappen text on the live site.",
    )
    parser.add_argument("client_id", nargs="?", help="Key under clients: in clients.yml")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON on stdout")
    return parser.parse_args(argv)


def _load_products(cfg: ClientConfig) -> list[ProductRecord]:
    """The parsed export, from the path every other script reads it from."""
    path = Path("output") / cfg.client_id / "data" / "products.json"
    if not path.is_file():
        raise ConfigError(f"{path} does not exist — run `python -m scripts.parse_export` first")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(entry) for entry in raw]


def _fetch(cfg: ClientConfig) -> dict[tuple[str, str], LiveText]:
    """Every tool-made page, with its generated fields read the one way that works.

    The listing is asked per language because an unscoped query on a WPML site answers with the
    default language only. The *fields* are then read by id with no language parameter, because a
    scoped read answers ``acf: []`` for a page that has them — which is how thirteen correct
    French pages came to look empty.
    """
    fields = tuple(cfg.wordpress.acf_map)
    with WordPressClient(cfg.wordpress) as client:
        pages = read_tool_pages(client, cfg.wordpress, fields)
    return {
        (gtin, language): LiveText(
            gtin=gtin,
            language=language,
            page_id=page.page_id,
            filled=page.filled,
            empty=tuple(f for f in fields if f not in page.filled),
        )
        for (gtin, language), page in pages.items()
    }


def _as_json(cfg: ClientConfig, report: LiveCopyReport, checked_at: str) -> dict[str, object]:
    return {
        "client_id": cfg.client_id,
        "checked_at": checked_at,
        "site_url": cfg.wordpress.site_url,
        "languages": list(report.languages),
        "fields": list(report.fields),
        "counts": report.counts,
        "products": [
            {
                "gtin": product.gtin,
                "name": product.name,
                "bucket": product.bucket.value,
                "missing_languages": list(product.missing_languages),
                "absent_languages": list(product.absent_languages),
            }
            for product in report.products
        ],
    }


def _render(report: LiveCopyReport) -> None:
    counts = report.counts
    # Worded by what happens to each group, and in that order, because that is what the reader
    # is deciding. The shell says the same three things in the same three words.
    print(
        f"{counts['in_scope']} product(s) in scope: {counts['needs_text']} to process (no live "
        f"text, input available), {counts['no_inputs']} skipped (no live text, input "
        f"unavailable), {counts['has_text']} skipped (live text already), {counts['held']} held "
        "(not eligible)."
    )
    for product in report.needs_text:
        where = ", ".join(product.missing_languages)
        print(f"  [process] {product.gtin} {product.name} — no live text in {where}")
    for product in report.no_inputs:
        print(f"  [skip] {product.gtin} {product.name} — no attr 1083 or 1067 in the export")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = get_client(resolve_client_id(args.client_id))
        products = in_scope(cfg, _load_products(cfg))
        # The Data screen's own verdict, so "eligible" cannot mean one thing there and another here.
        holds = held_products(cfg, products)
        held = {product.gtin14 for product in products if product.gtin in holds}
        live = _fetch(cfg)
    except (
        ConfigError,
        ExportParseError,
        MissingCredentialError,
        VideoMapError,
        WordPressAPIError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    report = classify(products, live, cfg.wordpress.languages, held=held)
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    if args.json:
        print(json.dumps(_as_json(cfg, report, checked_at), indent=2))
    else:
        _render(report)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
