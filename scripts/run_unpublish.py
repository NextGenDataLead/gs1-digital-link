"""Take a published product back down: retract its Digital Link, draft its pages.

Usage:
    python -m scripts.run_unpublish CLIENT_ID --gtin GTIN [--gtin GTIN ...] [--dry-run]

The inverse of ``run_execute`` for a product that should no longer be public. Per GTIN,
in this order:

1. **Retract the GS1 resolver record** — ``retract`` disables the record via
   ``activationStatus`` and deliberately leaves its ``links`` in place.
2. **Draft every language's WordPress page** — guarded by ``meta.gtin``, so a stale page
   id in state cannot draft somebody else's content.
3. **Record it in state** — ``wp_status``/``gs1_enabled``, so the next run knows the
   product is down on purpose.

**The order is the point.** A drafted page is not publicly reachable, so drafting first
would leave an enabled Digital Link resolving to a 404 for as long as the window lasts —
every scanned QR in that window hitting a dead end. Retracting first degrades to "QR does
nothing", which is the intended end state anyway.

Not implemented as ``post_status: draft`` in ``clients.yml`` plus a re-run, which is the
obvious move and does not work: ``run_execute`` verifies each page with an
*unauthenticated* HEAD, a draft answers 404, the row raises, and the GTIN is blocked —
leaving the pages drafted, state unwritten and the resolver still enabled. That is the
half-done state this script exists to avoid.

Idempotent: a GTIN already retracted and drafted is a no-op. Deliberately takes explicit
``--gtin`` values rather than a plan — unpublishing is a decision about named products,
and there is no plan artifact that means "take these down".

``--dry-run`` logs the intended mutations and touches nothing.

Exit codes:
    0  every GTIN was taken down (or already was)
    1  one or more GTINs errored (state still saved for those that succeeded)
    2  config/setup error (bad client id, missing GS1 creds, unknown GTIN)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from pydantic import ValidationError

from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, StateError
from lib.gs1_dl_client import GS1Config as ResolvedGS1Config
from lib.gs1_dl_client import GS1DigitalLinkClient
from lib.records import State
from lib.state import load_state, save_state
from lib.wp_client import WordPressClient

_log = logging.getLogger("scripts.run_unpublish")

_EXIT_OK = 0
_EXIT_ERRORS = 1
_EXIT_CONFIG_ERROR = 2

#: The post status an unpublished product's pages are moved to. Draft rather than trash:
#: trashing appends ``__trashed`` to the slug, which would forfeit ``p-{gtin}`` — the slug
#: the resolver's target URL is built from — and make a later re-publish land elsewhere.
_DOWN_STATUS = "draft"


def _unpublish_gtin(
    cfg: ClientConfig, gtin: str, wp: WordPressClient, gs1: GS1DigitalLinkClient, state: State
) -> bool:
    """Take one GTIN down; return whether it fully succeeded.

    GS1 first, then the pages — see the module docstring. State is updated per language as
    each page is drafted, so a failure part-way leaves an accurate record of how far it
    got rather than an all-or-nothing lie.
    """
    entries = state.entries.get(gtin, {})
    retracted = gs1.retract(gtin)
    _log.info(
        "GS1 %s: %s", gtin, "retracted" if retracted else "no resolver record (nothing to retract)"
    )

    ok = True
    for language, entry in sorted(entries.items()):
        try:
            page = wp.set_page_status(
                cfg.wordpress.post_type, entry.wp_page_id, gtin=gtin, status=_DOWN_STATUS
            )
        except Exception as exc:  # noqa: BLE001 — one language's failure must not strand the rest
            _log.error("unpublish %s/%s failed: %r", gtin, language, exc)
            ok = False
            continue
        if page is None:
            _log.warning(
                "unpublish %s/%s: page %d is already gone", gtin, language, entry.wp_page_id
            )
        state.entries[gtin][language] = entry.model_copy(
            update={
                "wp_status": _DOWN_STATUS if page is not None else entry.wp_status,
                "retracted": True,
                # Retraction *deletes the links* — it clears the array and disables the
                # record — so the hash of "the link set we wrote" is no longer true of
                # anything. Blanking it is what makes `lib.state._has_no_resolver_link`
                # fire if this GTIN is ever revived: `--revive --only pages` carries the
                # prior hash forward, so a stale one made the revived row classify
                # UNCHANGED and the resolver record was never written back.
                "gs1_link_set_hash": "",
                "last_run": datetime.now(UTC),
            }
        )
    return ok


def _preview_gtin(cfg: ClientConfig, gtin: str, state: State) -> None:
    """Log what :func:`_unpublish_gtin` would do, without doing it."""
    entries = state.entries.get(gtin, {})
    _log.info(
        "[dry-run] %s: would retract the GS1 resolver record, then draft %s",
        gtin,
        ", ".join(
            f"{cfg.wordpress.post_type}/{entry.wp_page_id} ({lang})"
            for lang, entry in sorted(entries.items())
        )
        or "no pages (none in state)",
    )


def _unknown_gtins(gtins: list[str], state: State) -> list[str]:
    """Return the requested GTINs state has no pages for.

    A hard error rather than a warning: without state there are no page ids, so the
    WordPress half cannot run and a "success" would mean a retracted resolver still
    pointing at live pages — the exact silent half-done outcome this script avoids.
    """
    return [gtin for gtin in gtins if not state.entries.get(gtin)]


def _run(
    cfg: ClientConfig, gtins: list[str], resolved_gs1: ResolvedGS1Config | None, *, dry_run: bool
) -> int:
    """Take the GTINs down (or preview it); return the process exit code."""
    state = load_state(cfg.client_id)
    unknown = _unknown_gtins(gtins, state)
    if unknown:
        print(
            f"no state for GTIN(s) {', '.join(unknown)} — nothing known to unpublish",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_ERROR

    if dry_run or resolved_gs1 is None:
        for gtin in gtins:
            _preview_gtin(cfg, gtin, state)
        print(f"[dry-run] {len(gtins)} GTIN(s), 0 error(s)", file=sys.stderr)
        return _EXIT_OK

    with (
        WordPressClient(cfg.wordpress) as wp,
        GS1DigitalLinkClient(resolved_gs1) as gs1,
    ):
        results = [_unpublish_gtin(cfg, gtin, wp, gs1, state) for gtin in gtins]
    save_state(state)

    errors = results.count(False)
    print(f"{len(gtins)} GTIN(s), {errors} error(s)", file=sys.stderr)
    return _EXIT_ERRORS if errors else _EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_unpublish",
        description="Retract a product's Digital Link and draft its WordPress pages.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument(
        "--gtin",
        action="append",
        required=True,
        metavar="GTIN",
        help="GTIN to unpublish; repeat for more than one",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview intended mutations without performing them"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
        resolved_gs1 = None if args.dry_run else cfg.gs1.resolve()
    except (ConfigError, StateError, ValidationError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    return _run(cfg, args.gtin, resolved_gs1, dry_run=args.dry_run)


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
