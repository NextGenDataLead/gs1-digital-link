"""How a derived artefact names the thing it came from.

A batch is two uploaded files and the ticks the operator puts on them. Three facts about it cannot
be recovered from the files themselves, however carefully they are filed:

* **What the export was called when it was sent.** The upload renames it to ``export.xlsx``, so the
  only answer left on disk is a timestamp. "Which export did this run use?" deserves the name the
  operator recognises.
* **Which export a selection was chosen against.** Nothing links the two. A selection made against
  last quarter's export looks exactly like one made this morning.
* **Which selection a run consumed.** ``input/`` means *what the next run will use* and is
  overwritten by the next batch, so by the time anybody asks, the answer has moved.

So each of them is **recorded when it happens**, in one line, here:

    input/{client}/process/history.jsonl

One append-only file rather than a sidecar per artefact, for two reasons. ``selection/`` is a folder
the operator opens in Finder and a ``.json`` beside every ``.xlsx`` doubles what they have to read
past. And the third fact — *a run used this* — is learned **after** the selection is written, which
a write-once sidecar cannot hold without being rewritten. Appending handles a later fact natively.

The run's half is not here: it is ``runs/{stamp}/inputs.json``, written by the run into its own
directory, because a run reads ``input/`` and writes ``output/`` and that is not negotiable. Same
record type (:class:`SourceRef`), same rule, in the folder that owns the artefact.

**Name is the reference; the hash is the check.** A record points at an archived *filename*, and
carries the sha256 only to prove the live file still is that one. That is this project's existing
convention — ``lib/generator.py`` says a fingerprint is "a validity check only, never a reuse key",
and ``docs/troubleshooting.md`` records why media identity had to move *into* the name when metadata
turned out to be droppable. A hash used as the lookup key would make every rename a silent miss.

**Nothing here can fail an operator's action.** :func:`append` returns whether it wrote and never
raises: a save that succeeded must not be reported as failed because a note about it could not be
filed. The cost is that a record can be missing, so every reader treats absence as *not recorded* —
never as *no source*. Those are different answers and only one of them is safe to act on.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, ValidationError

from lib.input_layout import PROCESS_DIR, client_root

#: The ledger's name, inside ``process/`` — one level above the two halves it records, because it
#: records both. Machine-shaped, and the README in the client folder says so in one line.
HISTORY_NAME: Final = "history.jsonl"

#: Bumped when a line's shape changes. Read before validation, so a line written by a *newer*
#: version is counted as unreadable rather than half-understood: pydantic would otherwise drop the
#: keys it did not expect and hand back a record that looks complete. That is the failure
#: ``one-field-two-questions`` is about, and the reason this field exists at all.
#:
#: 2 added a selection's ``mode``. Older lines are still read — the new field is optional and a
#: version-1 line simply has none — so :data:`_READABLE` names every version this code understands.
VERSION: Final = 2
_READABLE: Final = frozenset({1, VERSION})

#: What a batch publishes — ``lib.gates.Mode``'s values, spelled out because ``lib.gates`` is a
#: consumer of this module's records and must not become a dependency of it.
RunMode = Literal["pages", "links", "both"]

#: What a recorded document is. Not a second field beside a ``kind`` saying the same thing — one
#: value, because two fields answering one question is how they come to disagree.
What = Literal["export", "product-list", "selection"]

#: Read in chunks so a 500 kB export — or a much larger one — never lands in memory whole.
_CHUNK: Final = 1 << 16


class SourceRef(BaseModel):
    """One file, named and checked.

    ``name`` is the **dated archive** this file's bytes match — the thing a later reader can still
    open. ``None`` means no record names it, which is the honest answer for a file placed by hand
    rather than uploaded: it is not an error and must not read as one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None
    given_name: str | None = None
    sha256: str
    bytes: int
    rows: int | None = None


