"""Eager, legible checks that a client is ready to run — the pure half of ``scripts/doctor``.

Every failure this module reports was previously a failure the operator met *late*. A missing
secret surfaced as :class:`~lib.errors.MissingCredentialError` at the first API call (E15), so
parse, plan and dry-run could all pass before it fired. A blank ``clients.yml`` field surfaced
as the first pydantic error and only the first. Generated copy that no longer matched the export
surfaced not at all: those units simply vanished from the plan (E21). The point of a preflight
is to move each of those to a moment where nothing is half-done and nothing is permanent.

Every check here is a function from configuration to a :class:`CheckResult`. None of them print,
none of them exit, and none of them write anything the pipeline reads — so a UI, a test, and the
CLI can all run the same checks and disagree only about how to display them.

**Nothing here calls** :func:`lib.state.load_state`. An idle peek at a corrupt state file
*quarantines* it (E19) — a side effect no diagnostic should have, and one that would make merely
looking at the system change what the next run does.

Two checks earn their place by guarding traps that are otherwise silent:

* :func:`check_generator` catches a ``generator`` block removed as unused cleanup. ``run_plan``
  derives ``require_generated_copy = cfg.generator is not None``, so deleting the block does not
  raise — it disables the E21 guard, and units with no copy publish with blank taglines instead
  of being held.
* :func:`check_generation_results` catches a run whose generated copy is missing or no longer
  matches the export. Copy is written fresh every run and never stored, so the question is not
  how much has accumulated but whether the file on disk answers every unit this run would write
  (:func:`units_needing_copy`) — and whether it still describes this export. The fingerprint covers
  ``{inputs, language, prompt_version}``, so any feed edit or version bump leaves those units
  uncovered, and an uncovered unit is an E21 omission, which is to say invisible.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import jsonschema

from lib import client_files
from lib.batch import Chosen, in_force
from lib.categories import assign_categories, coverage_report
from lib.committed_config import committed_path, matches
from lib.config import DEFAULT_CLIENTS_PATH, ClientConfig, get_client, load_clients
from lib.data_dir import CODE_ROOT
from lib.errors import (
    ConfigError,
    ExportParseError,
    GS1APIError,
    MissingCredentialError,
    OrchestratorError,
    ProcessListError,
    StateError,
    VideoMapError,
    WordPressAPIError,
)
from lib.gates import Mode
from lib.generator import generation_context, load_results, missing_copy
from lib.gs1_dl_client import GS1DigitalLinkClient
from lib.holds import held_units, video_gate_for
from lib.input_layout import archive_path
from lib.media_video import (
    VideoMapSummary,
    canon_gtin,
    check_video_map,
    files_by_language,
    load_video_map,
    summarize_video_map,
    video_gate,
)
from lib.process_list import load_process_list
from lib.provenance import history_path, read
from lib.records import ProductRecord, SkipReason
from lib.state import WILL_BE_WRITTEN, classify_units, keeps_live_copy, peek_state
from lib.video_status import VideoStatus, video_status
from lib.wp_client import WordPressClient, WordPressIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Where ``scripts/parse_export.py`` writes the normalised products.
_PRODUCTS_RELATIVE: Final = Path("data") / "products.json"

#: WordPress roles that can publish. A credential that authenticates but holds none of these
#: passes a naive check and then fails mid-run, after some rows are already live.
_PUBLISHING_ROLES: Final = frozenset({"administrator", "editor"})

#: A WordPress application password is issued as six space-separated groups. Fewer almost
#: always means the value lost its quotes in ``.env`` and was truncated at the first space.
_APP_PASSWORD_GROUPS: Final = 6

#: GS1 v2 error code for "no valid contract found" — the hard blocker that no code or config
#: change can fix, because the Digital Link contract is provisioned by GS1 on the account.
_NO_CONTRACT_CODE: Final = "21011"

#: How much of an error body to quote back. Enough to identify it, short enough to read.
_ERROR_BODY_CHARS: Final = 300


class Status(StrEnum):
    """How a check came out.

    ``NA`` is not a pass. A check that does not apply — no ``categories`` block, no video
    mapping — has proved nothing, and reporting it as ``OK`` would let an operator read a
    green line as evidence about a thing that was never looked at.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    NA = "n/a"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict.

    ``remedy`` is separate from ``detail`` on purpose: the detail says what is true, the
    remedy says what to do about it, and an operator who is not an engineer needs the second
    one more. It is empty when there is nothing to do.
    """

    name: str
    title: str
    status: Status
    detail: str
    remedy: str = ""
    data: dict[str, object] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Whether this check should stop a run."""
        return self.status is Status.FAIL


# --- Offline checks ----------------------------------------------------------


def check_config(path: str | Path = DEFAULT_CLIENTS_PATH) -> CheckResult:
    """Validate ``clients.yml`` against the JSON Schema, reporting **every** field at fault.

    Schema errors are collected with ``jsonschema.iter_errors`` rather than left to
    :func:`lib.config.load_clients`, which raises on the first one and discards the
    ``json_path`` — so an operator with four blank fields fixes them one run at a time,
    learning about the next only after correcting the last.

    Pydantic validation still runs afterwards (it enforces cross-field rules the schema
    cannot express); its first error is reported as-is, since by then the shape is sound.
    """
    path = Path(path)
    if not path.is_file():
        return CheckResult(
            "config",
            "Configuration file",
            Status.FAIL,
            f"{path} does not exist",
            remedy="Copy clients.example.yml to clients.yml and fill in your client's block.",
        )

    schema_errors = list(_schema_errors(path))
    if schema_errors:
        return CheckResult(
            "config",
            "Configuration file",
            Status.FAIL,
            f"{path} has {len(schema_errors)} schema error(s): " + "; ".join(schema_errors),
            remedy="Fix each field named above. clients.example.yml shows a working value.",
            data={"errors": schema_errors},
        )

    try:
        clients = load_clients(path)
    except (ConfigError, ExportParseError) as exc:
        return CheckResult(
            "config",
            "Configuration file",
            Status.FAIL,
            f"{path} failed validation: {exc}",
            remedy="Fix the field named above.",
        )
    return CheckResult(
        "config",
        "Configuration file",
        Status.OK,
        f"{path} is valid; {len(clients)} client(s) defined: {', '.join(sorted(clients))}",
        data={"clients": sorted(clients)},
    )


def _schema_errors(path: Path) -> Iterator[str]:
    """Yield every JSON Schema violation in ``path`` as ``"<json path>: <message>"``."""
    import yaml  # noqa: PLC0415 — local, so importing this module stays cheap for a UI

    schema_path = Path(__file__).resolve().parent.parent / "schema" / "clients.schema.json"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        yield f"$: cannot be read as YAML ({exc})"
        return
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        yield f"{error.json_path}: {error.message}"


