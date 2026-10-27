"""Build a run plan by classifying products against prior state (IMPLEMENTATION_SPEC §8.2).

Usage:
    python -m scripts.run_plan CLIENT_ID [--include-published] [--links-only] [--products PATH]

Loads the client config, its persisted state, and the parsed products, then classifies
each ``(GTIN, language)`` as NEW / UNCHANGED / CHANGED (``lib.state.diff_against_state``)
and writes the resulting :class:`~lib.records.Plan` to ``output/{client_id}/plan.json``.

When the client configures a ``process_list``, products are first gated to the GTINs on
that list — **every GTIN in the file is processed**, and every product not on it is
excluded and reported in the summary. The list carries no status columns and the tool
interprets no cell values: the operator prepares it by deleting the rows that should not
run. Without a ``process_list`` config, every product is planned (the plain spec
behaviour).

    --products:   default output/{client_id}/data/products.json
    --links-only: build the plan for a ``run_execute --only links`` run — the generated-copy
                  rule (E21) is not applied, because a links run renders no page. The plan is
                  marked ``links_only`` and ``run_execute`` refuses it for any other mode.

When ``process_list.target_url_column`` is set, every row of a GTIN whose cell is filled carries
that address as ``listed_url``: the page a links run points the resolver at, for a product whose
page this tool did not make. It is read in every mode, so a pages or both run can refuse a GTIN
whose listed page is not the one it would publish.

A *corrupt* state file is not fatal (E19): ``load_state`` moves it aside and starts fresh,
and the summary leads with a warning — every row then re-plans as NEW, which is idempotent
to execute but rewrites live pages and resolver targets. An *unreadable* one still exits 2.

Everything the run concluded but did not put *in* the plan — the gate exclusions, the tally
of units dropped before classification, the E19 reset and where the corrupt file went — is
also written to ``plan.summary.json``, and the stderr line is carried in it verbatim. It used
to exist only as prose on a stream, so the only reader that could ever see it was the process
that ran the command.

Emits:  output/{client_id}/plan.json (a Plan as JSON)
        output/{client_id}/plan.summary.json (a PlanSummary as JSON, always)
Exit codes:
    0  plan written
    2  config/state error (bad client id, unreadable products/state/control file,
       missing slug/target_url patterns)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NamedTuple

from pydantic import ValidationError

from lib.categories import assign_categories
from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, GeneratorError, ProcessListError, StateError, VideoMapError
from lib.generator import (
    generation_context,
    load_results,
    merge_generated,
    withhold_published_copy,
)
from lib.holds import video_gate_for
from lib.link_set import link_is_stale
from lib.media_video import VideoGate, canon_gtin
from lib.process_list import load_listed_targets, load_process_list
from lib.records import (
    Plan,
    PlanClassification,
    PlanRow,
    PlanSummary,
    ProductRecord,
    SkippedUnit,
    SourceIssue,
    State,
)
from lib.state import diff_against_state, load_state, video_arrived

_log = logging.getLogger("scripts.run_plan")

_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 2

#: Leads the summary when prior state was reset from a corrupt file (E19). Every row then
#: re-plans as NEW: re-running them is idempotent, but it rewrites live pages and resolver
#: targets rather than skipping them, so the operator must see this before confirming.
_STATE_RESET_WARNING = (
    "WARNING: prior state was corrupt and has been reset (backed up alongside state.json). "
    "All rows re-plan as NEW — executing them will rewrite live pages and resolver targets."
)

#: Printed above the counts when ``--include-published`` re-admitted finished GTINs. It leads for
#: the same reason the E19 reset warning does: it changes what the counts underneath *mean*. A
#: CHANGED row in an ordinary plan is a product on its way to its first page; in this plan it is a
#: live page about to be rewritten, and the two read identically once the flag has scrolled away.
_INCLUDE_PUBLISHED_WARNING = (
    "NOTE: --include-published — GTINs that are already published and resolvable were re-planned "
    "instead of being treated as finished. A CHANGED row here rewrites a LIVE page. Pages are "
    "matched by slug/meta.gtin and updated in place, not duplicated, and an untouched product "
    "still classifies UNCHANGED and is never executed."
)

#: Printed above the counts when ``--links-only`` built the plan. It leads because it limits what
#: the plan may be used for: rows with no generated copy are in it, which is right for a resolver
#: write and wrong for a page.
_LINKS_ONLY_NOTE = (
    "NOTE: --links-only — the generated-copy rule was not applied, so this plan can only be "
    "executed with --only links (run_execute refuses it otherwise)."
)


def _default_products_path(client_id: str) -> Path:
    """The default parsed-products location written by ``scripts/parse_export.py``."""
    return Path("output") / client_id / "data" / "products.json"


def _load_products(path: Path) -> list[ProductRecord]:
    """Read the parsed-products JSON array into ``ProductRecord``s."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(item) for item in data]


