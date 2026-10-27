"""What the screens read: config facts, file freshness, plan and run artifacts.

Read-only, and every function tolerates the file being absent — a shell that raises before the
operator has run anything is a shell that cannot help them run it. "Not there yet" is a state to
display, not an error.

Nothing here loads ``state.json``, for the reason :mod:`lib.preflight` gives: an idle read of a
corrupt one quarantines it (E19), and looking at the system must not change what the next run does.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.batch import Batch, in_force
from lib.config import ClientConfig, ProcessListConfig, get_client, resolve_client_id
from lib.errors import ConfigError, ProcessListError
from lib.gates import Mode
from lib.input_layout import archive_path
from lib.media_video import canon_gtin
from lib.process_list import load_process_list
from lib.provenance import history_path, read
from lib.records import Plan, PlanSummary, ProductRecord, RunOutcome, State
from lib.run_files import iter_logs, newest_log, stamp_of
from ui import DATA_ROOT, progress


@dataclass(frozen=True)
class FileFact:
    """A path, whether it is there, and how stale it is.

    Age is shown wherever a file is named because gate 0's export cross-check is exactly this
    question — "is this the file you mean?" — and a modification date answers it faster than a
    path does.
    """

    path: Path
    exists: bool
    modified: datetime | None
    size: int

    @property
    def age(self) -> str:
        """ "12 days ago", or "missing"."""
        return age_of(self.modified if self.exists else None)


def age_of(modified: datetime | None) -> str:
    """ "today" / "yesterday" / "12 days ago", or "missing".

    A function as well as a property because the batch panel has the timestamp without the path —
    ``lib.batch`` cannot import this module, and a second way of spelling "12 days ago" on the same
    screen as the first is how the two come to disagree by a day.
    """
    if modified is None:
        return "missing"
    days = (datetime.now(UTC) - modified).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def file_fact(path: str | Path) -> FileFact:
    """Describe a path without reading it."""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = DATA_ROOT / resolved
    try:
        stat = resolved.stat()
    except OSError:
        return FileFact(resolved, exists=False, modified=None, size=0)
    return FileFact(
        resolved,
        exists=True,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        size=stat.st_size,
    )


def client_id() -> str | None:
    """The single configured client, or ``None`` when the config cannot say which."""
    try:
        return resolve_client_id(None)
    except ConfigError:
        return None


def client_config(cid: str | None) -> ClientConfig | None:
    """The client's config, or ``None`` when it will not load — the Setup screen says why."""
    try:
        return get_client(cid)
    except (ConfigError, OSError):
        return None


def is_production(cfg: ClientConfig) -> bool:
    """Whether this client's GS1 environment is the permanent one."""
    return cfg.gs1.environment == "production"


def output_dir(cid: str) -> Path:
    return DATA_ROOT / "output" / cid


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_plan(cid: str) -> Plan | None:
    """The last plan, or ``None``."""
    data = _load_json(output_dir(cid) / "plan.json")
    if data is None:
        return None
    try:
        return Plan.model_validate(data)
    except ValueError:
        return None


def load_plan_summary(cid: str) -> PlanSummary | None:
    """The last plan's summary — counts, exclusions, the E19 flag, and the stderr line verbatim."""
    data = _load_json(output_dir(cid) / "plan.summary.json")
    if data is None:
        return None
    try:
        return PlanSummary.model_validate(data)
    except ValueError:
        return None


def product_count(cid: str) -> int | None:
    """How many products the parsed catalogue holds, or ``None`` when it has not been parsed."""
    data = _load_json(output_dir(cid) / "data" / "products.json")
    return len(data) if isinstance(data, list) else None


def doctor_check(payload: Any, name: str) -> dict[str, Any] | None:
    """One named check out of ``scripts.doctor --json``, or ``None``.

    The payload is whatever the subprocess printed, so it may not be a list at all — a crashed
    command still says something, and every caller here would rather show that than raise.
    """
    if not isinstance(payload, list):
        return None
    return next((entry for entry in payload if entry.get("name") == name), None)


def live_counts(payload: Any) -> dict[str, int] | None:
    """The three figures from ``scripts.report_live_copy``, or ``None`` when it said nothing.

    ``None`` rather than zeroes. Zero products needing text and a report that failed to run look
    identical as numbers and mean opposite things, and the screen must be able to say which — a
    green "nothing to do" built on a crashed subprocess is the failure this whole project is
    arranged against.
    """
    counts = payload.get("counts") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        return None
    wanted = ("in_scope", "has_text", "needs_text", "no_inputs")
    if not all(isinstance(counts.get(key), int) for key in wanted):
        return None
    return {key: int(counts[key]) for key in wanted}