def check_generator(cfg: ClientConfig) -> CheckResult:
    """Guard the E21 trap: a ``generator`` block deleted because it "looked unused".

    ``run_plan`` derives ``require_generated_copy = cfg.generator is not None``. Removing the
    block therefore does not raise — it silently turns off the check that holds a copy-less
    unit back, and those units publish with blank taglines instead.

    This matters most on a machine that has no API key and never generates anything, where the
    block genuinely *looks* like dead configuration. It is not: it is the switch.

    An absent block is only reported as a failure when generated copy exists for this client,
    which is proof that a generator was configured once. With no copy and no block, the client
    simply has no generator and E21 does not apply.
    """
    if cfg.generator is not None:
        return CheckResult(
            "generator_block",
            "Generator block (E21 guard)",
            Status.OK,
            f"present (prompt_version {cfg.generator.prompt_version}); "
            "a new page with no generated copy is held out of the plan; a live one keeps its text",
        )
    if _generated_unit_count(cfg.client_id):
        return CheckResult(
            "generator_block",
            "Generator block (E21 guard)",
            Status.FAIL,
            "clients.yml has no `generator` block, but generated copy exists for this client — "
            "so one was configured before. Without it run_plan sets "
            "require_generated_copy=False and a unit with no copy publishes a blank tagline "
            "instead of being held.",
            remedy="Restore the `generator:` block in clients.yml. It is required even on a "
            "machine with no API key, which never generates anything: it is the E21 switch, "
            "not a credential.",
        )
    return CheckResult(
        "generator_block",
        "Generator block (E21 guard)",
        Status.NA,
        "no `generator` block and no generated copy — this client does not use generated "
        "copy, so E21 does not apply",
    )


def in_scope(cfg: ClientConfig, products: list[ProductRecord]) -> list[ProductRecord]:
    """The products a run could touch, decided from configuration alone.

    Composes the same two gates ``run_plan`` applies — the process list, then the
    confirmed-video allowlist behind ``media.restrict_to_mapped_gtins`` — out of the same
    ``lib`` primitives, so a check reports on the work the operator actually asked for rather
    than on the whole catalogue. Without this, a client whose export holds 127 products but
    whose process list names 38 gets told about 224 missing cache entries it will never need,
    and learns to ignore the report.

    This is deliberately a **superset** of ``run_plan``'s scope: it omits the "already
    published" drop, because deciding that needs ``state.json``, and an idle read of a corrupt
    state file quarantines it (E19). Erring wide is the safe direction — a preflight may
    mention a unit that turns out to be finished; it must never stay silent about one that is
    not.
    """
    scoped = products
    if cfg.process_list is not None:
        try:
            listed = load_process_list(cfg.process_list)
        except ProcessListError:
            return scoped  # check_process_list reports this; do not fail twice over it
        scoped = [product for product in scoped if product.gtin14 in listed]
    # Deliberately no video narrowing here. A GTIN without a confirmed video is *in scope and
    # held* (E24), not out of scope: it is a product the operator asked for and cannot yet have,
    # which is a different fact from one they never asked about, and only the first is actionable.
    # Narrowing here made it invisible on every surface at once — this figure, the plan, and the
    # quality report — so the missing video looked like nothing rather than like work.
    return scoped


def held_for_video(cfg: ClientConfig, scoped: list[ProductRecord]) -> list[ProductRecord]:
    """Which of ``scoped`` E24 will hold — what :class:`lib.media_video.VideoGate` does not admit.

    No confirmed video in every language, unless ``media.publish_without_video``; two confirmed
    files in one language, always. Composed from the same gate ``run_plan`` holds on rather than
    re-deciding what "confirmed" means. A second opinion about that is the
    mistake :func:`in_scope` exists to prevent.

    Empty when the rule is off, when there is no mapping, or when the mapping will not load. The
    last is deliberate: :func:`check_video_coverage` reports an unreadable mapping properly, this
    check must not fail a second time over it, and a hold count that could not be computed must
    not be presented as zero.
    """
    media = cfg.media
    if media is None or not media.restrict_to_mapped_gtins or not media.video_map_path:
        return []
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError:
        return []
    gate = video_gate(
        vmap, list(cfg.wordpress.languages), publish_without_video=media.publish_without_video
    )
    return [product for product in scoped if not gate.admits(product.gtin)]


def without_video(cfg: ClientConfig, scoped: list[ProductRecord]) -> list[ProductRecord]:
    """Which of ``scoped`` will publish with no video in at least one language.

    Non-empty only under ``media.publish_without_video``: these are the products the gate admits
    although they are not confirmed in every language. Every surface that says what a run
    publishes says this beside it, because a page without its video looks finished — and the
    count of pages, which is what every other figure is, cannot tell the two apart.

    Empty when the rule is off, when there is no mapping, or when it will not load — the same
    three cases, for the same reasons, as :func:`held_for_video`.
    """
    media = cfg.media
    if (
        media is None
        or not media.restrict_to_mapped_gtins
        or not media.publish_without_video
        or not media.video_map_path
    ):
        return []
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError:
        return []
    gate = video_gate(vmap, list(cfg.wordpress.languages), publish_without_video=True)
    return [
        product
        for product in scoped
        if gate.admits(product.gtin) and canon_gtin(product.gtin) not in gate.confirmed
    ]


def load_video_status(cfg: ClientConfig, scoped: list[ProductRecord]) -> VideoStatus | None:
    """``scoped`` joined to the client's video mapping and folders — or ``None`` with no mapping.

    ``None`` for no ``media`` block, no ``video_map_path``, or a mapping that will not load: the
    three cases every surface already treats as "nothing to say about video here", and
    :func:`check_video_coverage` is where an unreadable mapping is reported. The join itself is
    :func:`lib.video_status.video_status`, which is pure; this is only its loader, shared by the
    report and the Data screen so neither re-decides what the mapping says.

    :func:`held_for_video` is deliberately left as it was: ``report_live_copy`` and
    :mod:`lib.live_copy` depend on its signature, and it is the doctor's vocabulary.
    """
    media = cfg.media
    if media is None or not media.video_map_path:
        return None
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError:
        return None
    return video_status(
        vmap,
        scoped,
        languages=cfg.wordpress.languages,
        files_by_language=files_by_language(media.video_folders),
        publish_without_video=media.restrict_to_mapped_gtins and media.publish_without_video,
    )