def _gate(
    products: list[ProductRecord], listed: frozenset[str]
) -> tuple[list[ProductRecord], dict[str, int]]:
    """Filter products to the GTINs the operator listed for processing.

    Membership is the whole rule: a product is a candidate if and only if its GTIN is on
    the list. No cell values are read, so there is nothing here to misinterpret — see
    :mod:`lib.process_list` for why that matters.

    Returns the listed products plus a one-key tally (``not_listed``) of how many were
    excluded. GTINs are compared as GTIN-14 so a 13-digit barcode in the file joins to a
    14-digit product GTIN regardless of a leading zero.
    """
    candidates = [product for product in products if product.gtin14 in listed]
    return candidates, {"not_listed": len(products) - len(candidates)}


def _pilot_gate(
    products: list[ProductRecord],
    state: State,
    excluded: dict[str, int],
    gate: VideoGate | None,
    *,
    include_published: bool = False,
) -> tuple[list[ProductRecord], dict[str, int]]:
    """Drop GTINs that are already finished (§9.5), unless ``include_published``.

    **``include_published`` exists because "finished" is an assumption, not a fact.** A product
    whose source data changes after it goes live is not finished, and this gate removed it before
    classification — so the plan came back empty and a run reported success having written
    nothing. That is indistinguishable from "there was nothing to do". The flag re-admits those
    GTINs and lets :func:`lib.state.diff_against_state` decide: an untouched product still
    classifies UNCHANGED and is never executed, so the flag widens what is *considered*, not what
    is published.

    It is deliberately not the default. Re-planning finished products makes a routine run capable
    of rewriting live pages, which the operator must choose rather than inherit — and every
    surface that reports a plan says the flag was used (see :class:`~lib.records.PlanSummary`).

    A no-op unless ``media.restrict_to_mapped_gtins`` (``gate`` is ``None`` otherwise). Extends
    the tally with ``already_present`` (published *and* resolvable, *and* not waiting for a video
    the client has since confirmed — see below). ``run_execute`` hard-enforces the video rule
    independently, so a ``--plan`` slice can still update an already-present pilot GTIN.

    **It no longer drops GTINs that lack a confirmed video.** That was a silent exclusion: the
    product vanished before classification, so it appeared in no plan, no skip list and no report —
    indistinguishable from a product nobody had asked about. Those GTINs now reach
    :func:`lib.state.diff_against_state` and are *held* there (E24), which puts them in
    ``PlanDiff.skipped`` and therefore in the data-quality report, where the missing video can be
    acted on. Same products excluded from publishing; the difference is that the operator can see
    which, and why.

    **"Finished" means published *and* resolvable, not merely having a state entry.** A
    ``run_execute --only pages`` run writes an entry whose ``gs1_link_set_hash`` is empty — the
    page is live but no Digital Link points at it, which :func:`lib.state._has_no_resolver_link`
    reports CHANGED so a follow-up ``--only links`` has something to plan. Treating any entry as
    finished dropped those GTINs here, *before* classification ran, so the row never reached
    ``_classify`` and the plan came back empty: a ``pages`` run silently removed its own GTIN from
    every subsequent plan and the two-step flow could not complete. Requiring a link-set hash in
    every language keeps a half-published GTIN in the queue until its resolver record exists.
    Entries written before ``--only`` existed all carry a real hash, so no prior state reclassifies.

    **Nor does it mean "has every video it is going to get".** Under ``media.publish_without_video``
    a product goes live with no video in a language, and when the client later confirms one, this
    gate is the first thing that would hide it: dropped as present, it never reaches the
    classification that would notice the arrival (:func:`lib.state.video_arrived`), and the page
    serves no video forever while every run reports nothing to do. So a GTIN with an arriving video
    stays in, and the classification decides — CHANGED for that language only.
    """
    excluded = {**excluded, "already_present": 0}
    if include_published or gate is None or not gate.enforced:
        return products, excluded

    present = {
        canon_gtin(gtin)
        for gtin, entries in state.entries.items()
        if all(entry.gs1_link_set_hash for entry in entries.values())
        and not any(
            video_arrived(
                entry,
                gate.file_for(gtin, language),
                on_disk=gate.on_disk(language, gate.file_for(gtin, language)),
            )
            for language, entry in entries.items()
        )
    }
    kept: list[ProductRecord] = []
    for product in products:
        if product.gtin14 in present:
            excluded["already_present"] += 1
        else:
            kept.append(product)
    return kept, excluded


