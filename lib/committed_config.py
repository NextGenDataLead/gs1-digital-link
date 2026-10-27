"""The client's settings kept in git, beside the live copy the app reads (``scripts.config_sync``).

``clients.yml`` lives in the **data folder**: the Setup screen edits it there, and every entry
point reads it there. It holds settings and the *names* of the credential variables, never their
values. A reviewed, versioned copy of it is committed at ``config/{client_id}.yml`` in the program
folder, so a change to the settings goes through a pull request like a change to the code, and
the next person to set up a machine starts from what was agreed rather than from whatever file
was last copied.

Two copies of one file drift, so this module only ever does three things with them: says whether
they still match (the preflight check), copies the live one into the program folder to be
committed (``save``), and copies the committed one over the live one, keeping the live one aside
first (``take``). The live copy stays the one that is used. ``.env`` is never part of this:
passwords do not go into git.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Final

from lib.data_dir import CODE_ROOT

#: Where the committed settings live, relative to the program folder.
COMMITTED_DIR: Final = "config"


def committed_path(client_id: str, code_root: Path = CODE_ROOT) -> Path:
    """The committed copy of ``client_id``'s settings in the program folder."""
    return code_root / COMMITTED_DIR / f"{client_id}.yml"


def _text(path: Path) -> str:
    # Git for Windows may check the committed copy out with CRLF; that is not a difference.
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def matches(live: Path, committed: Path) -> bool:
    """Whether the live settings say exactly what the committed copy says."""
    return _text(live) == _text(committed)


def save(live: Path, committed: Path) -> None:
    """Copy the live settings into the program folder, to be committed."""
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(_text(live), encoding="utf-8")


def take(live: Path, committed: Path, backup_dir: Path, now: datetime) -> Path | None:
    """Replace the live settings with the committed copy; return where the old one was kept.

    ``None`` when there was no live file to keep. The caller validates the committed copy first:
    a file that will not load must never replace one that does.
    """
    kept: Path | None = None
    if live.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        kept = backup_dir / f"clients.yml.before-take-{now.strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copy2(live, kept)
    live.write_text(_text(committed), encoding="utf-8")
    return kept