def check_scope(
    cfg: ClientConfig, products: list[ProductRecord], *, links_only: bool = False
) -> CheckResult:
    """State plainly how many products a run would touch, and what removed the rest.

    ``links_only``: the batch writes GS1 links only, which the video rule does not hold — so it
    is not mentioned, rather than reported as a hold the run will not apply.

    The number an operator most needs and is least often given. "38 GTINs on the list" and
    "15 GTINs a run can actually publish" are very different facts, and every gate between
    them is silent by design — a filtered-out product produces no error, no row and no count.
    """
    if not products:
        return CheckResult(
            "scope",
            "What a run would touch",
            Status.WARN,
            "no parsed products",
            remedy="Run `python -m scripts.parse_export` first.",
        )
    scoped = in_scope(cfg, products)
    # Only the narrowings `in_scope` actually performs. It named `media.restrict_to_mapped_gtins`
    # here too, which `in_scope` has never applied and must not — see its comment for why. So the
    # sentence promised a filter the figure had not been through: on the pilot it read "37 in
    # scope, after the process list and the video rule" while 20 of those 37 were held and only 17
    # could publish. Gate 0 renders this string verbatim, which is where an operator forms their
    # picture of the run, and the missing 20 produce no error, no row and no count.
    reasons = []
    if cfg.process_list is not None:
        reasons.append(f"process list ({cfg.process_list.path})")
    detail = f"{len(scoped)} of {len(products)} product(s) in the export are in scope"
    if reasons:
        detail += ", after " + " and ".join(reasons)
    # The video rule is reported as what it is — a hold on products that are in scope — rather
    # than as a narrowing that already happened.
    held = [] if links_only else held_for_video(cfg, scoped)
    bare = [] if links_only else without_video(cfg, scoped)
    publishes_bare = (
        cfg.media is not None
        and cfg.media.restrict_to_mapped_gtins
        and cfg.media.publish_without_video
    )
    if held:
        why = (
            "two confirmed videos in one language"
            if publishes_bare
            else "want of a confirmed video in every language"
        )
        detail += (
            f". {len(held)} of those are held for {why} (media.restrict_to_mapped_gtins), so a "
            f"run would publish {len(scoped) - len(held)}"
        )
    if bare:
        detail += (
            f". {len(bare)} would publish with no video in at least one language "
            "(media.publish_without_video)"
        )
    data: dict[str, object] = {
        "in_scope": len(scoped),
        "total": len(products),
        # The GTINs themselves, not only the count, so a consumer can *filter* by scope rather
        # than merely report it. The Content screen lists cached copy and must show this run's
        # units rather than everything the cache has ever accumulated; without the list it would
        # have to re-derive scope, and a second implementation of "what will this run touch" is
        # the mistake `in_scope` exists to prevent.
        #
        # ``ProductRecord.gtin`` verbatim — the same field the generator keys its results by
        # (``(gtin, language)``). A normalised variant here would silently fail to match for any
        # client whose feed carries 13-digit codes, and the failure would look like "nothing is
        # in scope" rather than like a bug.
        #
        # Deliberately uncapped, unlike ``generation_results``'s ``pending_units``: that is a
        # list to read, this is a list to filter with, and a truncated filter hides in-scope
        # work — the exact failure this data is here to fix.
        "in_scope_gtins": [product.gtin for product in scoped],
    }
    if not scoped:
        return CheckResult(
            "scope",
            "What a run would touch",
            Status.FAIL,
            detail + " — a run would publish nothing and report success",
            remedy="Check that the process list names GTINs that are in the export, and that "
            "the gates above are not excluding all of them.",
            data=data,
        )
    return CheckResult(
        "scope",
        "What a run would touch",
        Status.OK,
        detail + f". Every check below reports on those {len(scoped)}, not on the whole export.",
        data=data,
    )


def units_needing_copy(
    cfg: ClientConfig, scoped_products: list[ProductRecord], *, include_live: bool = False
) -> set[tuple[str, str]] | None:
    """The ``(GTIN, language)`` units this run would create or change — the ones needing copy.

    The companion to :func:`in_scope`, one question further on. ``in_scope`` says which products a
    run could touch from configuration alone; this says which *units* it would actually write,
    which needs the classification and therefore ``state.json``. Copy is written per run for those
    units only: an UNCHANGED row is never confirmed and never executed, so copy for it is text
    nothing will read. ``run_generate`` narrows the producer to this set and
    :func:`check_generation_results` counts over it, so the two cannot disagree about what a run
    owes.

    **Two narrowings, not one.** A unit is left out when it classifies UNCHANGED, *and* when the
    plan will hold its product whatever a producer writes — :func:`lib.holds.held_units`, which is
    E23/E24/E22 asked with the plan's own predicates. Only the first was applied until now, so on
    the pilot client this returned 74 units for a run that could publish 20: the other 54 were
    products with no confirmed video or missing mandatory data, and every one of them was copy
    written to be skipped. What it deliberately does *not* apply is E18 or E21, which sit
    downstream of generation and would each hold the unit they were about to be closed by.

    **A CHANGED unit of a page already live is left out too, unless** ``include_live``. Copy for
    it is optional: without any, it is written keeping the text on the page
    (:func:`lib.state.keeps_live_copy`). Regenerating it replaces live text, so that is asked for,
    never inherited — the Content screen's "include already-published pages" box, off by default.

    **It reads state with :func:`~lib.state.peek_state`, never ``load_state``.** ``load_state``
    quarantines a corrupt file (E19) and returns an empty state; doing that from here would consume
    the reset before ``run_plan`` could report it, and the operator would never see "every row
    re-plans as NEW" at the plan gate — the one place that warning changes a decision.

    Args:
        cfg: The client config; supplies the categories, the URL patterns and the languages.
        scoped_products: The products already narrowed by :func:`in_scope`.

    Returns:
        The units to generate for, or ``None`` when the answer cannot be decided — an unparseable
        state file, a video map that will not load, or URL patterns the client has not set.
        ``None`` means "ask for everything", the same direction :func:`in_scope` errs in: a
        preflight may name a unit that turns out to be finished, but a run that quietly writes no
        copy for a page it is about to publish shows up as a blank page rather than as an error.
        The holds are not applied on that path either — a caller told "everything" is told it
        about every in-scope unit, so ``None`` keeps one meaning rather than two.
    """
    try:
        state = peek_state(cfg.client_id)
    except StateError:
        return None  # check_state_file is where a broken state file is reported
    try:
        held = held_units(cfg, scoped_products)
        gate = video_gate_for(cfg)
    except VideoMapError:
        return None  # check_video_map reports this; do not fail twice over it
    # Categories are inside the content hash, so they must be assigned exactly as ``run_plan``
    # assigns them or every live unit classifies CHANGED and the narrowing silently does nothing.
    categorised, _ = assign_categories(cfg.categories, scoped_products)
    try:
        classified = classify_units(
            categorised, state, cfg.wordpress.languages, cfg.wordpress, video_gate=gate
        )
    except ConfigError:
        return None  # check_config reports the missing patterns
    return {
        unit
        for unit, kind in classified.items()
        if kind in WILL_BE_WRITTEN
        and unit not in held
        and (include_live or not keeps_live_copy(state.entries.get(unit[0], {}).get(unit[1]), kind))
    }