def _assign_categories(
    cfg: ClientConfig, products: list[ProductRecord]
) -> tuple[list[ProductRecord], list[SourceIssue]]:
    """Assign each product's site category (Phase 7.5), and say out loud what did not resolve.

    The assignment itself is :func:`lib.categories.assign_categories`, shared with the caller that
    has to reproduce this run's records before the generator (``run_generate``). What stays here
    is the operator-facing warning summary: it belongs to the command the operator is watching, and
    emitting it from the shared helper would make every caller repeat it.

    Called before classification so the category is part of the content hash: a category change
    reclassifies as CHANGED.
    """
    assigned, issues = assign_categories(cfg.categories, products)

    for brick in sorted({i.value for i in issues if i.issue == "category_unmapped" and i.value}):
        _log.warning("GPC brick %s maps to no category term; leaving category unset", brick)
    missing_brick = sum(1 for i in issues if i.issue == "category_brick_missing")
    if missing_brick:
        _log.warning("%d product(s) have no GPC brick to derive a category from", missing_brick)
    return assigned, issues


def _generate_content(
    cfg: ClientConfig, products: list[ProductRecord]
) -> tuple[list[ProductRecord], list[SourceIssue]]:
    """Fold this run's generated copy onto each product (generator SPEC) — file-only, no network.

    A no-op when the client has no ``generator`` config. Otherwise reads this run's
    ``generation_results.json`` and runs :func:`lib.generator.merge_generated`, materialising the
    combined title, tagline, and three-part description onto each record.

    It reads that file and never writes it, so re-running the plan is free and repeatable. There
    is nothing to reuse from a previous run: copy is written fresh each time, and a result whose
    fingerprint no longer matches the feed is dropped rather than published — see
    :func:`lib.generator.merge_generated`.

    It runs before ``diff_against_state`` because the *skip* decisions need it: E21 asks whether a
    tagline exists, and filling a missing French name stops E18 firing for a gap the generator has
    since closed (a genuine gap gets no generated fields and falls to the E18 backstop). What it
    deliberately does **not** feed is the content hash — the caller passes the pre-generator
    records as ``hash_source``, so a re-generation that words the same source data differently
    leaves a published page UNCHANGED rather than rewriting it. Model output is not stable enough
    to be a change signal.

    Returns the products with generated fields set, plus one :class:`SourceIssue` per
    generated/adjusted value, per blank marketing message, and per value filled by translating the
    language the feed does carry it in.

    The ``gdsn_map``/``gdsn_extras`` are passed because they carry the ``translate`` flags: which
    values may be filled is a client's decision about its own page, not a rule in code.
    """
    if cfg.generator is None:
        return products, []
    results = load_results(cfg.client_id)
    context = generation_context(
        cfg.wordpress.languages,
        cfg.wordpress.default_language,
        cfg.generator.prompt_version,
        cfg.export.gdsn_map,
        cfg.export.gdsn_extras,
    )
    return merge_generated(products, results, context)


