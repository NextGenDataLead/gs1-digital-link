"""Decide, record and report this installation's data folder (:mod:`lib.install_location`).

Called by the double-click scripts, never by an operator directly:

    python -m scripts.data_folder --install   # install.command / install.bat
    python -m scripts.data_folder             # start.command / start.bat

The last line on stdout is the folder, so a start script can capture it into ``GS1_DATA_DIR``;
everything meant for a person goes to stderr. Without ``--install`` nothing is written: an
already-set ``GS1_DATA_DIR`` wins (IT may pin one), then the same rules as the installer, read-only
— and where the installer would *create* a folder, it exits 1 asking for the installer instead.

Exempt from the ``enter_data_dir`` / ``load_env`` rule (``tests/lib/test_env.py``): it is what
*decides* the data folder, so it cannot start inside one, and it touches no credential.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lib.client_files import adopt, configured_clients
from lib.data_dir import CODE_ROOT, DATA_DIR_ENV
from lib.install_location import (
    PlacementError,
    is_code_folder,
    place,
    pointer_file,
    settle,
)

_EXIT_OK = 0
_EXIT_NOT_PLACED = 1


def _say(line: str = "") -> None:
    print(line, file=sys.stderr)


def _install(code_root: Path, pointer: Path, home: Path) -> int:
    try:
        placement = place(code_root, pointer, home)
    except PlacementError as exc:
        _say(str(exc))
        return _EXIT_NOT_PLACED
    notes = settle(placement, pointer, code_root)
    # The one write into a folder that already holds data: a client's own prompt and templates,
    # copied from this release only where the data folder has none of them yet.
    clients = configured_clients(placement.folder / "clients.yml")
    notes += adopt(code_root, placement.folder, clients)

    _say(f"This installation's data folder is: {placement.folder}")
    for note in notes:
        _say(f"  - {note}")
    if placement.how == "here":
        _say("  It is this folder itself, as before. Nothing in it was changed.")
    elif placement.how == "remembered" and is_code_folder(placement.folder):
        _say("  That is an earlier version's folder, which still holds the data.")
        _say("  KEEP IT. Deleting it would delete the record of what was published.")
    _say("  Back it up: it holds the credentials and the record of what was published.")
    print(placement.folder)
    return _EXIT_OK


def _report(code_root: Path, pointer: Path, home: Path) -> int:
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    if configured:
        print(configured)
        return _EXIT_OK
    # The installer's rules, read-only: a folder that already holds data is used where it is —
    # so an installation that predates the installer step, or a development clone, still starts —
    # and the two refusals apply here too.  Only creating a new folder is left to the installer.
    try:
        placement = place(code_root, pointer, home)
    except PlacementError as exc:
        _say(str(exc))
        return _EXIT_NOT_PLACED
    if placement.how == "new":
        _say("This machine has no data folder yet: run the installer first.")
        return _EXIT_NOT_PLACED
    print(placement.folder)
    return _EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    code_root: Path = CODE_ROOT,
    pointer: Path | None = None,
    home: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="data_folder", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--install", action="store_true", help="Decide, create and record the data folder"
    )
    args = parser.parse_args(argv)
    pointer = pointer or pointer_file()
    if args.install:
        return _install(code_root, pointer, home or Path.home())
    return _report(code_root, pointer, home or Path.home())


if __name__ == "__main__":
    raise SystemExit(main())
