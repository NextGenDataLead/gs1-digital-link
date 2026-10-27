"""Run the content generator's producer spine (generator SPEC, commit 4).

Usage:
    python -m scripts.run_generate CLIENT_ID [--products PATH] [--emit]
    python -m scripts.run_generate CLIENT_ID --validate [--results PATH]
    python -m scripts.run_generate CLIENT_ID --backend api

The spine is producer-agnostic: it works out what copy this run needs and moves it through the
shared contract in ``lib.generator``. It

1. computes the units needing a producer (``pending_requests``), each tagged tighten or generate —
   **every unit this run will publish, every run**, except those whose feature/benefit copy (attr
   1067) is short enough to publish verbatim, which ``run_plan`` materialises straight from the
   feed; and
2. hands those units to a producer by one of two paths that write the same
   ``generation_results.json``:

   - **emit / validate** (default): ``--emit`` writes the pending requests to
     ``output/{client_id}/data/generation_requests.json`` for an in-session (Claude Code) producer
     to answer, and ``--validate`` checks the results that session wrote before a wave depends on
     them.
   - **API backend** (``--backend api``): drives :class:`lib.llm.AnthropicClient` (the headless
     Messages-API producer) over the units via :func:`run_producer` and writes the results file
     directly. Requires an enabled ``generator`` config block and its API key.

**Nothing is cached.** There is no store between runs: ``run_plan`` reads this run's results file
and publishes from it, and a later run regenerates rather than reusing. Idempotency comes from the
other end — generated copy is excluded from the content hash (#97), so writing it again does not
reclassify a live page. This pipeline fails silently; verify the emitted requests and the results
against real parsed data, not just green tests.

Only the products **in scope** are considered — the process list and the confirmed-video
allowlist, via ``lib.preflight.in_scope``, the same filter the doctor's ``scope`` check reports.
Before that this command worked from the whole catalogue and disagreed with the doctor by two
orders of magnitude.

Of those, only the ``(gtin, language)`` units a run would **create or change** are asked for
(``lib.preflight.units_needing_copy``), and only where the plan will not hold the product outright
(``lib.holds.held_units`` — E23 missing mandatory data, E24 no confirmed video, E22 no hero image).
An UNCHANGED row is never confirmed and never executed, so copy for it is text nothing will read;
a held product never becomes a row at all. On the pilot client those two together are the
difference between 74 units and 20. **That is scope, not reuse** — a unit is left out because
nothing will be published for it, never because copy for it already exists, which is the rule the
removed cache broke. When the answer cannot be decided (no state, no URL patterns, no readable
video map) every unit is asked for, because a run that quietly writes no copy for a page it is
about to publish surfaces as a blank page rather than as an error.

Emits (--emit):         output/{client_id}/data/generation_requests.json
Reads (--validate):     output/{client_id}/data/generation_results.json (writes nothing, unless
                        --results named another path, which is then placed at the canonical one)
Writes (--backend api): output/{client_id}/data/generation_results.json
Exit codes:
    0  success
    2  config error (bad client id, missing files, generator not enabled, missing key, API error)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NamedTuple

from pydantic import BaseModel, ConfigDict, ValidationError

from lib.config import ClientConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, GeneratorError, LLMAPIError, MissingCredentialError
from lib.generator import (
    DEFAULT_PROMPT_VERSION,
    MODE_TIGHTEN,
    GenerationContext,
    GenerationRequest,
    LLMClient,
    ResultItem,
    ResultsFile,
    generation_context,
    load_results,
    missing_copy,
    pending_requests,
    result_item,
    results_path,
    save_results,
)
from lib.holds import held_units
from lib.llm import AnthropicClient, load_voice_template
from lib.media_video import canon_gtin
from lib.preflight import in_scope, units_needing_copy
from lib.records import ProductRecord, SkipReason

_log = logging.getLogger("scripts.run_generate")

REQUESTS_FILENAME: Final = "generation_requests.json"

_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 2


# --- Emit/validate file contract ---------------------------------------------


class RequestsFile(BaseModel):
    """The emitted units for an in-session producer to answer (``--emit`` output).

    Carries the ``prompt_version`` and fingerprints so a session can echo them back in its
    results, letting ``--validate`` and ``run_plan`` reject copy written against inputs that have
    since changed. ``ResultsFile`` — the answer to this file — lives in ``lib.generator``, because
    ``run_plan`` and the doctor both read it and neither may import a script.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    prompt_version: str
    generated_at: datetime
    requests: list[GenerationRequest]