def _write_issue_report(client_id: str, filename: str, issues: list[SourceIssue]) -> None:
    """Write a per-step issue report to ``output/{client}/data/{filename}``, always — even empty.

    Written unconditionally (like ``parse_export``'s source_issues.json) so an empty file means
    "this run found nothing" and a missing file means "this step did not run". Each step owns its
    own file, separate from source_issues.json, which ``parse_export`` owns and overwrites.
    """
    path = Path("output") / client_id / "data" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [issue.model_dump(mode="json") for issue in issues]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class _PlanResult(NamedTuple):
    """A plan and everything the caller must report about how it was arrived at.

    ``state`` is carried whole rather than reduced to its ``reset_from_corrupt`` flag,
    because the quarantine path beside it is the *evidence* for that flag — an operator
    told a reset happened will ask where the old file went, and only ``load_state`` knows.
    """

    plan: Plan
    excluded: dict[str, int]
    state: State
    category_issues: list[SourceIssue]
    generated_issues: list[SourceIssue]
    #: Whether ``--include-published`` re-admitted already-finished GTINs. Carried rather than
    #: inferred from ``excluded["already_present"] == 0``, which is also what a first run looks
    #: like — the two must never be confused by anything reporting this plan.
    included_published: bool = False


def _with_listed_urls(rows: list[PlanRow], listed: dict[str, str]) -> list[PlanRow]:
    """Attach each GTIN's listed page to every one of its rows, leaving the rest untouched."""
    return [
        row.model_copy(update={"listed_url": listed[row.product.gtin14]})
        if row.product.gtin14 in listed
        else row
        for row in rows
    ]


def _build_plan(
    cfg: ClientConfig,
    products: list[ProductRecord],
    *,
    include_published: bool = False,
    links_only: bool = False,
) -> _PlanResult:
    """Gate, assign categories, merge generated copy, classify, and assemble the :class:`Plan`.

    Returns the plan, the gate-exclusion tally, the loaded state (whose ``reset_from_corrupt``
    the caller must surface, because it means every row re-plans as NEW), the category-mapping
    issues (unmapped bricks left unset), and the generated-content issues (one per
    generated/adjusted value and per blank marketing message).
    """
    listed: dict[str, str] = {}
    if cfg.process_list is not None:
        candidates, excluded = _gate(products, load_process_list(cfg.process_list))
        listed = load_listed_targets(cfg.process_list, cfg.wordpress.site_url)
    else:
        candidates, excluded = products, {"not_listed": 0}

    state = load_state(cfg.client_id)
    # A links-only plan writes no page, so nothing a page needs may hold it — not the video, not
    # the image, not a blank page field, not a missing name (operator, 2026-10-09). What can stop
    # it is its target, and ``run_execute`` refuses a target that does not serve immediately
    # before every GS1 write.
    gate = None if links_only else video_gate_for(cfg)
    candidates, excluded = _pilot_gate(
        candidates, state, excluded, gate, include_published=include_published
    )

    candidates, category_issues = _assign_categories(cfg, candidates)
    # The record as the feed defines it, categories included and the generator's output not. This
    # is what the classification compares against prior state, so re-generating copy over
    # unchanged source data leaves a live page alone instead of rewriting it with new wording.
    feed_view = {product.gtin: product for product in candidates}
    candidates, generated_issues = _generate_content(cfg, candidates)
    if cfg.generator is not None:
        # Copy older than the page it would go on is already live (or was replaced): sending it
        # again would rewrite text this run did not set out to touch. Without it, a live page
        # keeps its text — ``lib.state.keeps_live_copy``.
        candidates = withhold_published_copy(
            candidates,
            {
                (gtin, language): entry.last_run
                for gtin, entries in state.entries.items()
                for language, entry in entries.items()
                if entry.wp_page_id is not None
            },
            load_results(cfg.client_id).generated_at,
        )

    rows, skipped = diff_against_state(
        candidates,
        state,
        cfg.wordpress.languages,
        cfg.wordpress,
        require_generated_copy=cfg.generator is not None and not links_only,
        require_hero_image=(
            cfg.media is not None and cfg.media.require_hero_image and not links_only
        ),
        mandatory_sources=None if links_only else cfg.export.all_sources,
        video_gate=gate,
        hash_source=feed_view,
        name_required=not links_only,
    )
    rows = _mark_stale_links(cfg, rows, state)
    counts = {c: sum(1 for row in rows if row.classification is c) for c in PlanClassification}
    plan = Plan(
        client_id=cfg.client_id,
        generated_at=datetime.now(UTC),
        total=len(rows),
        counts=counts,
        rows=_with_listed_urls(rows, listed),
        skipped=skipped,
        links_only=links_only,
    )
    return _PlanResult(plan, excluded, state, category_issues, generated_issues, include_published)


