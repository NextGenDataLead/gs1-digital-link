"""Move one client's data folder from one machine to another, ledger first (``scripts.handover``).

Two people publish for the same client, at different times, from different machines. Whatever
machine publishes owns ``state.json`` from that moment — the ledger of permanent GS1 writes — so
handing the work over means handing the ledger over, and the one failure that matters is a
machine overwriting a ledger that knows about a publish it does not.

A **pack** is a zip of the data folder's ``clients.yml``, ``input/{client}/`` — which includes the
client's own prompt and page templates (:mod:`lib.client_files`) — and ``output/{client}/``, with a
``handover.json`` manifest. Three things stay out:

* video files, by extension (gigabytes, copied across once, separately, and rarely changing);
* ``output/{client}/media/``, the transcode cache, which the receiving machine rebuilds;
* ``.env``, unless asked for. Each person may hold credentials of their own (a WordPress
  application password per person is what makes one revocable), and a pack that always carried
  ``.env`` would quietly replace the receiver's with the sender's.

**Unpacking refuses** when the ledger already here records a page the pack's ledger does not, or
records it more recently: that machine published after the pack was made, and replacing its
ledger would forget a permanent write. Nothing is written in that case. Otherwise every file that
would change is first copied to ``input/{client}/superseded/handover-{stamp}/``, then replaced.
Files here that the pack does not carry are left alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import ValidationError

from lib.media_video import VIDEO_EXTENSIONS
from lib.owner_only import restrict
from lib.records import State

MANIFEST_NAME: Final = "handover.json"
CONFIG_NAME: Final = "clients.yml"
ENV_NAME: Final = ".env"
BACKUP_PREFIX: Final = "handover-"

#: Volume and editor cruft that is never data: Finder, Explorer, and Office's lock files.
_CRUFT_NAMES: Final = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_LOCK_PREFIX: Final = "~$"
#: Folders Windows writes onto every drive it indexes — a video disk carries one.
_CRUFT_DIRS: Final = frozenset({"System Volume Information", "$RECYCLE.BIN"})
#: How many of the entries a refusal names before it says "and N more".
_SHOWN: Final = 5


class HandoverError(Exception):
    """The pack cannot be made or applied as asked. Nothing has been written."""


@dataclass(frozen=True)
class Manifest:
    """What a pack says about itself, written to ``handover.json`` inside it."""

    client_id: str
    packed_at: str
    machine: str
    with_credentials: bool
    #: sha256 of the packed ``state.json``; ``None`` when the folder had no ledger yet.
    state_sha256: str | None
    #: The most recent ``last_run`` the packed ledger records, ISO 8601; ``None`` when empty.
    ledger_latest: str | None
    files: tuple[str, ...]

    def to_json(self) -> str:
        payload = {**self.__dict__, "files": list(self.files)}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        data = json.loads(raw)
        return cls(
            client_id=str(data["client_id"]),
            packed_at=str(data["packed_at"]),
            machine=str(data["machine"]),
            with_credentials=bool(data["with_credentials"]),
            state_sha256=data["state_sha256"],
            ledger_latest=data["ledger_latest"],
            files=tuple(str(name) for name in data["files"]),
        )


@dataclass(frozen=True)
class UnpackResult:
    """What applying a pack did, or — on a dry run — would do."""

    manifest: Manifest
    #: Files that are new here or whose content changed.
    written: tuple[str, ...]
    #: Files already identical here.
    unchanged: int
    #: Where the replaced files were copied first; ``None`` when nothing was replaced.
    backup: Path | None


# --- what goes in --------------------------------------------------------------------------------


def state_file(client_id: str) -> str:
    """The ledger's path inside a data folder, as a pack spells it."""
    return f"output/{client_id}/state.json"


def _is_carried(relative: PurePosixPath, client_id: str) -> bool:
    name = relative.name
    if name in _CRUFT_NAMES or name.startswith(_LOCK_PREFIX):
        return False
    if _CRUFT_DIRS.intersection(relative.parts):
        return False
    if relative.suffix.lower() in VIDEO_EXTENSIONS:
        return False
    parts = relative.parts
    if parts[:3] == ("output", client_id, "media"):
        return False
    # Earlier handovers' backups: carrying them would grow every pack by the one before it.
    superseded = ("input", client_id, "superseded")
    return not (
        parts[: len(superseded)] == superseded
        and len(parts) > len(superseded)
        and parts[len(superseded)].startswith(BACKUP_PREFIX)
    )


def members(root: Path, client_id: str, *, with_credentials: bool) -> list[str]:
    """The data-folder files a pack carries, as sorted POSIX paths relative to ``root``."""
    names: list[str] = []
    if (root / CONFIG_NAME).is_file():
        names.append(CONFIG_NAME)
    if with_credentials and (root / ENV_NAME).is_file():
        names.append(ENV_NAME)
    for top in ("input", "output"):
        base = root / top / client_id
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_file() and _is_carried(relative, client_id):
                names.append(str(relative))
    return sorted(set(names))


# --- the ledger ----------------------------------------------------------------------------------


def _parse_state(raw: bytes, where: str) -> State:
    try:
        return State.model_validate_json(raw)
    except ValidationError as exc:
        raise HandoverError(
            f"the ledger {where} cannot be read ({exc.error_count()} problem(s)), so it cannot be "
            "compared. Nothing was written."
        ) from exc


def ledger_latest(state: State) -> datetime | None:
    """The most recent write the ledger records, or ``None`` for an empty ledger."""
    times = [entry.last_run for languages in state.entries.values() for entry in languages.values()]
    return max(times, default=None)