class Recorded(BaseModel):
    """One line of the ledger: a document, when it arrived, and what it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = VERSION
    what: What
    at: datetime
    of: SourceRef
    #: Keyed by :data:`What`. Empty for an upload, which came from outside the tool.
    sources: dict[str, SourceRef] = {}
    #: A selection's only: what the operator chose to publish with it, on the Data screen. ``None``
    #: on every upload and on a selection saved before the choice moved there — never a default.
    mode: RunMode | None = None


class RunInputs(BaseModel):
    """What one run read — ``runs/{stamp}/inputs.json``.

    Its own file in the run's own directory rather than a line in the ledger, because a run reads
    ``input/`` and writes ``output/`` and that is not negotiable. Same record type, same rule, in
    the folder that owns the artefact.

    ``dry_run`` is load-bearing: a rehearsal reads the same selection as a real publish, and a
    selection "used by" a dry run has not been published. Conflating the two would report a draft as
    finished, which is the direction that costs something.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = VERSION
    at: datetime
    mode: str
    dry_run: bool
    sources: dict[str, SourceRef] = {}


class History(NamedTuple):
    """The ledger as far as it could be read.

    ``unreadable`` is part of the answer, not an aside: a truncated final line is what a process
    killed mid-append leaves, and a reader that silently dropped it would report one fewer save than
    happened. The same count catches a line from a version this code does not know.
    """

    entries: list[Recorded]
    unreadable: int

    def newest(self, what: What) -> Recorded | None:
        """The last recorded document of this kind, or ``None``.

        By position in the file and not by the ``at`` timestamp: the file is append-only, so its
        order *is* the order things happened, and a clock that stepped backwards cannot reorder it.
        """
        for entry in reversed(self.entries):
            if entry.what == what:
                return entry
        return None

    def named(self, what: What, name: str) -> Recorded | None:
        """The recorded document of this kind with this archived name, or ``None``."""
        for entry in reversed(self.entries):
            if entry.what == what and entry.of.name == name:
                return entry
        return None


def history_path(export: Path) -> Path | None:
    """Where this client's ledger lives, given its export path, or ``None`` outside the layout.

    ``None`` is an ordinary state, not a failure: at least one client is still on the pre-layout
    flat shape, and nothing about it should start raising.

    Takes a **path and not a config**, like everything else here. Configured paths are relative and
    nothing in this module knows what they are relative *to*: ``scripts/`` runs with the repository
    as its working directory, while the shell is a long-lived process started from wherever the
    operator double-clicked and resolves every configured path against ``ui.DATA_ROOT`` itself.
    Resolving here would have picked the wrong one of those two, silently, and written a second
    ledger next to nothing.
    """
    root = client_root(export)
    return None if root is None else root / PROCESS_DIR / HISTORY_NAME