def live_products(payload: Any, bucket: str) -> list[dict[str, Any]]:
    """The products in one bucket, in the order the report listed them."""
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return []
    return [
        product
        for product in products
        if isinstance(product, dict) and product.get("bucket") == bucket
    ]


def live_gtins(payload: Any, bucket: str) -> list[str]:
    """Just the GTINs of one bucket — what a generate command is built from."""
    return [str(product.get("gtin", "")) for product in live_products(payload, bucket)]


def live_checked_at(payload: Any) -> str:
    """When the site was last asked, in minutes rather than in ISO-8601.

    The stamp exists because this answer goes stale — somebody publishes, and the figures on
    screen describe a site that has moved. "2026-09-14T14:48:18+00:00" is precise and useless for
    that: an operator cannot subtract it from now at a glance, so a check from this morning reads
    exactly like one from a moment ago.
    """
    stamp = payload.get("checked_at") if isinstance(payload, dict) else None
    if not stamp:
        return "just now"
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return str(stamp)
    minutes = int((datetime.now(UTC) - when).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes == 1:
        return "a minute ago"
    if minutes < _MINUTES_IN_HOUR:
        return f"{minutes} minutes ago"
    hours = minutes // _MINUTES_IN_HOUR
    return "an hour ago" if hours == 1 else f"{hours} hours ago"


#: Where "N minutes ago" stops reading as a number and starts reading as a duration.
_MINUTES_IN_HOUR = 60


def copy_summary(entry: dict[str, Any] | None) -> str | None:
    """The coverage check in the operator's words, or ``None`` when it cannot be read.

    The check itself reports in **units** — one product in one language — because that is the
    plan's unit of work and every row count beside it is in the same currency. It is the right
    word for the figures and the wrong word for the sentence: an operator reading "46 held by the
    plan" has to know that a unit is half a product before the number means anything, and then
    still has to work out that the thing to go and fix is a product.

    So this says both, each where it belongs: the work in **pages** (a unit *is* a page, and that
    is the word the operator already uses for what this publishes), and everything excluded from
    the work in **products**, split by who unblocks it — a video to confirm, or data to fix in
    MyGS1. Those two never merge into one "blocked" figure, for the same reason ``_excluded_aside``
    keeps unchanged and held apart: they go to different people.

    Returns ``None`` rather than a guess when the payload has no usable figures, so the caller can
    fall back to the check's own detail instead of printing a sentence built from nothing.
    """
    data = (entry or {}).get("data") or {}
    total, pending = data.get("total"), data.get("pending")
    if not isinstance(total, int) or not isinstance(pending, int):
        return None
    if total == 0:
        lead = "Nothing to write — no page in this batch needs new text."
    elif pending == 0:
        lead = f"Ready — all {total} page(s) this run publishes have their text."
    else:
        lead = (
            f"{pending} of the {total} page(s) this run publishes have no text yet. Those pages "
            "would be left out of the run, so generate again before publishing."
        )
    return lead + _excluded_products(data)


def _excluded_products(data: dict[str, Any]) -> str:
    """Why the batch is bigger than the work: what is finished, and what is blocked on whom."""
    unchanged = data.get("products_unchanged")
    video = data.get("products_held_video")
    source = data.get("products_held_data")
    if not all(isinstance(value, int) for value in (unchanged, video, source)):
        return ""
    parts = []
    if unchanged:
        parts.append(f"{unchanged} product(s) in this batch are already up to date")
    blocked = [
        text
        for text, count in (
            (f"{video} need a confirmed video", video),
            (f"{source} need data fixed in MyGS1", source),
        )
        if count
    ]
    if blocked:
        held = (video or 0) + (source or 0)
        parts.append(f"{held} are blocked ({', '.join(blocked)})")
    return f" {' and '.join(parts)}." if parts else ""


@dataclass(frozen=True)
class Scope:
    """What a run would touch, as the doctor's ``scope`` check reports it.

    Read from the doctor rather than recomputed here, and that is the point: ``lib.preflight``
    already composes the two gates that decide scope — the process list, then the confirmed-video
    allowlist behind ``media.restrict_to_mapped_gtins`` — and a second implementation of "what
    will this run touch" is the same class of mistake as a second implementation of the gates.

    ``in_scope`` is deliberately a **superset** of what ``run_plan`` will classify: it omits the
    already-published drop, because deciding that needs ``state.json`` and an idle read of a
    corrupt one quarantines it (E19). So this is the ceiling on what a run could touch, never a
    promise of how many rows it will write — that number arrives at the plan gate.
    """

    in_scope: int
    total: int
    #: The doctor's sentence, verbatim, naming what removed the rest.
    detail: str
    #: The check failed: nothing is in scope, so a run would publish nothing and report success.
    empty: bool
    #: The in-scope GTINs, as ``ProductRecord.gtin`` — the same field the generated-copy cache is
    #: keyed by, so a screen can filter cache entries down to this run without renormalising.
    #: Empty when the doctor predates this field; callers must treat that as "scope unknown"
    #: rather than as "nothing is in scope".
    gtins: frozenset[str]


def batch_scope(cid: str, cfg: ClientConfig) -> Scope | None:
    """The saved batch as a :class:`Scope` — the products Next saved on Data, nothing else.

    ``None`` when there is no list or it will not read. Asked of the files rather than of the
    doctor because the screen that needs it (Content's review) runs before the preflight does,
    and passing it nothing made the review show every product the results file had ever held.
    """
    if cfg.process_list is None:
        return None
    try:
        named = load_process_list(
            ProcessListConfig(
                path=str(_resolved(cfg.process_list.path)),
                gtin_column=cfg.process_list.gtin_column,
            )
        )
    except ProcessListError:
        return None
    products = load_products(cid)
    gtins = frozenset(product.gtin for product in products if product.gtin14 in named)
    return Scope(in_scope=len(gtins), total=len(products), detail="", empty=not gtins, gtins=gtins)


def scope_from(payload: Any) -> Scope | None:
    """Read the doctor's ``scope`` check, or ``None`` when it did not report one.

    ``None`` is a state to display, not a reason to fall back on the catalogue count. Showing the
    catalogue total under a label that says "in scope" would be the defect this replaces, wearing
    the right words.
    """
    entry = doctor_check(payload, "scope")
    if entry is None:
        return None
    data = entry.get("data") or {}
    in_scope, total = data.get("in_scope"), data.get("total")
    if not isinstance(in_scope, int) or not isinstance(total, int):
        return None
    gtins = data.get("in_scope_gtins")
    return Scope(
        in_scope=in_scope,
        total=total,
        detail=str(entry.get("detail") or ""),
        empty=entry.get("status") == "fail",
        gtins=frozenset(g for g in gtins if isinstance(g, str))
        if isinstance(gtins, list)
        else frozenset(),
    )


@dataclass(frozen=True)
class ResultsSplit:
    """A results file divided into this run's units and everything else.

    The file is written per run rather than accumulated, so ``others`` no longer means "copy from
    older batches this machine kept". It now means the file was produced against a **different
    scope** than the one about to run — which is a stronger signal than the old accumulation was,
    and the screen says so rather than folding it away as normal.
    """

    #: Copy for GTINs this run would touch.
    in_scope: dict[str, Any]
    #: Copy for everything else — written for a scope that is not this one.
    others: dict[str, Any]
    #: In-scope GTINs with no copy at all, sorted.
    missing: tuple[str, ...]
    #: Whether the split actually happened. ``False`` means scope was unknown, so ``in_scope``
    #: holds the whole file unfiltered and a caller must say so rather than present it as
    #: the batch.
    scoped: bool


def group_results(results: list[Any]) -> dict[str, dict[str, Any]]:
    """Group a results file's flat item list into ``{gtin: {language: item}}``.

    The file is a list because that is what a producer writes one entry at a time; a screen reads
    it per product. Items that are not objects, or carry no gtin, are dropped rather than raising:
    this runs against a file a human may have hand-edited.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        gtin, language = item.get("gtin"), item.get("language")
        if isinstance(gtin, str) and isinstance(language, str):
            grouped.setdefault(gtin, {})[language] = item
    return grouped


def text_written_for(
    gtins: Collection[str], entries: dict[str, dict[str, Any]], languages: Collection[str]
) -> bool:
    """Whether every one of ``gtins`` has text — a non-empty tagline list — in every language.

    What unlocks Content's review and its Next: the products the site says need text either got it
    this visit or already had it from an earlier Generate, which should not cost a second one.
    Barcodes are compared at 14 digits, because the live report and the results file are keyed by
    two different fields that can spell the same product differently.
    """
    written = {canon_gtin(gtin): per_language for gtin, per_language in entries.items()}
    return all(
        all(
            isinstance(entry := written.get(canon_gtin(gtin), {}).get(language), dict)
            and entry.get("usps")
            for language in languages
        )
        for gtin in gtins
    )


def split_results(entries: dict[str, dict[str, Any]], scope: Scope | None) -> ResultsSplit:
    """Divide this run's copy into the GTINs it covers and the rest.

    Membership is a plain set test against :attr:`Scope.gtins`, which the doctor reports as
    ``ProductRecord.gtin`` — the same field the results are keyed by. Nothing is renormalised here,
    deliberately: a second opinion about how a GTIN is spelled is a second opinion about what a
    run covers.

    An unknown scope returns everything as ``in_scope`` with ``scoped=False`` rather than an
    empty split. Filtering to nothing would hide the copy entirely and read as "there is none",
    which is wrong in the direction that stops an operator looking.
    """
    if scope is None or not scope.gtins:
        return ResultsSplit(in_scope=dict(entries), others={}, missing=(), scoped=False)
    return ResultsSplit(
        in_scope={gtin: value for gtin, value in entries.items() if gtin in scope.gtins},
        others={gtin: value for gtin, value in entries.items() if gtin not in scope.gtins},
        missing=tuple(sorted(scope.gtins - set(entries))),
        scoped=True,
    )


def load_ledger(cid: str) -> State:
    """What this tool has published for ``cid`` (``state.json``), read without touching it.

    Empty when absent or unreadable: the one caller asks it which products already have a page of
    ours to point a GS1 record at, and an unreadable ledger then means "none known" — the run's own
    check still stands between that and a write.
    """
    data = _load_json(output_dir(cid) / "state.json")
    try:
        return State.model_validate(data)
    except ValueError:
        return State(client_id=cid, entries={})


def load_products(cid: str) -> list[ProductRecord]:
    """The parsed catalogue, or an empty list when it is absent or unreadable.

    Empty rather than ``None``: the one screen that reads this uses it for fuzzy *suggestions*,
    so an unparsed catalogue should cost the suggestions and nothing else.
    """
    data = _load_json(output_dir(cid) / "data" / "products.json")
    if not isinstance(data, list):
        return []
    try:
        return [ProductRecord.model_validate(item) for item in data]
    except ValueError:
        return []


@dataclass(frozen=True)
class RunLog:
    """One run's JSONL, as far as it got.

    ``partial`` is the point of reading it this way. The log is appended row by row as the run
    goes, so a file that stops mid-way is a run that stopped mid-way — and that is exactly the
    case an operator most needs to see, because live pages and permanent records may already
    exist for the rows that did land.
    """

    path: Path
    outcomes: list[RunOutcome]
    modified: datetime | None
    unreadable_lines: int

    @property
    def stamp(self) -> str:
        """What to call this run on screen.

        Not ``path.name``: a run's log is ``{ts}/run.jsonl`` now, so every run would be labelled
        "run.jsonl" and the list would read as one run repeated twenty times. The older flat
        ``{ts}.jsonl`` logs are still on disk and still have to come out as the same name they
        always did.
        """
        return stamp_of(self.path)

    @property
    def ok(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "ok")

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    @property
    def dry_run(self) -> bool:
        return bool(self.outcomes) and all(o.status == "dry-run" for o in self.outcomes)


def load_run(path: Path) -> RunLog:
    """Read one run log, keeping the rows that parse and counting the ones that do not.

    A truncated final line is normal for a run killed mid-write, and discarding the whole file
    over it would throw away the record precisely when it matters most.
    """
    outcomes: list[RunOutcome] = []
    unreadable = 0
    try:
        text = path.read_text(encoding="utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return RunLog(path, [], None, 0)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            outcomes.append(RunOutcome.model_validate_json(line))
        except ValueError:
            unreadable += 1
    return RunLog(path, outcomes, modified, unreadable)


def recent_runs(cid: str, limit: int = 20) -> list[RunLog]:
    """The most recent run logs, newest first.

    Sorted by modification time rather than by name: a same-second second run is named
    ``{ts}-1.jsonl``, which sorts *before* ``{ts}.jsonl`` because ``-`` precedes ``.``.
    """
    paths = sorted(iter_logs(cid), key=lambda p: p.stat().st_mtime, reverse=True)
    return [load_run(path) for path in paths[:limit]]


def _newest_run(cid: str) -> Path | None:
    """The most recent run log's path, without reading any of them."""
    try:
        return newest_log(cid)
    except OSError:
        return None


def rail_facts(cid: str | None, cfg: ClientConfig | None) -> dict[str, str]:
    """One short fact per rail entry: "have I done this yet?", answerable from any screen.

    **Facts, never ticks.** A green tick on Data because *an* export exists cannot tell you it is
    the *right* export, and a tick that lies is worse than no tick — the same reasoning that put
    scope rather than the catalogue count on gate 0. A date and a row count can be checked against
    what the operator believes; a checkmark can only be trusted or not.

    **Everything here must stay stat-cheap.** This runs on every render of every screen, so one
    subprocess would put a quarter-second on all seven — which is why the counts an operator
    really wants (units with copy, checks passing) are *not* here: they need the doctor, and they
    are already on the screens that own them. ``plan.summary.json`` is read because it is a small
    file written for exactly this, not because reading files is free.

    Preflight has no entry. It leaves no artifact, and there is no cheap way to say when it last
    ran — an empty fact is better than a misleading one.
    """
    if cid is None or cfg is None:
        return {}
    facts = {
        "Data": file_fact(cfg.export.path).age,
        "Content": file_fact(output_dir(cid) / "data" / "generation_results.json").age,
    }
    if (summary := load_plan_summary(cid)) is not None:
        facts["Publish"] = f"{summary.total} row{'' if summary.total == 1 else 's'}"
    newest = _newest_run(cid)
    facts["Runs"] = file_fact(newest).age if newest else "none yet"
    return facts


def mode_from(value: str | None) -> Mode:
    """Parse a mode name, defaulting to the least destructive one.

    Defaulting to ``pages`` rather than ``both`` is deliberate: an unreadable or absent choice
    must never resolve toward the mode that writes permanent records.
    """
    try:
        return Mode(value or "")
    except ValueError:
        return Mode.PAGES


# --- The batch in force ------------------------------------------------------------------------


def _resolved(path: str) -> Path:
    """A configured path against the data folder. Every path in ``clients.yml`` is relative."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else DATA_ROOT / candidate


def locked_steps(cid: str | None, cfg: ClientConfig | None) -> set[str]:
    """The steps the rail draws without a link — none while the config will not load, the same
    exemption as the route guard in :mod:`ui.app`."""
    return set() if cfg is None else progress.of(cid).locked()


def batch_in_force(cfg: ClientConfig) -> Batch | None:
    """Which two files a run would use, cached for as long as they do not change.

    ``None`` for a client with no ``process_list`` block, which plans every product and so has no
    selection to describe.

    **Cached, and keyed on what the files actually are.** This runs on four screens and costs two
    workbook parses plus three digests — a fifth of a second on a real export, which is why
    ``rail_facts`` deliberately carries none of it and stays stat-cheap. The key is every input's
    ``(mtime, size)``, so a save or an upload invalidates it by changing the thing it describes
    rather than by anybody remembering to clear anything.
    """
    if cfg.process_list is None:
        return None
    export = _resolved(cfg.export.path)
    selection = _resolved(cfg.process_list.path)
    product_list = archive_path(selection)
    history_file = history_path(export)

    key = (
        str(selection),
        _stamp_of(export),
        _stamp_of(selection),
        _stamp_of(product_list),
        _stamp_of(history_file),
    )
    cached = _BATCHES.get(str(selection))
    if cached is not None and cached[0] == key:
        return cached[1]

    batch = in_force(
        export=export,
        selection=selection,
        product_list=product_list,
        history=read(history_file),
        gtin_column=cfg.process_list.gtin_column,
        products=product_count(cfg.client_id),
    )
    _BATCHES[str(selection)] = (key, batch)
    return batch


def batch_mode(cfg: ClientConfig | None) -> Mode | None:
    """What the saved batch publishes, as chosen on the Data screen — or ``None`` when no save of
    the selection in force recorded it. Read from disk, like the rest of the batch."""
    if cfg is None:
        return None
    batch = batch_in_force(cfg)
    return batch.mode if batch is not None else None


def _stamp_of(path: Path | None) -> tuple[float, int] | None:
    """A file's identity for cache purposes: when it changed and how big it is."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


#: One entry per client, for the life of the process. Keyed by the live selection's path so a
#: repo with two clients configured cannot serve one's batch for the other.
_BATCHES: dict[str, tuple[tuple[object, ...], Batch]] = {}