#: The ``diff`` key on a row whose GS1 record no longer matches its page (``_mark_stale_links``).
STALE_LINK_DIFF: Final = "gs1_link_out_of_date"


def _mark_stale_links(cfg: ClientConfig, rows: list[PlanRow], state: State) -> list[PlanRow]:
    """Turn an UNCHANGED row CHANGED when its GTIN's GS1 record says something else.

    A ``pages`` run records a page's new title and content hash and keeps the old link-set hash,
    because it wrote no link. Judged on the content hash alone, the row is then UNCHANGED, and a
    ``links`` run does nothing while the record goes on carrying the old name — which is what
    renaming 38 Democlient products did (2026-10-22). :func:`lib.link_set.link_is_stale` rebuilds
    the link set from the ledger and asks.

    Asked of the product **as the row carries it** — after the generator has combined the title
    with its variation — because that is the record ``run_execute`` builds the link from. Every
    language row of the GTIN is marked: the record is written for the whole GTIN at once.
    """
    by_gtin: dict[str, PlanRow] = {}
    for row in rows:
        by_gtin.setdefault(row.gtin, row)
    stale = {
        gtin
        for gtin, row in by_gtin.items()
        if link_is_stale(cfg, row.product, state.entries.get(gtin, {}))
    }
    if not stale:
        return rows
    _log.info("%d GTIN(s) whose GS1 record is out of date with its page: %s", len(stale), stale)
    return [
        row.model_copy(
            update={
                "classification": PlanClassification.CHANGED,
                "diff": {STALE_LINK_DIFF: ("GS1 record", "out of date with the page")},
            }
        )
        if row.gtin in stale and row.classification is PlanClassification.UNCHANGED
        else row
        for row in rows
    ]


def _write_plan(client_id: str, plan: Plan) -> Path:
    """Write ``plan.json`` under the client's output directory and return its path."""
    path = Path("output") / client_id / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    return path


def _summarise(result: _PlanResult) -> PlanSummary:
    """Assemble the machine-readable summary, ``text`` included.

    Built from the same values the stderr line is built from, and carrying that line
    verbatim, so a second reader shows the operator the same words rather than a
    reconstruction that can drift from them.
    """
    plan = result.plan
    return PlanSummary(
        client_id=plan.client_id,
        generated_at=plan.generated_at,
        total=plan.total,
        counts=plan.counts,
        skipped=dict(Counter(unit.reason for unit in plan.skipped)),
        excluded={reason: n for reason, n in result.excluded.items() if n},
        unmapped_categories=len(result.category_issues),
        generated_issues=len(result.generated_issues),
        state_reset_from_corrupt=result.state.reset_from_corrupt,
        state_corrupt_backup=result.state.corrupt_backup,
        included_published=result.included_published,
        links_only=plan.links_only,
        text=_summary(
            plan,
            result.excluded,
            result.state.reset_from_corrupt,
            len(result.category_issues),
            len(result.generated_issues),
            result.included_published,
        ),
    )


def _write_summary(client_id: str, summary: PlanSummary) -> Path:
    """Write ``plan.summary.json`` beside the plan and return its path.

    Written on every run, never conditionally: a missing file has to mean "run_plan did not
    run", so that an empty tally can mean "it ran and found nothing". Those are different
    facts and a reader that cannot tell them apart is the E21 trap in another costume.
    """
    path = Path("output") / client_id / "plan.summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    return path