def sha256_of(path: Path) -> str | None:
    """The file's digest, or ``None`` if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def describe(
    path: Path,
    *,
    name: str | None = None,
    given_name: str | None = None,
    rows: int | None = None,
) -> SourceRef | None:
    """A :class:`SourceRef` for a file on disk, or ``None`` if it is not readable."""
    digest = sha256_of(path)
    if digest is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return SourceRef(name=name, given_name=given_name, sha256=digest, bytes=size, rows=rows)


def read(path: Path | None) -> History:
    """Every line the ledger has, oldest first, counting the ones that did not parse.

    A missing file is an empty history, not an error — it is what every client had until the first
    upload after this shipped.
    """
    if path is None or not path.is_file():
        return History([], 0)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return History([], 0)

    entries: list[Recorded] = []
    unreadable = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            unreadable += 1
            continue
        if not isinstance(data, dict) or data.get("v") not in _READABLE:
            unreadable += 1
            continue
        try:
            entries.append(Recorded.model_validate(data))
        except ValidationError:
            unreadable += 1
    return History(entries, unreadable)


def append(path: Path | None, entry: Recorded) -> bool:
    """Add one line. Returns whether it landed; never raises.

    One line, opened in append mode and flushed — the shape ``_RunLog`` uses for the same reason: a
    reader may be looking at this file while it grows, and a record half-written is a record that
    reads as unparseable rather than as something else.
    """
    if path is None:
        return False
    line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    except OSError:
        return False
    return True


def resolve(
    live: Path, history: History, what: What, *, rows: int | None = None
) -> SourceRef | None:
    """Identify the live file: its own hash, and the archived name the ledger gives those bytes.

    The lookup is *newest record of this kind first, by position*, and it only accepts a hash match.
    That ordering matters: a re-upload of bytes already seen should resolve to the newest copy of
    them, which is the one beside the live file.

    A miss returns a ref with ``name=None`` rather than ``None`` — the file is real and identified
    by its hash; what is missing is a record naming it. Collapsing those two into one answer is how
    "nobody wrote this down" comes to read as "there is no export".
    """
    ref = describe(live, rows=rows)
    if ref is None:
        return None
    for entry in reversed(history.entries):
        if entry.what == what and entry.of.sha256 == ref.sha256:
            # The row count comes from the record too, when the caller has not counted for itself.
            # These are the same bytes, so it is the same count — and it spares a caller that only
            # wants to *name* a file from parsing a workbook to say how big it was. Without it,
            # ``inputs.json`` recorded ``rows: null`` for everything, so the Runs screen could name
            # the export a run used but not say how many rows its selection held.
            return ref.model_copy(
                update={
                    "name": entry.of.name,
                    "given_name": entry.of.given_name,
                    "rows": ref.rows if ref.rows is not None else entry.of.rows,
                }
            )
    return ref


# --- Recording ---------------------------------------------------------------------------------
#
# The write points. Each returns whether the line landed, so a caller can say so; none of them can
# stop the act they are describing.


def record_upload(
    history: Path | None,
    what: What,
    *,
    kept: Path | None,
    given_name: str | None,
    rows: int | None = None,
) -> bool:
    """Record a file arriving. ``kept`` is the dated archive — the name a later reader can open.

    Recorded from the *archive* and not from the live file, even though they are byte-identical when
    this is called: the live one is about to be replaced and its name says nothing, while the dated
    copy is the thing a selection or a run will point at months later.
    """
    if kept is None:
        return False
    ref = describe(kept, name=kept.name, given_name=given_name, rows=rows)
    if ref is None:
        return False
    return append(history, Recorded(what=what, at=datetime.now(UTC), of=ref))


def record_selection(  # noqa: PLR0913 — the save, its two sources, and what it says about them
    history: Path | None,
    saved: Path,
    *,
    product_list: Path,
    export: Path,
    rows: int | None = None,
    mode: RunMode | None = None,
) -> bool:
    """Record a save: the dated selection, the two documents it was chosen from, and its mode.

    Both sources are resolved by hashing the file **on disk now**, rather than by trusting the last
    upload record. That is deliberate — an export replaced outside the shell, or a client set up
    before any of this existed, still gets identified by its bytes, and the only thing lost is the
    name.
    """
    ref = describe(saved, name=saved.name, rows=rows)
    if ref is None:
        return False

    known = read(history)
    sources: dict[str, SourceRef] = {}
    chosen_from: tuple[tuple[What, Path], ...] = (
        ("product-list", product_list),
        ("export", export),
    )
    for what, live in chosen_from:
        found = resolve(live, known, what)
        if found is not None:
            sources[what] = found
    return append(
        history,
        Recorded(what="selection", at=datetime.now(UTC), of=ref, sources=sources, mode=mode),
    )


def record_run(  # noqa: PLR0913 — one keyword per document a run reads, plus where to look
    path: Path,
    *,
    mode: str,
    dry_run: bool,
    selection: Path,
    product_list: Path,
    export: Path,
    history: Path | None,
) -> bool:
    """Write ``inputs.json`` for a run. Returns whether it landed; never raises.

    Called before the first live write, for the reason the log path is announced up front: a run
    that dies part-way has still said what it consumed, and that is exactly when somebody needs to
    know — live pages and permanent records may already exist for the rows that landed.
    """
    known = read(history)
    sources: dict[str, SourceRef] = {}
    read_from: tuple[tuple[What, Path], ...] = (
        ("selection", selection),
        ("product-list", product_list),
        ("export", export),
    )
    for what, live in read_from:
        found = resolve(live, known, what)
        if found is not None:
            sources[what] = found
    payload = RunInputs(at=datetime.now(UTC), mode=mode, dry_run=dry_run, sources=sources)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def read_run(path: Path) -> RunInputs | None:
    """What a run read, or ``None`` — a legacy run has no such file, which is not an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("v") not in _READABLE:
        return None
    try:
        return RunInputs.model_validate(data)
    except ValidationError:
        return None