# --- Paths & IO --------------------------------------------------------------


def _data_path(client_id: str, filename: str) -> Path:
    """Return ``output/{client_id}/data/{filename}``."""
    return Path("output") / client_id / "data" / filename


def _default_products_path(client_id: str) -> Path:
    """The parsed-products location written by ``scripts/parse_export.py``."""
    return _data_path(client_id, "products.json")


def _load_products(path: Path) -> list[ProductRecord]:
    """Read the parsed-products JSON array into ``ProductRecord``s."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProductRecord.model_validate(item) for item in data]


# --- Preparation & the producer seam -----------------------------------------


class _Prepared(NamedTuple):
    """What this run needs a producer for, and what it decided that from."""

    requests: list[GenerationRequest]
    products: list[ProductRecord]
    #: The ``(gtin, language)`` units this run would write, or ``None`` when that could not be
    #: decided and every unit is therefore asked for. Carried so the coverage line and the
    #: validation are asked about exactly the set the producer was.
    units: set[tuple[str, str]] | None
    #: The in-scope units the plan will hold whatever is written for them, and why. Kept apart
    #: from the units left out for being already live: both are units this run writes no copy for,
    #: and only one of them is finished. Empty when ``units`` is ``None`` — nothing was narrowed.
    held: dict[tuple[str, str], SkipReason]
    #: Whether the operator named the products (``--gtins``) instead of the ledger choosing them.
    #: The "already live and unchanged" figure is derived by subtraction, and on this path the
    #: subtraction is meaningless — the named set deliberately includes units the ledger would
    #: call unchanged, and every held unit as well, so the formula went **negative**. A count that
    #: can be negative is a count nobody should be shown.
    named: bool = False
    #: Live pages that change and keep the text they carry, because ``--include-published`` was
    #: not given (:func:`lib.state.keeps_live_copy`). Units this run writes, but writes no copy for
    #: — so neither "unchanged" nor "held" may claim them.
    keeps_live: frozenset[tuple[str, str]] = frozenset()


def _named_gtins(values: list[str] | None) -> frozenset[str] | None:
    """Parse ``--gtins`` into canonical 14-digit GTINs, or ``None`` when it was not given.

    ``None`` and the empty set are different answers and the difference decides the run: ``None``
    means "use the default selection", an empty set means "the operator named nothing", and
    conflating them would turn a typo into a full regeneration of the batch.
    """
    if values is None:
        return None
    return frozenset(
        canon_gtin(item.strip()) for value in values for item in value.split(",") if item.strip()
    )


def _prepare(
    cfg: ClientConfig,
    context: GenerationContext,
    products_path: Path,
    *,
    only_gtins: frozenset[str] | None = None,
    include_published: bool = False,
) -> _Prepared:
    """Load products, narrow them to the units this run will write, and compute the requests.

    **Scope is applied here, once, because all three producer paths run through it** — ``--emit``,
    ``--validate`` and ``--backend api``. Before this, none of them knew scope existed: the products
    file was taken whole, so ``pending_requests`` was computed over the entire catalogue. On the
    pilot client that meant **224 requests emitted where 10 were in scope** — the doctor and this
    command answering the same question two orders of magnitude apart. Those are real tokens and
    real time spent writing copy for products nobody is publishing, and a content-review gate with
    hundreds of units in it, which is the surest way to make a review gate go unread.

    Narrowing happens in two steps, both imported rather than reimplemented, because a second
    implementation of "what will this run touch" is exactly what they exist to prevent:

    1. :func:`lib.preflight.in_scope` — the process list and the confirmed-video allowlist. The
       same function the doctor's ``scope`` check reports.
    2. :func:`lib.preflight.units_needing_copy` — of those, the ``(gtin, language)`` units that
       classify NEW or CHANGED *and* whose product the plan will not hold. An UNCHANGED row is
       never confirmed and never executed, so copy for it is text nothing will read; a held
       product never reaches a row at all. It returns ``None`` when it cannot decide (no state, no
       URL patterns, no readable video map), and every unit is asked for then — erring wide,
       because a run that quietly writes no copy for a page it is about to publish shows up as a
       blank page.

    The hold half of step 2 is what this docstring's own lesson looked like one gate on. Scope
    fixed 224 requests down to the process list; the holds fix **74 down to 20** on the same pilot
    client, where 17 GTINs have no confirmed video (E24) and 10 are missing mandatory source data
    (E23). Neither number is decided here — :func:`lib.holds.held_units` asks the plan's own
    predicates, so a rule can never mean one thing to the generator and another to the plan.

    **This is scope, not reuse.** A unit is left out because nothing will be published for it,
    never because copy for it already exists — see :func:`lib.generator.pending_requests`.

    Pure apart from reading its input file, the video map, and peeking at ``state.json``.
    """
    products = in_scope(cfg, _load_products(products_path))
    if only_gtins is not None:
        return _for_named(cfg, context, products, only_gtins)
    units = units_needing_copy(cfg, products, include_live=include_published)
    # Only when ``units`` is not ``None``, which is exactly the case where ``units_needing_copy``
    # has already read the video map without raising — so this cannot fail where it used not to.
    held = held_units(cfg, products) if units is not None else {}
    widened = None if units is None else units_needing_copy(cfg, products, include_live=True)
    keeps_live = frozenset() if units is None or widened is None else frozenset(widened - units)
    return _Prepared(
        pending_requests(products, context, units=units),
        products,
        units,
        held,
        keeps_live=keeps_live,
    )


def _for_named(
    cfg: ClientConfig,
    context: GenerationContext,
    products: list[ProductRecord],
    only_gtins: frozenset[str],
) -> _Prepared:
    """``--gtins``: write copy for exactly these products, in every configured language.

    The default selection asks ``state.json`` which units a run would create or change. That is
    the right question when the ledger is the truth, and this exists because it is not always: the
    ledger records what *this machine* wrote and does not travel between machines, so a second
    operator's copy can be confidently wrong about a page that is live and correct. The operator
    shell asks the **site** instead, and then needs a way to say "write copy for these".

    So this path reads no state at all. It narrows to the named products and asks for every
    language of each, which is deliberately wider than the default: a unit the ledger would call
    UNCHANGED is exactly what somebody picking a product to regenerate means to overwrite.

    Holds are computed and reported but **not applied**. A product with no confirmed video will
    not publish, so copy for it is copy nobody reads — the 74-to-20 narrowing this module's other
    path exists for. Refusing here would be different: the operator named this product, from a
    screen that showed them it was waiting on a video, and a command that silently drops what it
    was explicitly told to do is worse than one that spends a few tokens. It warns instead.

    Scope still applies. ``--gtins`` picks *within* the process list, never around it: a GTIN the
    operator has not put in scope is named back to them as unknown rather than quietly written.
    """
    ordered = [product for product in products if product.gtin14 in only_gtins]
    missing = only_gtins - {product.gtin14 for product in ordered}
    if missing:
        _log.warning(
            "--gtins named %d GTIN(s) not in scope for this client, and they are ignored: %s",
            len(missing),
            ", ".join(sorted(missing)),
        )
    units = {(product.gtin, language) for product in ordered for language in context.languages}
    held = held_units(cfg, ordered)
    if held:
        _log.warning(
            "%d of the named unit(s) belong to products the plan will hold (no confirmed video, "
            "or missing mandatory data). Copy is being written for them anyway because they were "
            "named explicitly; they will not publish until the hold is cleared.",
            len(held),
        )
    return _Prepared(
        pending_requests(ordered, context, units=units), ordered, units, held, named=True
    )


def _unchanged_units(prepared: _Prepared, context: GenerationContext) -> int:
    """How many in-scope units this run writes no copy for **because they are already live**.

    Reported rather than left implicit: "1 request" over a two-product scope needs a reason beside
    it, or the narrowing reads as a bug the first time an operator notices the number.

    The held units are subtracted rather than folded in. They are also units this run writes no
    copy for, and that is the whole of the resemblance: an unchanged unit is live and finished, a
    held one has never published and will not until its source data or its video is fixed. Counting
    the second as the first is how a hold gets read as a success.
    """
    if prepared.units is None or prepared.named:
        return 0
    return (
        len(prepared.products) * len(context.languages)
        - len(prepared.units)
        - len(prepared.held)
        - len(prepared.keeps_live)
    )


def run_producer(requests: list[GenerationRequest], client: LLMClient) -> list[ResultItem]:
    """Drive ``client`` over every pending unit and return the items to write.

    The shared producer loop for any :class:`~lib.generator.LLMClient` — the API backend and test
    fakes alike. Returns a value rather than filling a store, because there is no store; the caller
    writes the results file.
    """
    return [result_item(request, client.generate_copy(request)) for request in requests]


# --- Emit / validate ---------------------------------------------------------


def _emit(
    client_id: str,
    requests: list[GenerationRequest],
    prompt_version: str,
    now: datetime,
) -> Path:
    """Write the units an in-session producer must answer.

    Written **always**, even with nothing pending, so an empty ``requests`` list means "nothing to
    generate" rather than "no run has looked".

    This used to write the cache as well, to persist the verbatim prefill so emit and ingest could
    be called in either order. Both halves of that are gone: there is no cache, and feed-verbatim
    copy is derived at plan time rather than stored anywhere.
    """
    path = _data_path(client_id, REQUESTS_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = RequestsFile(
        client_id=client_id,
        prompt_version=prompt_version,
        generated_at=now,
        requests=requests,
    )
    path.write_text(
        json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


class _Validation(NamedTuple):
    """How a producer's results measured up against this run's units.

    ``surplus`` is separate from ``rejected`` because they mean opposite things. A rejected result
    is copy the run *wanted* and cannot use — stale, written against inputs the feed no longer
    holds. A surplus one is copy the run simply does not need, which under scoped generation is
    the ordinary state of a results file written a run ago: the batch shrinks as rows go live.
    Counting the second as the first puts an alarming number on a healthy run, and a number that
    is alarming when nothing is wrong is a number an operator stops reading.
    """

    usable: int
    rejected: int
    surplus: int


def _validate(  # noqa: PLR0913 — the run's units and the three reasons a unit is not one
    requests: list[GenerationRequest],
    results: ResultsFile,
    products: list[ProductRecord],
    units: set[tuple[str, str]] | None,
    held: dict[tuple[str, str], SkipReason],
    keeps_live: frozenset[tuple[str, str]] = frozenset(),
) -> _Validation:
    """Check a producer's results against this run's units.

    Writes nothing. It replaced ``--ingest``, which folded results into the cache: with the cache
    gone there is nothing to fold into, and ``run_plan`` reads the results file itself. What is
    left is the part that was always the point — telling the operator, before a wave, whether the
    copy that was just written actually answers this run.

    Each result is matched to a pending unit by ``(gtin, language)``. A result with no pending unit
    has **four** possible causes and the warning names which: the unit is not in this run's scope;
    its product is held out of the plan (E23/E24/E22) whatever is written for it; it is in scope
    but already live and unchanged, so nothing will be published for it (or live and changing,
    keeping its live text, because ``--include-published`` was not given); or the feed already
    supplies its copy verbatim. (There used to be another — "already cached fresh" — which cannot
    happen now that nothing is cached.) A result whose fingerprint no longer matches is rejected,
    the same decision ``run_plan`` will make and for the same reason.

    A unit answered **twice** is named rather than quietly resolved. ``run_plan`` takes the last
    entry, which is deterministic and documented, but two answers for one unit means only one of
    them was reviewed — and absorbing that silently is what #94's removed dedupe did wrong.
    """
    by_key = {(r.gtin, r.language): r for r in requests}
    in_scope_gtins = {product.gtin for product in products}
    seen: set[tuple[str, str]] = set()
    usable = 0
    rejected = 0
    surplus = 0
    for item in results.results:
        if (item.gtin, item.language) in seen:
            _log.warning(
                "%s/%s is answered more than once in this file; run_plan uses the last entry, so "
                "only one of them is the copy that publishes",
                item.gtin,
                item.language,
            )
            continue
        seen.add((item.gtin, item.language))
        request = by_key.get((item.gtin, item.language))
        if request is None:
            _log.warning(
                "no pending unit for %s/%s (%s); ignoring",
                item.gtin,
                item.language,
                _no_pending_reason(
                    item.gtin, item.language, in_scope_gtins, units, held, keeps_live
                ),
            )
            surplus += 1
            continue
        stale = (
            item.input_fingerprint is not None
            and item.input_fingerprint != request.input_fingerprint
        )
        if stale:
            _log.warning(
                "stale result for %s/%s (fingerprint mismatch — inputs changed since emit); "
                "run_plan will drop it too",
                item.gtin,
                item.language,
            )
            rejected += 1
            continue
        usable += 1
    return _Validation(usable, rejected, surplus)


def _no_pending_reason(  # noqa: PLR0913 — one set per reason it can give
    gtin: str,
    language: str,
    in_scope_gtins: set[str],
    units: set[tuple[str, str]] | None,
    held: dict[tuple[str, str], SkipReason],
    keeps_live: frozenset[tuple[str, str]] = frozenset(),
) -> str:
    """Why this run never asked for ``(gtin, language)``. Ordered widest cause first.

    The held case is named separately from the unchanged one because the two send the operator to
    different places: an unchanged unit needs nothing done, a held one needs its source data or its
    video fixed before any copy for it can ever be published.
    """
    if gtin not in in_scope_gtins:
        return "not in scope for this run"
    reason = held.get((gtin, language))
    if reason is not None:
        return f"held by the plan ({reason.value}), so this run publishes nothing for it"
    if (gtin, language) in keeps_live:
        return (
            "a live page that changes and keeps the text it has, so this run writes no copy for "
            "it — pass --include-published to write some"
        )
    if units is not None and (gtin, language) not in units:
        return "already live and unchanged, so this run publishes nothing for it"
    return "the feed supplies this unit's copy verbatim"


# --- API backend -------------------------------------------------------------


def _run_api_backend(
    cfg: ClientConfig,
    requests: list[GenerationRequest],
    prompt_version: str,
    now: datetime,
) -> tuple[int, str]:
    """Write this run's results file via the Anthropic API backend. Returns (written, model).

    Raises:
        ConfigError: The client has no enabled ``generator`` block.
        GeneratorError: The voice template for this prompt version is missing or empty.
        MissingCredentialError: The configured API-key env var is unset.
        LLMAPIError: A generation call failed or returned no usable result.
    """
    gen = cfg.generator
    if gen is None or not gen.enabled:
        raise ConfigError(
            f"--backend api requires an enabled `generator` block for {cfg.client_id!r}"
        )
    voice = load_voice_template(cfg.client_id, prompt_version)
    with AnthropicClient(gen, voice) as client:
        items = run_producer(requests, client)
    save_results(
        ResultsFile(
            client_id=cfg.client_id,
            provenance=f"api:{gen.model}",
            generated_at=now,
            results=items,
        )
    )
    return len(items), gen.model


# --- Summaries ---------------------------------------------------------------


def _coverage(
    products: list[ProductRecord],
    results: ResultsFile,
    context: GenerationContext,
    prepared: _Prepared,
) -> str:
    """Render how much of this run's copy is actually in hand (§ verify against real data).

    Asked of the results file through :func:`lib.generator.missing_copy`, which is the same
    function the doctor and ``run_plan`` resolve units with — and asked about the same units the
    producer was, so this line cannot claim coverage the plan will not have.

    "Units" without qualification would silently change meaning the moment generation was scoped:
    ``1/1 units have copy`` beside twenty in-scope units with none reads as full coverage. It says
    *to publish*, and names what it left out.
    """
    units = prepared.units
    total = len(products) * len(context.languages) if units is None else len(units)
    missing = len(missing_copy(products, results, context, units=units))
    left_out = []
    unchanged = _unchanged_units(prepared, context)
    if unchanged:
        left_out.append(f"{unchanged} already live and unchanged")
    if prepared.keeps_live:
        left_out.append(
            f"{len(prepared.keeps_live)} live and changing, keeping their live text — "
            "--include-published writes copy for them"
        )
    if prepared.held:
        left_out.append(f"{len(prepared.held)} held by the plan")
    aside = f" ({', '.join(left_out)})" if left_out else ""
    return f"{total - missing}/{total} unit(s) to publish have copy; {missing} without{aside}"


def _split(requests: list[GenerationRequest]) -> str:
    """Render the tighten/generate split of the units a producer must answer."""
    tighten = sum(1 for r in requests if r.mode == MODE_TIGHTEN)
    return f"{tighten} tighten, {len(requests) - tighten} generate"


# --- CLI ---------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_generate", description="Prepare and check generated product copy."
    )
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
        "--gtins",
        action="append",
        metavar="GTIN[,GTIN…]",
        help=(
            "Write copy for exactly these products, in every language, instead of asking "
            "state.json which units a run would create or change. Repeatable, and accepts a "
            "comma-separated list. Picks within the process list, never around it."
        ),
    )
    parser.add_argument(
        "--results",
        help=(
            "Path to the results JSON to check (default: output/{id}/data/generation_results.json)."
            " A file given here is placed at the default location once it validates."
        ),
    )
    parser.add_argument(
        "--include-published",
        action="store_true",
        help=(
            "Also write copy for pages that are already live and change. Without it they are "
            "published keeping the tagline and description they already carry"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--emit",
        action="store_true",
        help="Write the units an in-session producer must answer (default)",
    )
    mode.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Check a session's results against this run before publishing depends on them. "
            "Writes nothing unless --results named another path"
        ),
    )
    mode.add_argument(
        "--backend",
        choices=["api"],
        help="Write the results file via the API backend instead of emit/validate",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    now = datetime.now(UTC)
    try:
        cfg = get_client(args.client_id)
        products_path = (
            Path(args.products) if args.products else _default_products_path(cfg.client_id)
        )
        prompt_version = cfg.generator.prompt_version if cfg.generator else DEFAULT_PROMPT_VERSION
        context = generation_context(
            cfg.wordpress.languages,
            cfg.wordpress.default_language,
            prompt_version,
            cfg.export.gdsn_map,
            cfg.export.gdsn_extras,
        )
        prepared = _prepare(
            cfg,
            context,
            products_path,
            only_gtins=_named_gtins(args.gtins),
            include_published=args.include_published,
        )
        requests, products = prepared.requests, prepared.products

        if args.backend == "api":
            written, api_model = _run_api_backend(cfg, requests, prompt_version, now)
            results = load_results(cfg.client_id)
        elif args.validate:
            override = Path(args.results) if args.results else None
            results = load_results(cfg.client_id, override)
            checked = _validate(
                requests, results, products, prepared.units, prepared.held, prepared.keeps_live
            )
            if override is not None and override != results_path(cfg.client_id):
                # Validate *and place*: a producer may write anywhere, but only one path is the
                # one run_plan reads, and asking the operator to copy it by hand is a step to
                # forget.
                save_results(results)
        else:
            results = load_results(cfg.client_id)
            emit_path = _emit(cfg.client_id, requests, prompt_version, now)
    except (
        ConfigError,
        GeneratorError,
        MissingCredentialError,
        LLMAPIError,
        FileNotFoundError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    coverage = _coverage(products, results, context, prepared)
    if args.backend == "api":
        print(f"generated {written} via API ({api_model}); {coverage}", file=sys.stderr)
    elif args.validate:
        extra = f", {checked.surplus} surplus (not needed this run)" if checked.surplus else ""
        print(
            f"validated {checked.usable} result(s), rejected {checked.rejected}{extra}; {coverage}",
            file=sys.stderr,
        )
    else:
        _log.info("wrote %d request(s) to %s", len(requests), emit_path)
        print(
            f"emitted {len(requests)} request(s) to {emit_path} ({_split(requests)}); {coverage}",
            file=sys.stderr,
        )
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