def _summary(  # noqa: PLR0913 — one parameter per thing the operator must be told about
    plan: Plan,
    excluded: dict[str, int],
    state_was_reset: bool,
    unmapped_categories: int = 0,
    generated_issues: int = 0,
    included_published: bool = False,
) -> str:
    """Render the stderr summary (§8.2): gate exclusions when non-zero, E19 reset when it fired.

    The reset warning leads, because it reframes every count below it — with no prior state
    every row is NEW, and that is a full rewrite rather than the incremental run the operator
    is expecting.
    """
    line = (
        f"{plan.counts[PlanClassification.NEW]} new, "
        f"{plan.counts[PlanClassification.UNCHANGED]} unchanged, "
        f"{plan.counts[PlanClassification.CHANGED]} changed"
    )
    held = plan.counts[PlanClassification.HELD]
    if held:
        line += f", {held} held (unpublished; run_execute skips these without --revive)"
    not_listed = excluded.get("not_listed", 0)
    if not_listed:
        line += f"; {not_listed} excluded (not on the process list)"
    already_present = excluded.get("already_present", 0)
    if already_present:
        line += f"; {already_present} pilot-excluded (already have a page)"
    if plan.skipped:
        line += f"; {len(plan.skipped)} skipped ({_skip_tally(plan.skipped)})"
    if unmapped_categories:
        line += f"; {unmapped_categories} product(s) with unmapped category (left unset)"
    if generated_issues:
        line += f"; {generated_issues} generated-content note(s) — see generated_issues.json"
    listed = len({row.gtin for row in plan.rows if row.listed_url})
    if listed:
        line += f"; {listed} GTIN(s) with a listed page"
    if included_published:
        line = f"{_INCLUDE_PUBLISHED_WARNING}\n{line}"
    if plan.links_only:
        line = f"{_LINKS_ONLY_NOTE}\n{line}"
    if state_was_reset:
        # Above the include-published note as well: a corrupt-state reset re-plans *everything*
        # as NEW, which subsumes whatever the flag re-admitted.
        line = f"{_STATE_RESET_WARNING}\n{line}"
    return line


def _skip_tally(skipped: list[SkippedUnit]) -> str:
    """``"4 no_generated_copy, 2 missing_product_name"`` — commonest reason first.

    The reason is named, not just counted. "6 skipped" is a number an operator can shrug at;
    "6 no generated copy" is an instruction to go and generate it.
    """
    counts = Counter(unit.reason for unit in skipped)
    return ", ".join(f"{n} {reason.value}" for reason, n in counts.most_common())


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_plan", description="Build a run plan from products.")
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument(
        "--products",
        help="Path to the parsed products JSON (default: output/{id}/data/products.json)",
    )
    parser.add_argument(
        "--include-published",
        action="store_true",
        help=(
            "Re-plan GTINs that are already published and resolvable, instead of dropping them "
            "as finished. Use when source data changed after a product went live; a CHANGED row "
            "then rewrites a LIVE page"
        ),
    )
    parser.add_argument(
        "--links-only",
        action="store_true",
        help=(
            "Build the plan for a `run_execute --only links` run: units with no generated copy "
            "are kept, since no page is rendered. run_execute refuses such a plan for any "
            "other mode"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        cfg = get_client(args.client_id)
        products_path = (
            Path(args.products) if args.products else _default_products_path(cfg.client_id)
        )
        products = _load_products(products_path)
        result = _build_plan(
            cfg, products, include_published=args.include_published, links_only=args.links_only
        )
    except (
        ConfigError,
        GeneratorError,
        StateError,
        ProcessListError,
        # Same class of fault as the process list: an operator input file that will not load.
        # It reaches here from the pilot allowlist, where proceeding is not an option — an
        # allowlist that cannot be read would either publish everything or nothing, and both
        # are wrong answers to "which GTINs did the client confirm a video for?".
        VideoMapError,
        FileNotFoundError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    path = _write_plan(cfg.client_id, result.plan)
    if cfg.categories is not None:
        _write_issue_report(cfg.client_id, "category_issues.json", result.category_issues)
    if cfg.generator is not None:
        _write_issue_report(cfg.client_id, "generated_issues.json", result.generated_issues)
    summary = _summarise(result)
    _write_summary(cfg.client_id, summary)
    _log.info("wrote plan for %s (%d rows) to %s", cfg.client_id, result.plan.total, path)
    print(summary.text, file=sys.stderr)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
