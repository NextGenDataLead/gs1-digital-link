"""Where an installed copy keeps its data, decided once per machine user and remembered.

A release is a folder of code: download it, unzip it, double-click the installer. The next release
is a *different* folder. The data — ``.env``, ``clients.yml``, ``input/``, ``output/`` and the
``state.json`` ledger of permanent GS1 writes — must outlive both, so it lives in a data folder of
its own (:mod:`lib.data_dir`), and a one-line **pointer** in the user's application-settings folder
records where. The installer decides and records; ``start`` reads.

The decision, in order:

1. **Remembered** — a pointer from an earlier install. Every later release uses the same data.
2. **Here** — this code folder already holds data: an installation from before data folders
   existed (the original Democlient machine). It stays exactly where it is; the pointer just
   records it, so a later release unzipped elsewhere still finds it.
3. **New** — neither: a fresh folder in the user's home directory, seeded from the examples.

Two situations are refused rather than guessed, because each guess would be a second ledger:
the pointer names a folder that is gone, and both the pointer's folder and this one hold data.

Nothing here is ever deleted or overwritten. Seeding copies an example only where no file exists.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from lib.owner_only import restrict

APP_NAME: Final = "GS1 Digital Link"
POINTER_NAME: Final = "data-folder.txt"

#: A new installation's data folder, in the home directory. Not Documents: on a managed machine
#: that is often synced (OneDrive, iCloud), and a sync client rewriting ``output/`` mid-run is the
#: failure ``docs/operator-install.md`` already warns about for the code folder.
NEW_FOLDER_NAME: Final = "GS1 Digital Link data"

How = Literal["remembered", "here", "found", "new"]


class PlacementError(Exception):
    """The data folder cannot be decided without risking a second ledger. The message says why."""


@dataclass(frozen=True)
class Placement:
    """Where the data is, and how that was decided."""

    folder: Path
    how: How


def pointer_file(
    platform: str = sys.platform,
    home: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """The per-user file that remembers the data folder, where each OS keeps app settings."""
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME / POINTER_NAME
    if platform == "win32":
        appdata = environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / APP_NAME / POINTER_NAME
    config = environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(config) / "gs1-digital-link" / POINTER_NAME


def recorded(pointer: Path) -> Path | None:
    """The data folder the pointer names, or ``None`` when there is no pointer yet."""
    if not pointer.is_file():
        return None
    text = pointer.read_text(encoding="utf-8").strip()
    return Path(text) if text else None


def holds_data(folder: Path) -> bool:
    """Whether ``folder`` already belongs to an installation: settings, secrets or a ledger."""
    return (
        (folder / "clients.yml").is_file()
        or (folder / ".env").is_file()
        or any((folder / "output").glob("*/state.json"))
    )


def _same(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def place(code_root: Path, pointer: Path, home: Path) -> Placement:
    """Decide where the data is. Reads only; :func:`settle` makes it so.

    Raises:
        PlacementError: the remembered folder is missing, or two folders hold data.
    """
    remembered = recorded(pointer)
    here = holds_data(code_root)
    if remembered is not None:
        if not remembered.is_dir():
            raise PlacementError(
                f"This machine's data folder was recorded as {remembered}, and it is not there. "
                "If it was moved, put it back or tell whoever maintains this tool where it went. "
                "Nothing was changed: starting an empty one would lose sight of everything "
                f"published so far. (The record is {pointer}.)"
            )
        if here and not _same(remembered, code_root):
            raise PlacementError(
                f"Two folders hold this tool's data: {remembered} (recorded as this machine's) "
                f"and {code_root} (this download). Using either would hide the other's record "
                "of what was published. Nothing was changed — ask whoever maintains this tool "
                "which one is current."
            )
        return Placement(remembered, "remembered")
    if here:
        return Placement(code_root, "here")
    new = home / NEW_FOLDER_NAME
    return Placement(new, "found" if holds_data(new) else "new")


def _seed(example: Path, target: Path, *, private: bool) -> bool:
    if target.exists() or not example.is_file():
        return False
    shutil.copyfile(example, target)
    if private:
        restrict(target)
    return True


def settle(placement: Placement, pointer: Path, code_root: Path) -> list[str]:
    """Create and seed the data folder if it is new, and record it. Returns what was done.

    A folder that already holds data is never written to here — not even a missing ``.env``
    filled in from the example: only the pointer changes. Seeding is for a folder this call
    creates.
    """
    notes: list[str] = []
    folder = placement.folder
    if placement.how == "new":
        folder.mkdir(parents=True, exist_ok=True)
        for sub in ("input", "output"):
            (folder / sub).mkdir(exist_ok=True)
        if _seed(code_root / "clients.example.yml", folder / "clients.yml", private=False):
            notes.append("wrote clients.yml from the example — fill it in on the Setup screen")
        if _seed(code_root / ".env.example", folder / ".env", private=True):
            notes.append("wrote .env from the example (owner-only) — credentials go there")
    if recorded(pointer) != folder:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        staged = pointer.with_suffix(".tmp")
        staged.write_text(f"{folder}\n", encoding="utf-8")
        staged.replace(pointer)
        notes.append(f"recorded the data folder in {pointer}")
    return notes


def is_code_folder(folder: Path) -> bool:
    """Whether ``folder`` is a release's own folder — data kept the pre-data-folder way."""
    return (folder / "pyproject.toml").is_file()