def _excluded_aside(unchanged: int, held: int, keeps_live: int = 0) -> str:
    """Name the in-scope units this run writes no copy for, keeping the two reasons apart.

    Said out loud rather than left as arithmetic, because "20 unit(s) to publish" over a 37-product
    scope reads as a bug the first time an operator meets it — and a narrowing nobody can see is
    the one that turns out to be wrong.

    Each clause repeats "in-scope unit(s)" rather than sharing one subject, so a detail carrying
    only the second still says what the number counts. The reasons are never added together: an
    unchanged unit is live and finished, a held one has never published and will not until its
    source data or its video is fixed, and only one of those two is an operator's problem.
    """
    parts = []
    if keeps_live:
        parts.append(
            f"{keeps_live} in-scope unit(s) are live pages that change and keep the text already "
            "on the site"
        )
    if unchanged:
        parts.append(f"{unchanged} in-scope unit(s) are already live and unchanged")
    if held:
        parts.append(
            f"{held} in-scope unit(s) are held by the plan "
            "(no confirmed video, or missing mandatory data)"
        )
    if not parts:
        return ""
    return f"; {' and '.join(parts)}, so this run writes no copy for them"


def _product_counts(
    products: list[ProductRecord],
    wanted: set[tuple[str, str]] | None,
    held: dict[tuple[str, str], SkipReason],
    live: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """The same three groups counted in **products**, and split by what would unblock them.

    Every other figure here is in units, and stays that way: units are the plan's unit of work,
    and a count in anything else cannot be compared with the row counts beside it. These are
    *added* rather than substituted, because the two questions have different owners — "46 held"
    is what the run will do, "23 products blocked, 18 of them for want of a video" is what somebody
    has to go and fix, and nobody fixes half a product in one language.

    The split is by first-rule-fired, the same attribution ``held_units`` makes: E24 is the video
    map's problem and lands with whoever confirms videos, while E23 and E22 are source data and
    land in MyGS1. Collapsing those two into one number would send the operator to the wrong place.

    Empty when :func:`units_needing_copy` could not decide, because "everything" is not a set this
    can subtract from: a breakdown built on it would name products it never examined.
    """
    if wanted is None:
        return {}
    held_gtins = {gtin for gtin, _ in held}
    wanted_gtins = {gtin for gtin, _ in wanted}
    live_gtins = {gtin for gtin, _ in live or set()} - wanted_gtins
    video = {gtin for (gtin, _), reason in held.items() if reason is SkipReason.NO_CONFIRMED_VIDEO}
    return {
        "products_to_publish": len(wanted_gtins),
        "products_held_video": len(video),
        "products_held_data": len(held_gtins - video),
        "products_keep_live": len(live_gtins),
        "products_unchanged": len(
            {product.gtin for product in products} - held_gtins - wanted_gtins - live_gtins
        ),
    }


def check_generation_results(cfg: ClientConfig, products: list[ProductRecord]) -> CheckResult:
    """Report whether this run's ``generation_results.json`` covers the units it will publish.

    The core check when copy is written on one machine and published from another. Copy is not
    stored between runs, so this is not a question about accumulated coverage: it asks whether the
    file sitting on disk right now answers every unit this run would write, and whether it is
    still *about* this export. A unit it does not answer is an E21 omission — it leaves the plan
    without a row, and before ``Plan.skipped`` existed it left without a trace.

    **Scoped to the NEW/CHANGED units**, via :func:`units_needing_copy` — the same set
    ``run_generate`` asks the producer for. An already-published, unchanged unit is not generated
    for and must not be counted as uncovered, or this check FAILs on every run after the first,
    which is how a check stops being read. What it excluded is stated in the detail rather than
    dropped quietly: a narrowing nobody can see is the one that turns out to be wrong.

    Three ways a unit can be uncovered, and the fingerprint check is the one that matters most:
    the results file outlives the producer session that wrote it, so a ``parse_export`` re-run in
    between leaves copy describing data the feed no longer holds. Catching that here is what turns
    a forgotten regeneration into a loud failure *before* a wave rather than wrong copy on a live
    page. Entirely offline.
    """
    if cfg.generator is None:
        return CheckResult(
            "generation_results",
            "Generated copy for this run",
            Status.NA,
            "no `generator` block — this client publishes feed copy only",
        )
    products = in_scope(cfg, products)
    if not products:
        return CheckResult(
            "generation_results",
            "Generated copy for this run",
            Status.WARN,
            "no in-scope products to check the copy against",
            remedy="Run `python -m scripts.parse_export` first, and check the scope above.",
        )

    languages = cfg.wordpress.languages
    wanted = units_needing_copy(cfg, products)
    total = len(products) * len(languages) if wanted is None else len(wanted)
    # Safe to ask again only because ``wanted`` is not ``None``: that is precisely the case where
    # ``units_needing_copy`` already read the video map without raising.
    held_by_unit = {} if wanted is None else held_units(cfg, products)
    held = len(held_by_unit)
    # Live pages that change but keep their text: in neither ``wanted`` nor the holds, and not
    # unchanged either — they are written, just without copy. Counted apart so none of the three
    # figures claims them.
    widened = None if wanted is None else units_needing_copy(cfg, products, include_live=True)
    live = set() if wanted is None or widened is None else widened - wanted
    keeps_live = len(live)
    unchanged = len(products) * len(languages) - total - held - keeps_live
    context = generation_context(
        languages,
        cfg.wordpress.default_language,
        cfg.generator.prompt_version,
        cfg.export.gdsn_map,
        cfg.export.gdsn_extras,
    )
    try:
        results = load_results(cfg.client_id)
    except OrchestratorError as exc:
        # Reported rather than raised: the doctor exists to tell the operator what is wrong
        # before a wave, and crashing on the file it was asked to inspect does the opposite.
        return CheckResult(
            "generation_results",
            "Generated copy for this run",
            Status.FAIL,
            str(exc),
            remedy="Re-run the generate cycle for this client to write a fresh results file.",
            data={
                "total": total,
                "covered": 0,
                "pending": total,
                "pending_units": [],
                "unchanged": unchanged,
            },
        )
    missing = missing_copy(products, results, context, units=wanted)
    covered = total - len(missing)
    data: dict[str, object] = {
        "total": total,
        "covered": covered,
        "pending": len(missing),
        "pending_units": missing[:20],
        "unchanged": unchanged,
        # Separate from ``unchanged`` on purpose. Both are in-scope units this run writes no copy
        # for, and there the resemblance ends: an unchanged unit is already live and finished, a
        # held one has never published and will not until its source data or its video is fixed.
        # Adding them together would report the second as the first, which is the reading that
        # makes a hold look like a success.
        "held": held,
        "keeps_live": keeps_live,
        **_product_counts(products, wanted, held_by_unit, live),
    }
    excluded = _excluded_aside(unchanged, held, keeps_live)
    if not missing:
        return CheckResult(
            "generation_results",
            "Generated copy for this run",
            Status.OK,
            f"{total} unit(s) to publish, all covered{excluded}",
            data=data,
        )
    return CheckResult(
        "generation_results",
        "Generated copy for this run",
        Status.FAIL,
        f"{total} unit(s) to publish, {covered} covered, {len(missing)} without copy — those have "
        f"nothing written for this version of the export and will be dropped from the plan "
        f"(E21){excluded}",
        remedy="Run the generate cycle again for the current export: "
        "`python -m scripts.run_generate --emit`, write the copy, then `--validate`. Copy goes "
        "stale on any feed edit or prompt_version bump, not only on new products.",
        data=data,
    )


def check_process_list(cfg: ClientConfig) -> CheckResult:
    """Read the process list, which is also how a zero-GTIN file is caught.

    ``load_process_list`` treats an empty list as an error rather than an empty run, for the
    reason this whole module exists: an empty plan and a run that reports success having
    published nothing are indistinguishable from the outside.
    """
    if cfg.process_list is None:
        return CheckResult(
            "process_list",
            "Process list",
            Status.NA,
            "no `process_list` block — every product in the export is planned",
        )
    try:
        listed = load_process_list(cfg.process_list)
    except ProcessListError as exc:
        return CheckResult(
            "process_list",
            "Process list",
            Status.FAIL,
            str(exc),
            remedy=f"Check that {cfg.process_list.path} exists and has a "
            f"{cfg.process_list.gtin_column!r} column with at least one barcode.",
        )
    return CheckResult(
        "process_list",
        "Process list",
        Status.OK,
        f"{len(listed)} GTIN(s) listed for processing in {cfg.process_list.path}",
        data={"count": len(listed)},
    )


def check_selection_matches_export(cfg: ClientConfig) -> CheckResult:
    """Were these ticks chosen against the export that is on disk now?

    Nothing warned about this before, and there was no way to: a selection carries no trace of the
    export it was made against. The failure it catches is entirely silent — a barcode the current
    export has no row for produces no plan row, no error and no count anywhere, so the only evidence
    is a total one smaller than expected. Replacing the export after choosing a batch is the
    ordinary way to get there.

    A **warning, never a failure.** Re-checking the ticks may well be unnecessary; only the operator
    knows. And ``NOT_RECORDED`` reports ``NA`` rather than passing, because every batch saved before
    this was kept is in that state and a green line there would be a claim nobody made — the
    overclaiming that had to be taken out of the scope sentence once already.
    """
    name, title = "selection_matches_export", "Selection vs export"
    if cfg.process_list is None:
        return CheckResult(name, title, Status.NA, "no `process_list` block — nothing to compare")

    selection = Path(cfg.process_list.path)
    export = Path(cfg.export.path)
    batch = in_force(
        export=export,
        selection=selection,
        product_list=archive_path(selection),
        history=read(history_path(export)),
        gtin_column=cfg.process_list.gtin_column,
    )
    if batch.chosen_against is Chosen.THIS_EXPORT:
        return CheckResult(
            name, title, Status.OK, "the selection in force was chosen against this export"
        )
    if batch.chosen_against is Chosen.NOT_RECORDED:
        return CheckResult(
            name,
            title,
            Status.NA,
            "which export the selection was chosen against was not recorded — it predates this "
            "being kept",
        )
    return CheckResult(
        name,
        title,
        Status.WARN,
        f"the selection in force was chosen against {batch.chosen_export}, and the export on disk "
        f"is not that file",
        remedy="Open Data and check the ticks against this export. A barcode it has no row for is "
        "dropped with no error and no count.",
        data={"chosen_against": batch.chosen_export},
    )


def check_category_coverage(cfg: ClientConfig, products: list[ProductRecord]) -> CheckResult:
    """Run the ``build_brick_map --check`` gate offline: every GPC brick resolves to a term."""
    if cfg.categories is None:
        return CheckResult(
            "categories",
            "Category coverage",
            Status.NA,
            "no `categories` block — pages are published without a site category",
        )
    if not products:
        return CheckResult(
            "categories",
            "Category coverage",
            Status.WARN,
            "no parsed products to check brick coverage against",
            remedy="Run `python -m scripts.parse_export` first.",
        )
    report = coverage_report(products, cfg.categories)
    if report.is_complete:
        return CheckResult(
            "categories",
            "Category coverage",
            Status.OK,
            f"all {report.total_bricks} GPC brick(s) resolve to a term",
        )
    return CheckResult(
        "categories",
        "Category coverage",
        Status.WARN,
        f"{len(report.unmapped)} of {report.total_bricks} GPC brick(s) map to nothing: "
        + ", ".join(sorted(report.unmapped)),
        remedy="Add them to categories.brick_category_map, or set a per-GTIN override. "
        "`python -m scripts.build_brick_map --check` prints the same list. Unmapped bricks "
        "publish with the category left unset — the tool never guesses one.",
        data={"unmapped": sorted(report.unmapped)},
    )


def check_video_coverage(cfg: ClientConfig) -> CheckResult:
    """Run the ``build_video_map --check`` gate offline: every video file maps to a GTIN.

    Four outcomes, because they have four different fixes: no mapping configured (NA), a mapping
    that will not load (FAIL), the folders empty (WARN — copy the library across), and gaps in a
    mapping whose files are present (WARN — the client confirms them).
    """
    media = cfg.media
    if media is None or not media.video_map_path:
        return CheckResult(
            "video_map",
            "Video mapping",
            Status.NA,
            "no `media.video_map_path` — no videos are attached to pages",
        )
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError as exc:
        return CheckResult(
            "video_map",
            "Video mapping",
            Status.FAIL,
            str(exc),
            remedy="A missing file is a path problem — check media.video_map_path in clients.yml. "
            "A syntax error is an edit: the position above is where to look.",
        )
    files = files_by_language(media.video_folders)
    summary = summarize_video_map(vmap, files, cfg.wordpress.languages)
    data: dict[str, object] = {
        "confirmed_gtins": summary.confirmed_gtins,
        "files": summary.files,
        "entries": summary.entries,
        "unconfirmed": summary.unconfirmed,
        "ambiguous": summary.ambiguous,
        "missing_from_map": summary.missing_from_map,
        "file_missing": summary.file_missing,
        "issues": [
            issue.model_dump(mode="json") for issue in check_video_map(vmap, files)[:_ISSUE_SAMPLE]
        ],
    }
    if not summary.gaps:
        return CheckResult(
            "video_map",
            "Video mapping",
            Status.OK,
            f"{summary.files} video file(s), all mapped and confirmed; "
            f"{summary.confirmed_gtins} GTIN(s) have a confirmed video in every language",
            data=data,
        )
    # A gap is a WARN even under restrict_to_mapped_gtins, and especially then: the restriction
    # is what makes the gap *safe*. An unconfirmed GTIN is excluded from the run rather than
    # published with the wrong video, so this narrows the batch — it does not break it. Calling
    # a handled condition a failure is how a report earns the right to be ignored.
    return CheckResult(
        "video_map",
        "Video mapping",
        Status.WARN,
        _video_gap_detail(
            summary,
            restricted=media.restrict_to_mapped_gtins,
            publishes_bare=media.publish_without_video,
        ),
        remedy=_VIDEO_FILES_REMEDY if summary.no_files_found else _VIDEO_GAP_REMEDY,
        data=data,
    )


#: How many example gaps to carry in the check's payload for a screen to render.
_ISSUE_SAMPLE = 20

_VIDEO_FILES_REMEDY = (
    "Copy the video folders named under media.video_folders onto this machine — the mapping is "
    "the index to them, not a substitute. Nothing here is wrong with the mapping itself."
)

_VIDEO_GAP_REMEDY = (
    "`python -m scripts.build_video_map --check` lists each gap and writes "
    "video_map_issues.json. Confirming a mapping is the client's call, not the tool's."
)


def _video_gap_detail(
    summary: VideoMapSummary, *, restricted: bool, publishes_bare: bool = False
) -> str:
    """Say what is actually missing, counting each kind against its own denominator.

    This line used to read ``284 of 0 video file(s) are not yet confirmed``: every gap of every
    kind, over the number of files on disk. With the mapping handed over and the multi-gigabyte
    video library not yet copied — a day-one operator machine — each of the 166 rows also
    reported the file it names as absent, so the numerator counted a different thing from the
    denominator and comfortably exceeded it. A count that cannot be true teaches its reader to
    stop reading the line, which is expensive on the one screen they are meant to work down.
    """
    if summary.no_files_found:
        return (
            f"no video files found, but the mapping has {summary.entries} row(s) — the video "
            "folders are empty or not on this machine yet, so nothing can be attached to a page"
        )

    parts = [f"{summary.files} video file(s) found"]
    if summary.unconfirmed:
        parts.append(f"{summary.unconfirmed} mapping row(s) with no GTIN yet")
    if summary.missing_from_map:
        parts.append(f"{summary.missing_from_map} file(s) not in the mapping")
    if summary.file_missing:
        parts.append(f"{summary.file_missing} row(s) naming a file that is not there")
    if summary.ambiguous:
        parts.append(f"{summary.ambiguous} GTIN(s) mapped to more than one file")
    parts.append(f"{summary.confirmed_gtins} GTIN(s) confirmed in every language")

    detail = "; ".join(parts)
    if restricted and publishes_bare:
        detail += (
            " — the rest publish with no video where one is missing "
            "(media.publish_without_video), except a GTIN mapped to two files, which is held"
        )
    elif restricted:
        detail += " — and only those can be published, because media.restrict_to_mapped_gtins is on"
    return detail


def check_committed_config(
    client_id: str, live: str | Path = DEFAULT_CLIENTS_PATH, code_root: Path = CODE_ROOT
) -> CheckResult:
    """Whether the live ``clients.yml`` still says what the copy committed to git says.

    A warning, never a failure: the live copy is the one a run uses, and it is valid by the time
    this runs. What a difference means is that one side has a change the other does not — a Setup
    edit not yet committed, or a new release whose agreed settings this machine has not taken.
    """
    committed = committed_path(client_id, code_root)
    title = "Settings in git"
    if not committed.is_file():
        return CheckResult(
            "committed_config",
            title,
            Status.NA,
            f"no committed copy of {client_id}'s settings ({committed.relative_to(code_root)})",
        )
    if matches(Path(live), committed):
        return CheckResult(
            "committed_config",
            title,
            Status.OK,
            f"clients.yml matches {committed.relative_to(code_root).as_posix()}",
        )
    return CheckResult(
        "committed_config",
        title,
        Status.WARN,
        f"clients.yml differs from {committed.relative_to(code_root).as_posix()} in the program "
        "folder: a Setup change not committed yet, or a newer release's settings not taken",
        remedy="To keep this machine's settings: `python -m scripts.config_sync save`, then commit "
        "the file in a pull request. To take the committed ones: "
        "`python -m scripts.config_sync take`.",
    )


def check_client_files(
    cfg: ClientConfig, data_root: Path = Path(), code_root: Path = CODE_ROOT
) -> CheckResult:
    """Where this client's voice prompt and page templates are read from (:mod:`lib.client_files`).

    A warning when either comes only from the program folder: it still works, but those files
    are not in a handover pack, and a release that does not ship them would leave this client
    with no voice and the default page layout.
    """
    title = "Prompt and templates"
    if code_root.resolve() == data_root.resolve():
        return _client_files_in_a_clone(cfg.client_id, title, code_root)
    found = [
        client_files.locate(cfg.client_id, kind, data_root=data_root, code_root=code_root)
        for kind in (client_files.PROMPTS, client_files.TEMPLATES)
    ]
    present = [source for source in found if source.where != "none"]
    if not present:
        return CheckResult(
            "client_files",
            title,
            Status.NA,
            f"no prompt or templates of {cfg.client_id}'s own — the built-in page templates apply",
        )
    said = "; ".join(
        f"{source.kind} from the "
        + ("data folder" if source.where == "data" else "program folder")
        + f" ({source.path})"
        for source in present
    )
    if all(source.where == "data" for source in present):
        return CheckResult("client_files", title, Status.OK, said)
    return CheckResult(
        "client_files",
        title,
        Status.WARN,
        said + ". The program folder's copy is not in a handover pack, and a newer release may "
        "not ship it.",
        remedy="Run the installer again (it copies them into the data folder, and never "
        f"overwrites), or copy prompts/{cfg.client_id} to input/{cfg.client_id}/prompts and "
        f"templates/{cfg.client_id} to input/{cfg.client_id}/templates in the data folder.",
    )


#: How many differing files the clone warning names before "and N more".
_NAMED_FILES: Final = 3


def _client_files_in_a_clone(client_id: str, title: str, root: Path) -> CheckResult:
    """A development clone holds both copies; the gitignored one a pack brought in wins."""
    differ = [
        name
        for kind in (client_files.PROMPTS, client_files.TEMPLATES)
        for name in client_files.differences(client_id, kind, root=root)
    ]
    if not differ:
        return CheckResult(
            "client_files",
            title,
            Status.NA,
            "a development clone: the committed prompts/ and templates/ are what runs",
        )
    more = len(differ) - _NAMED_FILES
    shown = ", ".join(differ[:_NAMED_FILES]) + (f" and {more} more" if more > 0 else "")
    return CheckResult(
        "client_files",
        title,
        Status.WARN,
        f"{shown} differ from the committed copy, and win over it: a received handover pack "
        "brought them, so an edit to the committed prompts/ or templates/ is not what runs",
        remedy=f"Compare input/{client_id}/prompts and input/{client_id}/templates with "
        f"prompts/{client_id} and templates/{client_id}; commit the version you want, then "
        "delete the copy under input/ or make it identical.",
    )


def check_ffmpeg(cfg: ClientConfig) -> CheckResult:
    """Check for ``ffmpeg`` on PATH — the one external binary, and only when it is used."""
    media = cfg.media
    if media is None or not media.video_transcode:
        return CheckResult(
            "ffmpeg",
            "ffmpeg",
            Status.NA,
            "media.video_transcode is off — videos are uploaded as-is",
        )
    binary = media.ffmpeg_bin or "ffmpeg"
    found = shutil.which(binary)
    if found:
        return CheckResult("ffmpeg", "ffmpeg", Status.OK, f"{binary} found at {found}")
    return CheckResult(
        "ffmpeg",
        "ffmpeg",
        Status.FAIL,
        f"{binary} is not on PATH, but media.video_transcode is on",
        remedy="Install ffmpeg (macOS: `brew install ffmpeg`; Windows: "
        "`winget install Gyan.FFmpeg`, then open a new window so PATH is read again), or set "
        "media.video_transcode to false to upload the source files unchanged.",
    )


# --- Network checks ----------------------------------------------------------


def check_wordpress(cfg: ClientConfig) -> CheckResult:
    """Prove the WordPress credential authenticates *and* can still publish.

    ``GET /wp/v2/users/me?context=edit``, which is the check ``docs/troubleshooting.md``
    documents as a shell one-liner. The ``context=edit`` matters: a bare ``users/me`` returns
    200 for any credential that authenticates and omits ``roles`` entirely, so it cannot tell a
    working password from a working password on a demoted account.

    A 401 is reported with the six-groups hint, because the commonest cause by a wide margin is
    an application password that lost its quotes in ``.env`` and was truncated at the first
    space — a password the operator is certain is correct.
    """
    try:
        with WordPressClient(cfg.wordpress) as wp:
            identity = wp.whoami()
            detected = wp.detect_multilingual_plugin()
    except MissingCredentialError as exc:
        return CheckResult(
            "wordpress",
            "WordPress credential",
            Status.FAIL,
            str(exc),
            remedy=f"Set {cfg.wordpress.app_password_env} in .env, single-quoted — WordPress "
            "issues the password as six space-separated groups and an unquoted value is "
            "truncated at the first space.",
        )
    except WordPressAPIError as exc:
        return CheckResult(
            "wordpress",
            "WordPress credential",
            Status.FAIL,
            f"{cfg.wordpress.site_url} rejected the credential: {exc}",
            remedy=_wordpress_remedy(cfg, exc),
        )

    return _wordpress_verdict(cfg, identity, detected)


def _wordpress_remedy(cfg: ClientConfig, exc: WordPressAPIError) -> str:
    """The most likely fix for a WordPress error, chosen by status."""
    if exc.status_code == HTTPStatus.UNAUTHORIZED:
        return (
            f"Check {cfg.wordpress.app_password_env} in .env is single-quoted and has all "
            f"{_APP_PASSWORD_GROUPS} groups, and that {cfg.wordpress.username!r} still has an "
            "application password. An unquoted value truncates at the first space, which "
            "produces a 401 with a password the operator knows is correct."
        )
    if exc.status_code == HTTPStatus.FORBIDDEN:
        return (
            f"{cfg.wordpress.username!r} authenticated but lacks edit capability. Restore its "
            "editor role in WP Admin → Users."
        )
    return (
        f"Check that {cfg.wordpress.site_url} is reachable and its REST API is enabled "
        "(/wp-json should return JSON)."
    )


def _wordpress_verdict(
    cfg: ClientConfig, identity: WordPressIdentity, detected: str
) -> CheckResult:
    """Turn a successful ``whoami`` into a verdict, including the plugin-mismatch warning."""
    roles = identity.roles
    slug = identity.slug
    configured = cfg.wordpress.multilingual_plugin
    data: dict[str, object] = {
        "user": slug,
        "roles": roles,
        "plugin_configured": configured,
        "plugin_detected": detected,
    }

    if not _PUBLISHING_ROLES.intersection(roles):
        return CheckResult(
            "wordpress",
            "WordPress credential",
            Status.FAIL,
            f"authenticated as {slug!r} with roles {roles or ['(none)']} — none of which can "
            "publish",
            remedy=f"Give {cfg.wordpress.username!r} the editor role in WP Admin → Users. A "
            "credential that authenticates but cannot publish fails mid-run, after some rows "
            "are already live.",
            data=data,
        )

    # A mismatch is a warning, not a failure: _resolve_plugin lets the configured value win, so
    # the run still uses the right adapter. It is worth saying because the *other* direction is
    # silent — a site that answers "none" to both probes gets the NoOpAdapter, whose
    # link_translations does nothing and raises nothing, and every page publishes unlinked.
    if configured not in ("none", detected):
        return CheckResult(
            "wordpress",
            "WordPress credential",
            Status.WARN,
            f"authenticated as {slug!r} ({', '.join(roles)}), but the multilingual plugin is "
            f"configured as {configured!r} and the site probes as {detected!r}. The configured "
            "value wins, so the run proceeds — but one of the two is wrong.",
            remedy=f"Confirm {configured!r} is actually active on {cfg.wordpress.site_url}. If "
            "it is not, translations will not be linked and no error will say so.",
            data=data,
        )
    return CheckResult(
        "wordpress",
        "WordPress credential",
        Status.OK,
        f"authenticated as {slug!r} ({', '.join(roles)}) on {cfg.wordpress.site_url}; "
        f"multilingual plugin: {detected}",
        data=data,
    )


def check_site_serves(cfg: ClientConfig) -> CheckResult:
    """HEAD the site's public URL — the cheapest check here, and the one with no credential.

    Separated from :func:`check_wordpress` so "the site is down" and "the password is wrong"
    are two different answers rather than one confusing one.
    """
    try:
        with WordPressClient(cfg.wordpress) as wp:
            # verify_url returns True *or raises* — it never returns False. Both are handled
            # because the signature permits both and the comment in run_execute says so.
            served = wp.verify_url(cfg.wordpress.site_url)
    except (WordPressAPIError, MissingCredentialError) as exc:
        return CheckResult(
            "site_serves",
            "Site reachable",
            Status.FAIL,
            f"{cfg.wordpress.site_url} did not serve: {exc}",
            remedy="Check the site_url in clients.yml, and that the site is up.",
        )
    if not served:
        return CheckResult(
            "site_serves",
            "Site reachable",
            Status.FAIL,
            f"{cfg.wordpress.site_url} did not serve a 2xx/3xx",
            remedy="Check the site_url in clients.yml, and that the site is up.",
        )
    return CheckResult(
        "site_serves", "Site reachable", Status.OK, f"{cfg.wordpress.site_url} serves"
    )


def check_gs1(cfg: ClientConfig, products: list[ProductRecord]) -> CheckResult:
    """Mint a GS1 token and issue one read-only GET, so credentials fail here and not mid-run.

    Constructing the client mints nothing; the GET does, which is what makes this a credential
    check at all. The request is a read — it creates no record, and there is nothing to undo.

    **A ``None`` result cannot prove the account has a Digital Link contract.** The API answers
    a GTIN it has never seen with the same ``400 "No valid contract found for Gtin with id: …"``
    that the 21011 blocker produces, and the client maps that to ``None``. So a clean ``None``
    is reported as reachable-with-credentials-accepted and explicitly *not* as contract-present.
    Saying otherwise would be the most expensive kind of false pass: every write fails, and the
    operator has a green preflight telling them the account is fine.
    """
    environment = cfg.gs1.environment
    try:
        resolved = cfg.gs1.resolve()
    except ConfigError as exc:
        return CheckResult(
            "gs1",
            f"GS1 resolver ({environment})",
            Status.FAIL,
            str(exc),
            remedy="Fill in the account number and credential env-var names for this "
            "environment in the client's `gs1` block.",
        )

    gtin = products[0].gtin if products else None
    if gtin is None:
        return CheckResult(
            "gs1",
            f"GS1 resolver ({environment})",
            Status.WARN,
            "no parsed products, so there is no real GTIN to probe with",
            remedy="Run `python -m scripts.parse_export` first. This check will not invent a "
            "GTIN: a GS1 record can never be deleted, and a typo is permanent.",
        )

    try:
        with GS1DigitalLinkClient(resolved) as gs1:
            record = gs1.get(gtin)
    except MissingCredentialError as exc:
        return CheckResult(
            "gs1",
            f"GS1 resolver ({environment})",
            Status.FAIL,
            str(exc),
            remedy="Set the GS1 client id and secret for this environment in .env. The names "
            "come from the client's `gs1` block; the values come from MyGS1.",
        )
    except GS1APIError as exc:
        no_contract = _NO_CONTRACT_CODE in _gs1_error_codes(exc)
        return CheckResult(
            "gs1",
            f"GS1 resolver ({environment})",
            Status.FAIL,
            f"{exc}: {exc.response_body[:_ERROR_BODY_CHARS]}",
            remedy=(
                f"Error {_NO_CONTRACT_CODE}: this account has no Digital Link contract. That "
                "is provisioned by GS1 and cannot be fixed in code or config — see "
                "docs/gs1-nl-onboarding.md. Every write will fail until it is in place."
                if no_contract
                else "Check the account number and credentials for this environment against "
                "MyGS1, and docs/gs1-nl-onboarding.md for what the status means."
            ),
            data={"status_code": exc.status_code, "no_contract": no_contract},
        )

    if record is None:
        return CheckResult(
            "gs1",
            f"GS1 resolver ({environment})",
            Status.OK,
            f"credentials accepted and the {environment} resolver answered; GTIN {gtin} has no "
            "record yet, which is expected before its first publish. Note this response cannot "
            "distinguish 'not registered' from 'no Digital Link contract' — both are the same "
            "400.",
            data={"gtin": gtin, "registered": False},
        )
    return CheckResult(
        "gs1",
        f"GS1 resolver ({environment})",
        Status.OK,
        f"credentials accepted; GTIN {gtin} is already registered on the {environment} "
        "resolver, which also proves the account's Digital Link contract is live",
        data={"gtin": gtin, "registered": True},
    )


# --- Orchestration -----------------------------------------------------------


def load_products(client_id: str) -> list[ProductRecord]:
    """Read the parsed products, or an empty list when ``parse_export`` has not run.

    Missing is not an error here: a preflight run before the first parse is a reasonable
    thing to do, and the checks that need products say so themselves.
    """
    path = Path("output") / client_id / _PRODUCTS_RELATIVE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    try:
        return [ProductRecord.model_validate(item) for item in data]
    except (ValueError, TypeError):
        return []


def run_checks(
    client_id: str | None = None,
    *,
    config_path: str | Path = DEFAULT_CLIENTS_PATH,
    offline: bool = False,
    mode: Mode | None = None,
) -> list[CheckResult]:
    """Run every applicable check, in the order an operator should read them.

    ``mode`` is what the batch publishes. A links-only batch writes no page, so the checks that
    only a page needs — copy, categories, videos, ffmpeg — report ``NA`` for it rather than a
    verdict on something the run will never touch. ``None`` (the doctor run without ``--mode``)
    checks everything, as it always did.

    Config first, because nothing after it means anything if it fails — and when it does, that
    single result is the whole report rather than a page of cascading noise.

    Offline checks come before network ones so a run with no connectivity still returns
    everything it legitimately can, and ``offline=True`` stops before any credential is read.
    """
    config = check_config(config_path)
    if config.failed:
        return [config]

    try:
        cfg = get_client(client_id, config_path)
    except ConfigError as exc:
        return [
            config,
            CheckResult(
                "client",
                "Client selection",
                Status.FAIL,
                str(exc),
                remedy="Pass the client id explicitly, or remove the extra client from "
                "clients.yml — the id is only optional when exactly one is defined.",
            ),
        ]

    products = load_products(cfg.client_id)
    results = [
        config,
        check_committed_config(cfg.client_id, config_path),
        check_scope(cfg, products, links_only=mode is Mode.LINKS),
        check_generator(cfg),
        check_generation_results(cfg, products),
        check_process_list(cfg),
        check_selection_matches_export(cfg),
        check_category_coverage(cfg, products),
        check_video_coverage(cfg),
        check_client_files(cfg),
        check_ffmpeg(cfg),
    ]
    if mode is Mode.LINKS:
        results = [_not_for_links(result) for result in results]
    if offline:
        return results

    results.append(check_site_serves(cfg))
    results.append(check_wordpress(cfg))
    results.append(check_gs1(cfg, products))
    return results


#: The checks that are only about a page: a links-only batch writes none. See :func:`run_checks`.
PAGE_ONLY_CHECKS: Final = frozenset(
    {"generator_block", "generation_results", "categories", "video_map", "client_files", "ffmpeg"}
)


def _not_for_links(result: CheckResult) -> CheckResult:
    if result.name not in PAGE_ONLY_CHECKS:
        return result
    return CheckResult(
        result.name,
        result.title,
        Status.NA,
        "not needed — this batch publishes GS1 links only, and writes no page",
    )


def worst_status(results: list[CheckResult]) -> Status:
    """The most serious status in ``results`` — what the run as a whole should be called."""
    for status in (Status.FAIL, Status.WARN, Status.OK):
        if any(result.status is status for result in results):
            return status
    return Status.NA


def _generated_unit_count(client_id: str) -> int:
    """How many units this client's results file holds, or 0 when there is none.

    Evidence that a generator was configured here once — the job the cache's entry count used to
    do. A results file is per-run rather than accumulated, so this answers "was copy written for
    this client" over a narrower window than the cache did; that is the honest signal available,
    and it is still the one that matters, because a client that generates copy has a file here.
    """
    try:
        return len(load_results(client_id).results)
    except OrchestratorError:
        return 0


def _gs1_error_codes(exc: GS1APIError) -> set[str]:
    """The ``code`` values from a v2 ``ErrorResult[]`` body, or an empty set."""
    codes: set[str] = set()
    for result in exc.error_results or []:
        errors = result.get("errors")
        if isinstance(errors, list):
            codes.update(
                str(item["code"]) for item in errors if isinstance(item, dict) and "code" in item
            )
    return codes
