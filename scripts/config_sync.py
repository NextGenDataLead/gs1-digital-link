"""Keep the client's settings in git: compare, save or take ``config/{client_id}.yml``.

Usage:
    python -m scripts.config_sync check [CLIENT_ID]    # say whether the two match
    python -m scripts.config_sync save [CLIENT_ID]     # live clients.yml -> config/{id}.yml
    python -m scripts.config_sync take [CLIENT_ID]     # config/{id}.yml -> live clients.yml

``CLIENT_ID`` may be omitted when ``clients.yml`` defines exactly one client.

The live ``clients.yml`` is in the data folder and is the one every run uses; the Setup screen
edits it. ``config/{id}.yml`` in the program folder is the reviewed copy in git. **save** is for a
developer clone, before a pull request. **take** is for a machine that should adopt the agreed
settings: it checks the committed copy loads, keeps the live one in
``input/{id}/superseded/``, and replaces it. Passwords are never involved; ``.env`` stays out of
git. See :mod:`lib.committed_config`.

Exit codes:
    0  they match, or the save/take was done
    1  they differ (compare only), or the committed copy is missing or does not load
    2  config/usage error
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from lib.committed_config import committed_path, matches, save, take
from lib.config import ConfigError, load_clients, resolve_client_id
from lib.data_dir import CODE_ROOT, enter_data_dir
from lib.env import load_env
from lib.preflight import check_config

_EXIT_OK = 0
_EXIT_DIFFERS = 1
_EXIT_USAGE = 2


def _say(line: str) -> None:
    print(line, file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.config_sync", description=__doc__)
    parser.add_argument("action", choices=("check", "save", "take"))
    parser.add_argument("client_id", nargs="?", default=None)
    return parser


def main(
    argv: list[str] | None = None, *, root: Path | None = None, code_root: Path = CODE_ROOT
) -> int:
    args = _parser().parse_args(argv)
    folder = root if root is not None else Path.cwd()
    live = folder / "clients.yml"
    try:
        client_id = resolve_client_id(args.client_id, load_clients(live))
    except ConfigError as exc:
        _say(f"error: {exc}")
        return _EXIT_USAGE
    committed = committed_path(client_id, code_root)
    shown = committed.relative_to(code_root).as_posix()

    if args.action == "save":
        save(live, committed)
        _say(f"Saved {live} to {committed}. Commit it in a pull request; .env is not part of it.")
        return _EXIT_OK

    if not committed.is_file():
        _say(f"There is no committed copy at {shown}. `save` writes one.")
        return _EXIT_DIFFERS
    if args.action == "take":
        return _take(live, committed, folder / "input" / client_id / "superseded")
    if matches(live, committed):
        _say(f"clients.yml matches {shown}.")
        return _EXIT_OK
    _say(f"clients.yml differs from {shown}: `save` to keep this machine's, `take` to adopt git's.")
    return _EXIT_DIFFERS


def _take(live: Path, committed: Path, backup_dir: Path) -> int:
    loaded = check_config(committed)
    if loaded.failed:
        _say(f"Not taken: {committed} does not load: {loaded.detail}")
        return _EXIT_DIFFERS
    kept = take(live, committed, backup_dir, datetime.now(UTC))
    _say(f"Took {committed} as this machine's settings.")
    if kept is not None:
        _say(f"  The previous clients.yml is kept at {kept}")
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
