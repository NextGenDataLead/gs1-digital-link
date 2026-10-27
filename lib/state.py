"""Per-client run state: persistence, atomic writes, and content hashing.

Implements ``docs/IMPLEMENTATION_SPEC.md`` §4.8. State records, per
``(GTIN, language)``, the WordPress page id/URL, the featured-media id, the
content and GS1 link-set hashes, and the last-run timestamp — enough for
``scripts/run_execute.py`` to run idempotently and for change detection between
runs. The state models themselves (:class:`~lib.records.State`,
:class:`~lib.records.StateEntry`) live in ``lib/records.py``; this module is the
persistence/logic layer over them.

``save_state`` is **atomic**: it writes to a temporary file in the destination
directory and ``os.replace``s it into place, so a crash mid-write leaves the
previous ``state.json`` intact rather than a truncated file (§12 Phase 6 DoD).
``load_state`` recovers from a corrupt file rather than aborting (edge E19) — see its
docstring for why that is safe and what the caller must surface.

``diff_against_state`` (§4.8) classifies each ``(GTIN, language)`` against prior
state — NEW / UNCHANGED / CHANGED by content hash — and builds the ``PlanRow`` list
that ``scripts/run_plan.py`` writes to ``plan.json``. Its signature takes the whole
:class:`~lib.config.WordPressConfig` rather than §4.8's bare ``target_url_pattern``,
because building a ``PlanRow`` needs the slug pattern, site URL, post type, and
default language too — all of which live on that config. It returns a
:class:`PlanDiff`, not a bare list: the units it *drops* (E18/E21/E22) are as much of
its answer as the ones it keeps, and while they were only a warning log every caller
threw them away.

``classify_units`` is the same classification asked earlier and without the skip rules, for the
one caller that needs to know what a run would publish *before* there is anything to publish with:
``run_generate``, which writes copy only for the NEW and CHANGED units. Both go through
``_plan_unit``, so they cannot answer differently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from lib.errors import ConfigError, StateError
from lib.mandatory import missing_mandatory
from lib.media_video import canon_gtin
from lib.records import (
    PlanClassification,
    PlanRow,
    ProductRecord,
    SkippedUnit,
    SkipReason,
    State,
    StateEntry,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lib.config import WordPressConfig
    from lib.gdsn import GdsnSource
    from lib.media_video import VideoGate

_log = logging.getLogger(__name__)

#: Per-client state file, relative to the working directory (mirrors the
#: ``output/{client_id}/...`` layout used by ``scripts/parse_export.py``).
STATE_FILENAME: Final = "state.json"

#: Timestamp suffix for a quarantined corrupt state file (matches the run-log format).
CORRUPT_BACKUP_TS_FORMAT: Final = "%Y%m%dT%H%M%SZ"

#: The WordPress post status a live product page carries. Anything else means the page is
#: not publicly reachable, which :func:`_is_held` reads as "taken down on purpose".
_PUBLISHED_STATUS: Final = "publish"

#: The classifications a confirmed run actually writes. UNCHANGED is never confirmed and HELD is
#: dropped by ``run_execute`` regardless, so neither is a row that needs generated copy — which is
#: why E21 is asked only of these two. Also the set ``run_generate`` narrows generation to.
WILL_BE_WRITTEN: Final = frozenset({PlanClassification.NEW, PlanClassification.CHANGED})


def state_path(client_id: str) -> Path:
    """Return the state-file path for ``client_id`` (``output/{id}/state.json``)."""
    return Path("output") / client_id / STATE_FILENAME


def load_state(client_id: str) -> State:
    """Load a client's persisted state, or an empty state if none exists (§4.8).

    A **corrupt** state file is recovered from rather than fatal (edge E19): it is moved
    aside to ``state.json.corrupt.{ts}``, an ERROR is logged, and an empty state is
    returned with ``reset_from_corrupt`` set. State is a cache of what the tool believes
    it already did — derivable from the live systems, and safe to rebuild, because every
    write path is idempotent (§6.1–§6.5): ``upsert_page`` still finds the live page by
    slug or ``meta.gtin`` without a known id, ``safe_upsert`` reads before it writes, and
    QR renders are byte-identical. The cost of a reset is redundant work, not corruption.
    An **unreadable** file (permissions, I/O fault) is a different animal — that is an
    environmental fault where continuing would be wrong, so it still raises.

    Callers must surface ``reset_from_corrupt``: a reset turns an incremental re-run into
    a full rewrite (every row reclassifies as NEW), and the operator confirms the plan
    before any of it executes — so the reset has to reach them there, not only in a log.

    Args:
        client_id: The client whose state to load.

    Returns:
        The persisted :class:`~lib.records.State`; an empty one (``entries={}``) when no
        state file is present yet; or an empty one with ``reset_from_corrupt=True`` when
        a corrupt file was moved aside.

    Raises:
        StateError: If the file exists but cannot be read, or a corrupt file cannot be
            moved aside.
    """
    path = state_path(client_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State(client_id=client_id, entries={})
    except OSError as exc:
        raise StateError(f"cannot read state for {client_id!r} at {path}: {exc}") from exc

    try:
        return State.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        backup = _quarantine_corrupt(path, client_id, exc)
        _log.error(
            "state for %s at %s was corrupt (%s); moved to %s and starting fresh — every "
            "row will re-plan as NEW",
            client_id,
            path,
            exc,
            backup,
        )
        return State(
            client_id=client_id,
            entries={},
            reset_from_corrupt=True,
            corrupt_backup=str(backup),
        )


def peek_state(client_id: str) -> State:
    """Read a client's state **without** quarantining a corrupt one — for diagnostics only.

    :func:`load_state` moves a corrupt file aside (E19) so that a *run* can continue. That is
    right for a run and wrong for anything that is only looking: it turns an idle read into a
    change to what the next run does, and every published row would then re-plan as NEW. It is
    why ``scripts/doctor.py`` refuses to touch state at all.

    A reconciliation has to read it, though — comparing the ledger against the site is the whole
    job — so this is the read that cannot bite. Same parsing, no side effects, and a corrupt file
    is an error to report rather than a file to move.

    Args:
        client_id: The client whose state to read.

    Returns:
        The persisted :class:`~lib.records.State`, or an empty one when there is no file yet.

    Raises:
        StateError: If the file cannot be read, or will not parse. The file is left alone in
            both cases — including the corrupt one, which :func:`load_state` would have
            quarantined.
    """
    path = state_path(client_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State(client_id=client_id, entries={})
    except OSError as exc:
        raise StateError(f"cannot read state for {client_id!r} at {path}: {exc}") from exc

    try:
        return State.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise StateError(
            f"state for {client_id!r} at {path} will not parse ({exc}). It has been left "
            "exactly as it is — a run would quarantine it and start fresh, which is why this "
            "read does not."
        ) from exc


def _quarantine_corrupt(path: Path, client_id: str, cause: Exception) -> Path:
    """Move a corrupt state file aside to ``state.json.corrupt.{ts}`` and return its path.

    The bad file is preserved, never deleted: it is the only evidence of what went wrong,
    and the operator's instinct otherwise is to delete it.
    """
    ts = datetime.now(UTC).strftime(CORRUPT_BACKUP_TS_FORMAT)
    backup = path.with_name(f"{path.name}.corrupt.{ts}")
    try:
        os.replace(path, backup)
    except OSError as exc:
        raise StateError(
            f"state file for {client_id!r} at {path} is corrupt ({cause}) and cannot be "
            f"moved aside to {backup}: {exc}"
        ) from exc
    return backup


def save_state(state: State) -> None:
    """Atomically persist ``state`` to ``output/{client_id}/state.json`` (§4.8).

    Writes to a temporary file in the destination directory, flushes and fsyncs
    it, then ``os.replace``s it over the target. The replace is atomic on POSIX,
    so a crash at any point leaves either the old file or the fully-written new
    file — never a partial one.

    Args:
        state: The state to persist; its ``client_id`` determines the path.

    Raises:
        StateError: If the directory or file cannot be written.
    """
    path = state_path(state.client_id)
    payload = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise StateError(f"cannot write state for {state.client_id!r} at {path}: {exc}") from exc
    _log.info("Wrote state for %s (%d GTINs)", state.client_id, len(state.entries))


def compute_content_hash(product: ProductRecord, language: str, target_url: str) -> str:
    """Return a stable SHA-256 of the inputs that define a page's content (§4.8).

    The hash covers the full product, the language, and the resolver target URL,
    so any change to the rendered page or where it points changes the hash. It is
    canonical (sorted keys, fixed separators), hence deterministic across runs and
    processes.

    Args:
        product: The product whose content is being hashed.
        language: The page language (ISO 639-1).
        target_url: The resolver target URL for this ``(GTIN, language)``.

    Returns:
        The hex-encoded SHA-256 digest.
    """
    canonical = json.dumps(
        {
            "product": product.model_dump(mode="json"),
            "language": language,
            "target_url": target_url,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lang_segment(language: str, default_language: str) -> str:
    """Return the URL path segment for a language ("" for the default, else ``{lang}/``)."""
    return "" if language == default_language else f"{language}/"


def _is_held(prior: StateEntry) -> bool:
    """Whether this entry records a product that was deliberately taken down.

    Either half counts. ``run_unpublish`` retracts the resolver before drafting the pages,
    so an interrupted run leaves entries with ``retracted=True`` and a still-published
    page; treating that as held is what lets the next run finish the job rather than
    reverse it.

    The OR is also what keeps a *partial revive* honest: writing the resolver record back
    clears ``retracted``, and a product whose pages are still drafts stays held on the
    other half until they are published again.
    """
    return prior.wp_status != _PUBLISHED_STATUS or prior.retracted


def _has_no_resolver_link(prior: StateEntry) -> bool:
    """Whether this entry's page was published but its resolver link never written.

    An empty ``gs1_link_set_hash`` is what a ``run_execute --only pages`` run leaves
    behind: the page is live, but no Digital Link points at it. Every state file written
    before ``--only`` existed carries a real hash, so this never fires on old state.
    """
    return not prior.gs1_link_set_hash


def keeps_live_copy(prior: StateEntry | None, classification: PlanClassification) -> bool:
    """Whether a unit with no generated copy this run may still be written, keeping the live text.

    Generating copy is optional for a page that is already live. A CHANGED unit of a published page
    — a new title, a category, a video that arrived — is written with only what changed: the ACF
    payload omits a field with no value (:func:`lib.acf.build_acf_payload`), so the tagline and the
    description already on the page stay as they are. Skipping the Content step therefore cannot
    blank a page, and cannot count as a change either: copy is not in the content hash (#97).

    A NEW unit is never covered. It has no page yet, so publishing it without copy is the
    silently-blank page E21 exists to prevent.
    """
    return (
        classification is PlanClassification.CHANGED
        and prior is not None
        and prior.wp_page_id is not None
        and prior.wp_status == _PUBLISHED_STATUS
    )


def _page_video(gate: VideoGate | None, gtin: str, language: str) -> tuple[str | None, bool]:
    """The file this page gets, and whether it is in its folder — see :func:`video_arrived`."""
    if gate is None:
        return None, False
    video_file = gate.file_for(gtin, language)
    return video_file, gate.on_disk(language, video_file)


def video_arrived(prior: StateEntry, video_file: str | None, *, on_disk: bool = False) -> bool:
    """Whether this page now gets a video file other than the one it was published with.

    ``prior.video_file is None`` means the entry predates the field, so there is nothing to compare
    with and the answer is no — every page published before it was recorded was published under
    the rule that required a video, so none of them is waiting for one. ``""`` is a recorded "no
    video", which is what makes a product published without one come back once the client confirms
    a file: its content hash does not cover the video, so without this it would stay UNCHANGED, and
    the page would go on serving no video while every count said it had one.

    Only an *arriving* file counts. A confirmed file that disappears from the mapping does not
    reclassify the page, because the write path cannot take a video off a page — it only sets one —
    so re-running it would record "no video" over a page that still shows the old one.

    A file the last write could not prepare (``prior.video_failed``) counts again only once it is
    ``on_disk`` — in its folder, where the run reads it — and only when the failure was this
    machine's rather than the file's (``prior.video_failed_retry``, see
    :class:`~lib.records.StateEntry`). Without the first, a file still missing would rewrite the
    live page on every run and fail the same way; without the second, so would a file ffmpeg
    refuses. Without either, a page whose file was simply copied to the wrong folder never got
    it: the plan called it UNCHANGED and there was nothing left to confirm.
    """
    if not video_file or prior.video_file is None or video_file == prior.video_file:
        return False
    if video_file == prior.video_failed:
        return on_disk and prior.video_failed_retry is not False
    return True


def _classify(
    prior: StateEntry | None,
    content_hash: str,
    video_file: str | None = None,
    *,
    video_on_disk: bool = False,
) -> PlanClassification:
    """Classify one unit against its prior state entry.

    HELD is tested **before** the hash, because a deliberately unpublished product's hash
    still matches the content it was published with — that is what makes it invisible.
    Comparing content first would classify it UNCHANGED and let the next confirmed run put
    it straight back up.

    A page published without its resolver link (``run_execute --only pages``) is tested
    before the hash for the same reason and reported CHANGED: its content hash matches too,
    so on the hash alone a ``/gs1-pages`` run followed by ``/gs1-links`` would find every
    row UNCHANGED and silently publish nothing. HELD still outranks it — a product somebody
    took down does not become actionable just because half of it was never written.

    The field-level diff is :func:`_row_diff`, deliberately separate: only ``diff_against_state``
    builds rows to put one on, and folding it in here meant :func:`classify_units` computing a
    diff it discards — which made the title argument, and everything guarding it, unobservable.
    """
    if prior is None:
        return PlanClassification.NEW
    if _is_held(prior):
        return PlanClassification.HELD
    if _has_no_resolver_link(prior):
        return PlanClassification.CHANGED
    if video_arrived(prior, video_file, on_disk=video_on_disk):
        return PlanClassification.CHANGED
    if prior.content_hash == content_hash:
        return PlanClassification.UNCHANGED
    return PlanClassification.CHANGED


def _row_diff(  # noqa: PLR0913 — one argument per recorded field the diff compares
    prior: StateEntry | None,
    classification: PlanClassification,
    title: str,
    target_url: str,
    video_file: str | None = None,
    *,
    video_on_disk: bool = False,
) -> dict[str, tuple[str, str]] | None:
    """The field-level diff for a plan row, or ``None`` when there is nothing to show.

    Carries the fields whose prior value :class:`~lib.records.StateEntry` actually recorded —
    ``title`` and ``target_url`` (old ``wp_url`` → new) — in the order §10.6.2 presents them.
    Fields state does not keep are never fabricated, and an entry written before titles were
    persisted (``title is None`` on the *entry*) omits the title row rather than guessing. A
    CHANGED row whose recorded fields all still match carries no diff: the change is in the
    product body, which state does not retain.

    Only a CHANGED row has a diff. ``_has_no_resolver_link`` is asked again rather than passed
    down from :func:`_classify` — it is one pure predicate called twice, not a second opinion
    about what the classification means, and threading the reason through would give
    :func:`classify_units` a return value it has no use for.
    """
    if classification is not PlanClassification.CHANGED or prior is None:
        return None
    if _has_no_resolver_link(prior):
        return {"gs1_link": ("not written", "will be written")}
    diff: dict[str, tuple[str, str]] = {}
    if prior.title is not None and prior.title != title:
        diff["title"] = (prior.title, title)
    if prior.wp_url != target_url:
        diff["target_url"] = (prior.wp_url, target_url)
    if video_arrived(prior, video_file, on_disk=video_on_disk):
        diff["video"] = (prior.video_file or "none", video_file or "")
    return diff or None


class _Patterns(NamedTuple):
    """The two URL patterns every planned unit is built from."""

    slug: str
    target_url: str


def _patterns(wordpress: WordPressConfig) -> _Patterns:
    """The slug/target-URL patterns, validated once per call rather than once per unit.

    Raises:
        ConfigError: If either is unset. Both are required to build a plan — and to decide what a
            run would publish, which is the same computation asked a different way.
    """
    slug_pattern = wordpress.slug_pattern
    target_url_pattern = wordpress.target_url_pattern
    if slug_pattern is None or target_url_pattern is None:
        raise ConfigError(
            "wordpress.slug_pattern and wordpress.target_url_pattern are required to build a plan"
        )
    return _Patterns(slug_pattern, target_url_pattern)


class _UnitPlan(NamedTuple):
    """Everything the classification of one ``(GTIN, language)`` produces."""

    slug: str
    target_url: str
    content_hash: str
    classification: PlanClassification


def _plan_unit(  # noqa: PLR0913 — one argument per input the classification is a function of
    product: ProductRecord,
    language: str,
    wordpress: WordPressConfig,
    patterns: _Patterns,
    hashed: ProductRecord,
    prior: StateEntry | None,
    video_file: str | None = None,
    *,
    video_on_disk: bool = False,
) -> _UnitPlan:
    """Build one unit's slug, target URL, content hash and classification.

    The single implementation behind both :func:`diff_against_state` and :func:`classify_units`.
    They are asked the same question at different points in a run — ``run_generate`` asks which
    units a run would create or change, so it knows which need copy written; ``run_plan`` asks
    again once that copy is merged, to build the plan. Two implementations of it would drift, and
    the symptom would be copy generated for one set of units and demanded for another: a plan row
    with no copy, which reads as a producer failure rather than a classification one.

    It stops at the classification. The row's title and its diff belong to the caller that builds
    rows, and pulling them in here gave the other caller arguments whose effect it threw away.
    """
    slug = patterns.slug.format(gtin=product.gtin, gtin14=product.gtin14)
    target_url = patterns.target_url.format(
        site_url=wordpress.site_url.rstrip("/"),
        lang_segment=_lang_segment(language, wordpress.default_language),
        post_type=wordpress.post_type,
        slug=slug,
        gtin=product.gtin,
        gtin14=product.gtin14,
    )
    content_hash = compute_content_hash(hashed, language, target_url)
    classification = _classify(prior, content_hash, video_file, video_on_disk=video_on_disk)
    return _UnitPlan(slug, target_url, content_hash, classification)


def classify_units(
    products: list[ProductRecord],
    state: State,
    languages: list[str],
    wordpress: WordPressConfig,
    video_gate: VideoGate | None = None,
) -> dict[tuple[str, str], PlanClassification]:
    """How every ``(GTIN, language)`` would classify — with **no** skip rule applied.

    The question "what would this run publish?", asked before there is anything to publish with.
    ``run_generate`` needs it to write copy only for the rows a run will execute (NEW and CHANGED);
    UNCHANGED rows are never executed, so copy for them is text nothing will read. This became
    answerable at all with #97: the content hash covers the feed's record, so a unit's
    classification no longer depends on having generated copy.

    **Deliberately no E18/E21/E22/E23/E24.** Every one of them is downstream of this answer, and
    two would invert the dependency outright. E18 drops a language with no ``product_name`` — but a
    *translated* name is one of the things the producer supplies, so applying it here would drop
    the unit before the producer could close the gap, and the gap would then close itself out of
    existence. E21 drops a unit with no generated tagline, which before generation is all of them.

    Args:
        products: The products to classify — already narrowed to this run's scope by the caller.
        state: The client's persisted state (the classification baseline).
        languages: The languages to classify per product.
        wordpress: The client's WordPress config; supplies the slug/target-URL patterns, site URL,
            post type, and default language.

    Returns:
        ``(gtin, language) -> classification`` for every combination, in no particular order.

    Raises:
        ConfigError: If ``wordpress.slug_pattern`` or ``wordpress.target_url_pattern`` is unset.

    Note:
        There is no ``hash_source`` here, unlike :func:`diff_against_state`. That argument exists
        to exclude what a language model wrote, and this runs *before* a model has written
        anything: the records it is given are the feed's own by construction. Accepting one would
        be an argument no caller could have a use for — and it was here until a mutation showed
        that removing it broke nothing.
    """
    patterns = _patterns(wordpress)
    classified: dict[tuple[str, str], PlanClassification] = {}
    for product in products:
        for language in languages:
            prior = state.entries.get(product.gtin, {}).get(language)
            video_file, on_disk = _page_video(video_gate, product.gtin, language)
            unit = _plan_unit(
                product,
                language,
                wordpress,
                patterns,
                product,
                prior,
                video_file,
                video_on_disk=on_disk,
            )
            classified[(product.gtin, language)] = unit.classification
    return classified


class PlanDiff(NamedTuple):
    """What :func:`diff_against_state` found: the planned rows, and the units it dropped.

    A pair rather than a bare list so a caller cannot take the rows and leave the drops
    behind — which is exactly what every caller did while the drops were only a log line.
    """

    rows: list[PlanRow]
    skipped: list[SkippedUnit]


def _title_for(product: ProductRecord, language: str, default_language: str) -> str:
    """The product's name in ``language``, else in the default language, else its barcode."""
    return product.product_name.get(language, default_language) or product.gtin


def _skip(gtin: str, language: str, reason: SkipReason, detail: str) -> SkippedUnit:
    """Record a dropped unit *and* log it, so the two can never say different things."""
    _log.warning("SKIPPED %s (%s): %s", gtin, language, detail)
    return SkippedUnit(gtin=gtin, language=language, reason=reason, detail=detail)


def diff_against_state(  # noqa: PLR0913 — planning needs the products, baseline, and each policy flag
    products: list[ProductRecord],
    state: State,
    languages: list[str],
    wordpress: WordPressConfig,
    require_generated_copy: bool = False,
    require_hero_image: bool = False,
    mandatory_sources: dict[str, GdsnSource] | None = None,
    video_gate: VideoGate | None = None,
    hash_source: Mapping[str, ProductRecord] | None = None,
    name_required: bool = True,
) -> PlanDiff:
    """Classify each ``(GTIN, language)`` against prior state, building plan rows (§4.8, §8.2).

    For every product × language, computes the slug, resolver target URL, title, and
    content hash, then compares the hash to the persisted
    :class:`~lib.records.StateEntry`: no entry → NEW, equal hash → UNCHANGED, else
    CHANGED (carrying a ``title`` and/or ``target_url`` diff for whichever of those
    moved). An entry whose page was published without a resolver link is CHANGED whatever
    its hash says, and one that was deliberately taken down is HELD — see
    :func:`_classify`. A language with no ``product_name`` for a product is omitted with a warning
    (edge E18) rather than emitting a row with a missing title.

    **E21 is asked after the classification, and only of a row that will be written.** When
    ``require_generated_copy`` is set, a NEW or CHANGED unit with no generated tagline is omitted
    — the generator is configured but this unit has no copy, so publishing it would render a
    silently-blank page. An UNCHANGED or HELD unit is not asked at all: copy is written per run for
    the rows a run executes, and those two are never executed (``ui/pages/publish.py`` confirms
    neither, and ``run_execute`` drops HELD regardless), so arriving without copy is what correct
    looks like for them. Nor is a CHANGED unit of a page already live (:func:`keeps_live_copy`):
    it is written without the copy fields, so the page keeps its text, and the row says so in
    ``PlanRow.keeps_live_copy``. Asked before the classification, as it was until copy stopped being
    stored, E21 could not tell "nothing was written for this" from "nothing needed to be" — and
    reported every already-live page as a work item. The gap that E21 *does* catch is still
    reported upstream by ``merge_generated`` (``missing_generation_input``); this skip only keeps
    it out of the actionable plan.

    **What the hash is allowed to notice.** By default the hash covers the record it is given,
    whole. A caller that has enriched its records with language-model output — generated copy,
    or a language gap filled by translating the sibling — passes the pre-enrichment records as
    ``hash_source``, and the classification is computed from those instead. Model output is not
    stable across runs: regenerate over unchanged feed data and the wording moves, so a hash that
    covered it would reclassify every page CHANGED and rewrite the whole site having changed
    nothing. The enriched record is still what the row carries and what gets rendered; only the
    *comparison* ignores the parts a model wrote.

    Args:
        products: The products to plan.
        state: The client's persisted state (the classification baseline).
        languages: The languages to plan per product (``wordpress.languages``).
        wordpress: The client's WordPress config; supplies the slug/target-URL
            patterns, site URL, post type, and default language.
        require_generated_copy: When True (the client has a generator configured), skip any
            **NEW or CHANGED** ``(GTIN, language)`` lacking a generated tagline so a copy-less
            product is never published as a blank page (E21). Defaults to False for
            generator-less clients.
        require_hero_image: When True (``media.require_hero_image``), hold any GTIN whose source
            ``image_url`` is blank so a hero-less page is never published (E22). Off by default; a
            runtime image fetch failure still degrades gracefully and publishes (E7).
        mandatory_sources: The client's :attr:`ExportConfig.all_sources` — **both** declared
            maps. When given, a product missing any value marked ``required`` — or every member of
            a ``required_group`` — is held in **all** languages (E23), because a page assembled
            from an incomplete record publishes and looks finished. The hold is per product on
            purpose: publishing nl while fr is missing leaves a SKU half-live, which reads as
            success on every surface that counts pages. Named for the question rather than for
            one of the two maps: it took ``gdsn_map`` alone, which made ``required`` on a
            ``gdsn_extras`` entry a silent no-op here while the report was free to honour it.
        video_gate: The client's :class:`~lib.media_video.VideoGate`. When given, a product it
            does not admit is held in all languages (E24) — no confirmed video in some language
            (unless ``media.publish_without_video``), or two confirmed files in one. It is also
            what a published page's recorded video is compared with, so a page that went live
            without one reclassifies CHANGED once the client confirms a file. Passing a built gate
            rather than a path keeps this function free of file reading.
        hash_source: The records that *define* the content, keyed by GTIN, when they differ from
            the records being planned — see "What the hash is allowed to notice" above. Must cover
            every planned product: a partial mapping is a caller bug, and raising a ``KeyError``
            on it beats silently hashing the enriched record for the one GTIN that was forgotten,
            which would reclassify exactly that row and nothing else. Defaults to ``None``, which
            hashes each product itself.
        name_required: When False (a links-only plan), a language with no ``product_name`` is
            kept rather than dropped (E18): it writes no page, and its name is only the label of
            its link in the GS1 record, so it borrows the default language's name, or failing
            that the barcode. A link needs an address, not a name (operator, 2026-10-09).

    Returns:
        A :class:`PlanDiff`: one :class:`~lib.records.PlanRow` per planned
        ``(GTIN, language)`` in input order, and one
        :class:`~lib.records.SkippedUnit` per unit dropped by E18/E21/E22. The two are
        returned together, rather than the skips being left to a warning log the caller may
        or may not read, because a plan that silently lost every row is the failure this
        function is most able to cause.

    Raises:
        ConfigError: If ``wordpress.slug_pattern`` or ``wordpress.target_url_pattern``
            is unset — both are required to build a plan.
    """
    patterns = _patterns(wordpress)

    rows: list[PlanRow] = []
    skipped: list[SkippedUnit] = []
    for product in products:
        # E23/E24/E22 all drop the whole product, but each is recorded per language: the plan's
        # unit of work is ``(GTIN, language)``, and a count in any other unit cannot be compared
        # with the row counts beside it.
        gaps = missing_mandatory(product, mandatory_sources, languages) if mandatory_sources else []
        if gaps:  # E23
            detail = "missing mandatory source data: " + ", ".join(gap.label for gap in gaps)
            skipped.extend(
                _skip(product.gtin, language, SkipReason.MISSING_MANDATORY_FIELD, detail)
                for language in languages
            )
            continue
        if video_gate is not None and not video_gate.admits(product.gtin):  # E24
            detail = (
                "two confirmed videos in one language (held until the client picks one)"
                if canon_gtin(product.gtin) in video_gate.clashing
                else "no client-confirmed video in every language (held)"
            )
            skipped.extend(
                _skip(product.gtin, language, SkipReason.NO_CONFIRMED_VIDEO, detail)
                for language in languages
            )
            continue
        if require_hero_image and not (product.image_url or "").strip():  # E22
            skipped.extend(
                _skip(
                    product.gtin,
                    language,
                    SkipReason.BLANK_HERO_IMAGE,
                    "blank source image (held; require_hero_image)",
                )
                for language in languages
            )
            continue
        hashed = product if hash_source is None else hash_source[product.gtin]
        for language in languages:
            if name_required and language not in product.product_name.values:  # E18
                skipped.append(
                    _skip(
                        product.gtin,
                        language,
                        SkipReason.MISSING_PRODUCT_NAME,
                        f"missing product_name.{language}",
                    )
                )
                continue
            title = _title_for(product, language, wordpress.default_language)
            prior = state.entries.get(product.gtin, {}).get(language)
            video_file, on_disk = _page_video(video_gate, product.gtin, language)
            unit = _plan_unit(
                product,
                language,
                wordpress,
                patterns,
                hashed,
                prior,
                video_file,
                video_on_disk=on_disk,
            )
            tagline = product.generated_tagline
            copyless = require_generated_copy and not (tagline and tagline.values.get(language))
            keeps_live = copyless and keeps_live_copy(prior, unit.classification)
            if (
                copyless  # E21
                and unit.classification in WILL_BE_WRITTEN
                and not keeps_live
            ):
                skipped.append(
                    _skip(
                        product.gtin,
                        language,
                        SkipReason.NO_GENERATED_COPY,
                        "no generated copy (held for missing input)",
                    )
                )
                continue
            rows.append(
                PlanRow(
                    gtin=product.gtin,
                    language=language,
                    classification=unit.classification,
                    title=title,
                    slug=unit.slug,
                    content_hash=unit.content_hash,
                    target_url=unit.target_url,
                    diff=_row_diff(
                        prior,
                        unit.classification,
                        title,
                        unit.target_url,
                        video_file,
                        video_on_disk=on_disk,
                    ),
                    product=product,
                    keeps_live_copy=keeps_live,
                )
            )
    return PlanDiff(rows=rows, skipped=skipped)