def unknown_to(packed: State, here: State) -> list[str]:
    """``gtin/language`` entries this ledger records that the pack's does not, or not as recently.

    Each one is a write the pack would make this machine forget.
    """
    lost: list[str] = []
    for gtin, languages in sorted(here.entries.items()):
        for language, entry in sorted(languages.items()):
            theirs = packed.entries.get(gtin, {}).get(language)
            if theirs is None or theirs.last_run < entry.last_run:
                lost.append(f"{gtin}/{language}")
    return lost


# --- pack ----------------------------------------------------------------------------------------


def pack(  # noqa: PLR0913 — keyword-only after the first three; each one is a separate fact
    root: Path,
    client_id: str,
    out: Path,
    *,
    with_credentials: bool,
    now: datetime,
    machine: str,
) -> Manifest:
    """Write the pack for ``client_id`` from the data folder ``root`` to ``out``."""
    names = members(root, client_id, with_credentials=with_credentials)
    if CONFIG_NAME not in names:
        raise HandoverError(f"{root} has no {CONFIG_NAME}: this is not a data folder")

    ledger = root / state_file(client_id)
    state_sha: str | None = None
    latest: datetime | None = None
    if ledger.is_file():
        raw = ledger.read_bytes()
        state_sha = hashlib.sha256(raw).hexdigest()
        latest = ledger_latest(_parse_state(raw, f"at {ledger}"))

    manifest = Manifest(
        client_id=client_id,
        packed_at=now.astimezone(UTC).isoformat(),
        machine=machine,
        with_credentials=ENV_NAME in names,
        state_sha256=state_sha,
        ledger_latest=latest.isoformat() if latest else None,
        files=tuple(names),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, manifest.to_json())
        for name in names:
            archive.write(root / name, arcname=name)
    partial.replace(out)
    return manifest


# --- unpack --------------------------------------------------------------------------------------


def _allowed(name: str, client_id: str) -> bool:
    """Whether a member may be written: only what a pack carries, and never outside the folder."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        return False
    if name in (CONFIG_NAME, ENV_NAME):
        return True
    for top in (("input", client_id), ("output", client_id)):
        if path.parts[: len(top)] == top and len(path.parts) > len(top):
            return True
    return False


def read_manifest(archive: zipfile.ZipFile) -> Manifest:
    try:
        raw = archive.read(MANIFEST_NAME).decode("utf-8")
    except KeyError as exc:
        raise HandoverError(f"this zip has no {MANIFEST_NAME}: it is not a handover pack") from exc
    try:
        return Manifest.from_json(raw)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HandoverError(f"{MANIFEST_NAME} in this zip cannot be read: {exc}") from exc


def _check_ledger(archive: zipfile.ZipFile, manifest: Manifest, root: Path) -> None:
    here = root / state_file(manifest.client_id)
    if not here.is_file():
        return
    local = _parse_state(here.read_bytes(), f"at {here}")
    if state_file(manifest.client_id) in manifest.files:
        packed = _parse_state(archive.read(state_file(manifest.client_id)), "in the pack")
    else:
        packed = State(client_id=manifest.client_id, entries={})
    lost = unknown_to(packed, local)
    if lost:
        more = len(lost) - _SHOWN
        shown = ", ".join(lost[:_SHOWN]) + (f" and {more} more" if more > 0 else "")
        raise HandoverError(
            f"refused: the ledger on this machine records {len(lost)} page(s) the pack's ledger "
            f"does not, or records them more recently ({shown}). This machine published after "
            f"the pack was made on {manifest.machine} ({manifest.packed_at}), and unpacking would "
            "forget those writes. Nothing was written. Pack this machine instead and send that "
            "the other way, or ask the maintainer to merge the two ledgers."
        )


def unpack(zip_path: Path, root: Path, *, now: datetime, dry_run: bool = False) -> UnpackResult:
    """Apply the pack at ``zip_path`` to the data folder ``root``.

    Raises:
        HandoverError: The zip is not a pack, names a path outside what a pack carries, or the
            ledger here knows a write the pack's does not. Nothing has been written.
    """
    with zipfile.ZipFile(zip_path) as archive:
        manifest = read_manifest(archive)
        stray = [name for name in manifest.files if not _allowed(name, manifest.client_id)]
        missing = sorted(set(manifest.files) - set(archive.namelist()))
        if stray or missing:
            raise HandoverError(
                "this pack cannot be applied: "
                + "; ".join(
                    part
                    for part in (
                        f"it names paths outside a data folder ({', '.join(stray)})"
                        if stray
                        else "",
                        f"its manifest lists files it does not hold ({', '.join(missing)})"
                        if missing
                        else "",
                    )
                    if part
                )
            )
        _check_ledger(archive, manifest, root)

        changed: list[str] = []
        unchanged = 0
        for name in manifest.files:
            target = root / name
            if target.is_file() and target.read_bytes() == archive.read(name):
                unchanged += 1
            else:
                changed.append(name)

        replaced = [name for name in changed if (root / name).is_file()]
        backup = (
            root
            / "input"
            / manifest.client_id
            / "superseded"
            / f"{BACKUP_PREFIX}{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            if replaced
            else None
        )
        if not dry_run:
            if backup is not None:
                for name in replaced:
                    copy = backup / name
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    copy.write_bytes((root / name).read_bytes())
            for name in changed:
                _extract(archive, name, root / name)

    return UnpackResult(
        manifest=manifest, written=tuple(changed), unchanged=unchanged, backup=backup
    )


def _extract(archive: zipfile.ZipFile, name: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(archive.read(name))
    partial.replace(target)
    # Keep the sender's modification time: "newest export" and similar are read off it.
    stamp = datetime(*archive.getinfo(name).date_time).timestamp()
    os.utime(target, (stamp, stamp))
    if target.name == ENV_NAME:
        restrict(target)
