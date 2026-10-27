"""Hand one client's data folder — and with it the ledger — to another machine.

Usage:
    python -m scripts.handover pack [CLIENT_ID] [--with-credentials] [--out PATH]
    python -m scripts.handover unpack PACK.zip [--dry-run]

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

Whichever machine publishes owns ``state.json``. When two people publish for one client at
different times, the work moves between them with this: **pack** on the machine that has been
publishing, send the zip, **unpack** on the other, and only then publish there. Video files and the
transcode cache are not packed — copy the video folders across once, separately. ``.env`` is packed
only with ``--with-credentials``, for a first handover; after that each machine keeps its own.

Unpacking refuses, writing nothing, when the ledger already on that machine records a write the
pack's ledger does not: that would forget a permanent GS1 record. See :mod:`lib.handover`.

Runs in the data folder, like every entry point: set ``GS1_DATA_DIR`` when it is not this folder.
Nothing here reaches the network.

Emits (pack): handover-{client}-{stamp}.zip in the data folder, unless ``--out`` says otherwise
Exit codes:
    0  packed, or unpacked (or, with --dry-run, would unpack)
    1  refused, or the pack could not be read or written
    2  config/usage error
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from lib.config import ConfigError, load_clients, resolve_client_id
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.handover import ENV_NAME, HandoverError, pack, unpack

_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_USAGE = 2


def _say(line: str = "") -> None:
    print(line, file=sys.stderr)


def _megabytes(path: Path) -> str:
    return f"{path.stat().st_size / 1_000_000:.1f} MB"


def _pack(args: argparse.Namespace, root: Path, now: datetime) -> int:
    try:
        client_id = resolve_client_id(args.client_id, load_clients(root / "clients.yml"))
    except ConfigError as exc:
        _say(f"error: {exc}")
        return _EXIT_USAGE
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else root / f"handover-{client_id}-{stamp}.zip"
    try:
        manifest = pack(
            root,
            client_id,
            out,
            with_credentials=args.with_credentials,
            now=now,
            machine=platform.node() or "this machine",
        )
    except (HandoverError, OSError) as exc:
        _say(f"error: {exc}")
        return _EXIT_REFUSED

    _say(f"Packed {len(manifest.files)} file(s) for {client_id}: {out} ({_megabytes(out)})")
    _say(f"  Ledger: last write {manifest.ledger_latest or 'none — the ledger is empty'}")
    if manifest.with_credentials:
        _say(f"  {ENV_NAME} IS in this pack: it holds the passwords. Send it privately and delete")
        _say("  the zip once it has been unpacked.")
    else:
        _say(f"  {ENV_NAME} is not in this pack (add --with-credentials for a first handover).")
    _say("  Video files and output/…/media are not in it: copy the video folders separately.")
    _say("  From now on, do not publish from this machine until a pack comes back to it.")
    print(out)
    return _EXIT_OK


def _unpack(args: argparse.Namespace, root: Path, now: datetime) -> int:
    try:
        result = unpack(Path(args.pack), root, now=now, dry_run=args.dry_run)
    except (HandoverError, OSError, ValueError) as exc:
        _say(f"error: {exc}")
        return _EXIT_REFUSED

    manifest = result.manifest
    verb = "Would write" if args.dry_run else "Wrote"
    _say(f"Pack from {manifest.machine}, {manifest.packed_at}, client {manifest.client_id}")
    _say(f"  Ledger: last write {manifest.ledger_latest or 'none — the ledger is empty'}")
    _say(f"  {verb} {len(result.written)} file(s); {result.unchanged} already the same here.")
    for name in result.written:
        _say(f"    {name}")
    if result.backup is not None:
        kept = "would be copied" if args.dry_run else "were copied"
        _say(f"  The files it replaces {kept} first to {result.backup}")
    if not args.dry_run:
        _say("  Next: copy any new video files across, run `python -m scripts.doctor`, and in the")
        _say("  app check Runs → Does the site match the ledger? — before publishing from here.")
    return _EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.handover", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    packing = commands.add_parser("pack", help="zip this data folder for the other machine")
    packing.add_argument("client_id", nargs="?", default=None)
    packing.add_argument(
        "--with-credentials",
        action="store_true",
        help=f"also pack {ENV_NAME} (a first handover; it holds the passwords)",
    )
    packing.add_argument("--out", help="where to write the zip")

    unpacking = commands.add_parser("unpack", help="apply a pack from the other machine")
    unpacking.add_argument("pack", help="the handover zip")
    unpacking.add_argument(
        "--dry-run", action="store_true", help="say what would change; write nothing"
    )
    return parser


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    folder = root if root is not None else Path.cwd()
    now = datetime.now(UTC)
    if args.command == "pack":
        return _pack(args, folder, now)
    return _unpack(args, folder, now)


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
