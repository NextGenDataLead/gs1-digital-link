"""A client's own prompt and page templates: read from the data folder first, then the release.

The voice prompt (``generation.{prompt_version}.md``) and any HTML page templates belong to one
client, not to the program, so they live in the data folder beside that client's other files::

    input/{client}/prompts/generation.{prompt_version}.md
    input/{client}/templates/product.{language}.html

There they travel in a handover pack with everything else under ``input/{client}/``, and a newer
release — which ships none of them — cannot replace them.

A release may still carry ``prompts/{client}/`` and ``templates/{client}/`` (this repository does,
as their reviewed copy), and those are the **fallback**: an installation that has not moved its
files into the data folder keeps working exactly as before. The data folder wins whenever it has
the file. The built-in ``templates/_default/`` stays with the code; it is the program's, not a
client's.

Data paths here are relative, like every other data path in the project (see
:mod:`lib.data_dir`): the data folder is the working directory.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import yaml

from lib.data_dir import CODE_ROOT

PROMPTS: Final = "prompts"
TEMPLATES: Final = "templates"

#: The built-in fallback templates. Program files, so never adopted into a data folder.
DEFAULT_TEMPLATES: Final = "_default"

Kind = Literal["prompts", "templates"]


def data_dir(client_id: str, kind: Kind, data_root: Path = Path()) -> Path:
    """Where the data folder keeps this client's ``prompts`` or ``templates``."""
    return data_root / "input" / client_id / kind


def release_dir(client_id: str, kind: Kind, code_root: Path | None = None) -> Path:
    """Where a release carries this client's ``prompts`` or ``templates``, when it does."""
    return (CODE_ROOT if code_root is None else code_root) / kind / client_id


def voice_prompt_name(prompt_version: str) -> str:
    return f"generation.{prompt_version}.md"


def voice_prompt_candidates(
    client_id: str,
    prompt_version: str,
    *,
    data_root: Path = Path(),
    code_root: Path | None = None,
) -> tuple[Path, Path]:
    """The voice prompt's two possible places, in the order they are tried."""
    name = voice_prompt_name(prompt_version)
    return (
        data_dir(client_id, PROMPTS, data_root) / name,
        release_dir(client_id, PROMPTS, code_root) / name,
    )


@dataclass(frozen=True)
class Source:
    """Where a client's prompt or templates were found, for the preflight to say."""

    kind: Kind
    #: ``data`` (the data folder), ``release`` (the program folder only), or ``none``.
    where: Literal["data", "release", "none"]
    path: Path | None


def locate(
    client_id: str,
    kind: Kind,
    *,
    data_root: Path = Path(),
    code_root: Path | None = None,
) -> Source:
    """Which folder holds this client's ``prompts`` or ``templates`` — data folder first."""
    candidates: tuple[tuple[Literal["data", "release"], Path], ...] = (
        ("data", data_dir(client_id, kind, data_root)),
        ("release", release_dir(client_id, kind, code_root)),
    )
    for where, folder in candidates:
        if folder.is_dir() and any(p.is_file() for p in folder.iterdir()):
            return Source(kind, where, folder)
    return Source(kind, "none", None)


def differences(client_id: str, kind: Kind, *, root: Path) -> list[str]:
    """In a development clone, the data folder's files that differ from the committed ones.

    There the two copies sit in one folder: ``input/{client}/{kind}`` (gitignored, written by a
    received handover pack) and ``{kind}/{client}`` (committed, reviewed). The first one wins, so
    a difference means an edit to the committed copy is not what runs. Named relative to ``root``;
    a file only the data folder has counts too.
    """
    data, release = data_dir(client_id, kind, root), release_dir(client_id, kind, root)
    if not data.is_dir():
        return []
    differ: list[str] = []
    for path in sorted(p for p in data.rglob("*") if p.is_file()):
        twin = release / path.relative_to(data)
        if not twin.is_file() or twin.read_bytes() != path.read_bytes():
            differ.append(path.relative_to(root).as_posix())
    return differ


def adopt(code_root: Path, data_root: Path, client_ids: list[str]) -> list[str]:
    """Copy each client's prompts and templates from a release into the data folder.

    Only into a data folder that has **none** of that kind for that client yet: an existing
    ``input/{client}/prompts`` or ``templates`` folder — even a partial one — is left exactly as
    it is, because it is the operator's and may be newer than the release's. Nothing is ever
    overwritten or merged. Returns one line per folder copied.

    A development clone (data folder and program folder the same) is skipped: there the release's
    copy already *is* the data folder's, and copying it beside itself would only fork it.
    """
    if code_root.resolve() == data_root.resolve():
        return []
    notes: list[str] = []
    for client_id in client_ids:
        if client_id == DEFAULT_TEMPLATES:
            continue
        for kind in (PROMPTS, TEMPLATES):
            source = release_dir(client_id, kind, code_root)
            target = data_dir(client_id, kind, data_root)
            if not source.is_dir() or target.exists():
                continue
            shutil.copytree(source, target)
            notes.append(
                f"copied {kind}/{client_id} into the data folder "
                f"(input/{client_id}/{kind}) — it travels in a handover pack from now on"
            )
    return notes


def configured_clients(config: Path) -> list[str]:
    """The client ids a ``clients.yml`` defines, or none when it is missing or will not parse.

    Deliberately not :func:`lib.config.load_clients`: the installer calls this, and an install
    must not stop over a config the Setup screen is there to repair.
    """
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    clients = data.get("clients") if isinstance(data, dict) else None
    return sorted(str(key) for key in clients) if isinstance(clients, dict) else []
