"""Put pages this tool already published into their category — and change nothing else.

Usage:
    python -m scripts.set_categories [CLIENT_ID] [--gtin GTIN ...] [--apply]

Until 2026-10-08 ``run_execute`` never set a page's category: ``wordpress.taxonomies`` was
configured and never written, so none of the live pages showed on a category page. New and
changed pages get it now. This is for the ones already live, which a pages run would not reach
without rewriting them: their content hash is unchanged, so they classify UNCHANGED, and forcing
them CHANGED would generate new copy and change their live wording.

For every live page in the ledger (published, not retracted) it works out the product's category
the way the plan does (:func:`lib.categories.assign_categories`), finds that term in the page's own
language (WPML keeps ``keuken-fr`` beside ``keuken``), reads what the page has now, and — only with
``--apply`` — sets the term. Nothing but the taxonomy field is sent, so title, text, images, video
and status are untouched, and every page is checked to carry its product's ``meta.gtin`` first.

**A dry run is the default** and writes nothing; it reads the site, so it needs the WordPress
credentials too. Reversible: a category can be changed or removed in WordPress. Writes no state and
nothing in GS1.

Exit codes:
    0  every page has its category (or would, in a dry run)
    1  one or more pages could not be given theirs — each is named
    2  config/setup error (bad client id, no parsed products, no category taxonomy configured)
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from lib.categories import assign_categories
from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, OrchestratorError, StateError
from lib.live_inventory import live_products
from lib.preflight import load_products
from lib.state import peek_state
from lib.wp_client import WordPressClient

_EXIT_OK = 0
_EXIT_ERRORS = 1
_EXIT_CONFIG_ERROR = 2

#: The product field a category taxonomy is filled from (``wordpress.taxonomies``).
_CATEGORY_FIELD = "category"


@dataclass(frozen=True)
class _Page:
    """One live page and the category it should be in."""

    gtin: str
    language: str
    page_id: int
    url: str
    category: str | None


def _category_taxonomy(cfg: ClientConfig) -> str | None:
    """The taxonomy filled from the product's category, or ``None`` when none is configured."""
    return next(
        (
            name
            for name, source in cfg.wordpress.taxonomies.items()
            if source.map_from_column == _CATEGORY_FIELD
        ),
        None,
    )


def _live_pages(cfg: ClientConfig, gtins: set[str] | None) -> list[_Page]:
    """Every live page in the ledger, with its product's category; ``gtins`` narrows it."""
    products, _ = assign_categories(cfg.categories, load_products(cfg.client_id))
    category = {product.gtin14: product.category for product in products}
    state = peek_state(cfg.client_id)
    pages = []
    for product in live_products(state):
        if gtins is not None and product.gtin not in gtins:
            continue
        for page in product.pages:
            entry = state.entries[product.gtin][page.language]
            pages.append(
                _Page(
                    product.gtin,
                    page.language,
                    entry.wp_page_id,
                    page.url,
                    category.get(product.gtin),
                )
            )
    return pages


def _settle(  # noqa: PLR0911 — one return per answer the operator needs to tell apart
    cfg: ClientConfig, wp: WordPressClient, taxonomy: str, page: _Page, *, apply: bool
) -> tuple[str, bool]:
    """Bring one page into its category (or say what that would do). Returns (line, ok)."""
    where = f"{page.gtin}/{page.language} {page.url}"
    if page.category is None:
        return f"skip   {where}: the product has no category (its GPC brick is unmapped)", True
    try:
        term = wp.term_id(taxonomy, page.category, page.language)
        if term is None:
            return (
                f"ERROR  {where}: the site has no {page.category!r} term in {page.language} — "
                "create it there, or map the product to one that exists",
                False,
            )
        current = wp.page_terms(cfg.wordpress.post_type, page.page_id, page.gtin, taxonomy)
        if current == [term]:
            return f"ok     {where}: already in {page.category!r}", True
        verb = "set   " if apply else "would "
        if apply:
            wp.set_page_terms(cfg.wordpress.post_type, page.page_id, page.gtin, {taxonomy: [term]})
        return f"{verb} {where}: {current or 'no category'} → {page.category!r} ({term})", True
    except OrchestratorError as exc:  # a missing or foreign page, or a failed call: this page only
        return f"ERROR  {where}: {exc}", False


def _run(cfg: ClientConfig, gtins: set[str] | None, *, apply: bool) -> int:
    taxonomy = _category_taxonomy(cfg)
    if taxonomy is None:
        print(
            f"config error: no wordpress.taxonomies entry maps from {_CATEGORY_FIELD!r}",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR
    if not load_products(cfg.client_id):
        print(
            f"config error: no parsed products for {cfg.client_id} — run parse_export first",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR
    pages = _live_pages(cfg, gtins)

    errors = 0
    with WordPressClient(cfg.wordpress) as wp:
        for page in pages:
            line, ok = _settle(cfg, wp, taxonomy, page, apply=apply)
            errors += not ok
            print(line)
    mode = "" if apply else "[dry-run] "
    print(f"{mode}{len(pages)} live page(s), {errors} error(s)", file=sys.stderr)
    return _EXIT_ERRORS if errors else _EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="set_categories",
        description="Put live pages into their category, changing nothing else (dry run unless "
        "--apply).",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument(
        "--gtin", action="append", metavar="GTIN", help="Only this product; repeat for more"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the categories (default: only say what would)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
    except (ConfigError, StateError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    gtins = {gtin.zfill(14) for gtin in args.gtin} if args.gtin else None
    return _run(cfg, gtins, apply=args.apply)


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
