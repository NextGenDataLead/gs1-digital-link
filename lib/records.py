"""Canonical record schema and export-row parsing.

Implements ``docs/IMPLEMENTATION_SPEC.md`` §2 (type definitions) and §4.9
(``parse_excel_row``). :class:`ProductRecord` is the normalised, language-agnostic
internal shape produced by ``scripts/parse_export.py`` and consumed by every
downstream module (templates, WordPress client, GS1 client, QR, state).

The client-specific *bridge* from a raw export to this shape lives in the column
mapping (§3): for flat single-sheet exports here in :func:`parse_excel_row`, and
for GS1 Data Source / GDSN datapool exports in ``lib/gdsn.py`` (a spec extension —
see §3 notes).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lib.errors import ExportParseError

# --- Canonical target paths (§3.2) -------------------------------------------
#
# The set of ``ProductRecord`` field paths a column map may target. Shared with
# ``lib.config`` so an invalid mapping is caught at config-load time (edge E6).

#: Language-agnostic scalar fields addressable by a bare name.
SCALAR_TARGETS: Final[frozenset[str]] = frozenset(
    {"gtin", "brand", "gpc_brick_code", "net_content", "image_url", "category"}
)

#: Per-language fields addressable via dotted ``<field>.<lang>`` notation.
LOCALISED_TARGETS: Final[frozenset[str]] = frozenset(
    {"product_name", "description_short", "description_long"}
)

#: Prefix for free-form pass-through targets (``extras.<name>``).
_EXTRAS_PREFIX: Final = "extras"


def is_valid_target_path(path: str) -> bool:
    """Return whether ``path`` is a mappable ``ProductRecord`` field path (§3.2).

    Args:
        path: A canonical target path, e.g. ``"gtin"``, ``"product_name.nl"``,
            or ``"extras.hs_code"``.

    Returns:
        ``True`` for a language-agnostic scalar, a ``<localised>.<lang>`` path, or
        an ``extras.<name>`` path; ``False`` otherwise.
    """
    if "." not in path:
        return path in SCALAR_TARGETS
    head, _, tail = path.partition(".")
    if not tail:
        return False
    return head in LOCALISED_TARGETS or head == _EXTRAS_PREFIX


# --- Records (§2.1) ----------------------------------------------------------


class LocalisedText(BaseModel):
    """A text value that varies per language.

    Keys are ISO 639-1 codes (nl, en, fr, de, ...).

    Attributes:
        values: Mapping of language code to text.
    """

    model_config = ConfigDict(frozen=True)

    values: dict[str, str]

    def get(self, lang: str, fallback: str | None = None) -> str | None:
        """Return the text for ``lang``, else the ``fallback`` language's text.

        Args:
            lang: Preferred ISO 639-1 language code.
            fallback: Language code to fall back to when ``lang`` is absent.

        Returns:
            The matching text, or ``None`` when neither is present.
        """
        return self.values.get(lang, self.values.get(fallback) if fallback else None)


class ProductRecord(BaseModel):
    """The canonical internal shape for one product (§2.1).

    Language-agnostic at the top level; language-specific fields nested in
    :class:`LocalisedText`.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str = Field(..., pattern=r"^\d{8,14}$")
    brand: str
    product_name: LocalisedText

    gpc_brick_code: str | None = None
    net_content: str | None = None
    image_url: str | None = None
    category: str | None = None

    description_short: LocalisedText | None = None
    description_long: LocalisedText | None = None

    # Content-generator outputs (docs/clients/democlient-generator-spec.md). Net-new fields the feed
    # never writes: net-new so they stay distinguishable from feed values, per-language so ACF can
    # deliver nl/fr to one static field, and on the record so a run_plan merge step folds them into
    # the content hash before classification. ``None`` until generated.
    generated_tagline: LocalisedText | None = None
    generated_description: LocalisedText | None = None

    #: Pass-through values the feed carries once, language-agnostically (dimensions, material).
    extras: dict[str, str] = Field(default_factory=dict)

    #: Pass-through values whose source attribute is a per-language ``LanguageCode``/``Value``
    #: group. Separate from :attr:`extras` rather than widening it, so each value has exactly one
    #: home and no reader has to know which of two dicts is authoritative. Empty on a
    #: ``products.json`` written before this field existed — :meth:`extra` then falls back to the
    #: flat value, which is what those files hold.
    extras_localised: dict[str, LocalisedText] = Field(default_factory=dict)

    @property
    def gtin14(self) -> str:
        """The GTIN zero-padded to 14 digits for Digital Link URIs."""
        return self.gtin.zfill(14)

    def extra(self, name: str, language: str, fallback: str | None = None) -> str | None:
        """The pass-through extra ``name`` as it should read in ``language``.

        Every call site has to name a language, which is the point: reading an extra without one
        is how the Dutch token reached the French page. ``fallback`` is opt-in per caller because
        the right answer differs — a template renders one blob and a hole in it reads as broken,
        so it falls back; an ACF field is written independently and omitting it beats writing the
        wrong language into it.

        Args:
            name: The ``gdsn_extras`` key.
            language: The language the value is wanted in.
            fallback: Language to fall back to when ``language`` is absent.

        Returns:
            The value, or ``None`` when no extra of that name carries one for this language.
        """
        localised = self.extras_localised.get(name)
        if localised is not None:
            return localised.get(language, fallback)
        return self.extras.get(name)

    def extras_for(self, language: str, fallback: str | None = None) -> dict[str, str]:
        """Every pass-through extra as it should read in ``language``, in one mapping.

        Names whose value is absent for ``language`` (and for ``fallback``) are omitted rather
        than rendered empty, so a caller can tell "carries nothing here" from "carries a blank".
        """
        names = [*self.extras, *self.extras_localised]
        resolved = ((name, self.extra(name, language, fallback)) for name in names)
        return {name: value for name, value in resolved if value is not None}


# --- Plan types (§2.2) -------------------------------------------------------


class PlanClassification(StrEnum):
    """How a plan row compares to prior state (§2.2).

    ``HELD`` means the product was deliberately taken down (``run_unpublish``) and must
    not be re-published as a side effect of a routine run. It outranks the other three
    because it is a fact about *intent*, not about content: a held product's hashes still
    match, so without it the row classifies UNCHANGED and executing the plan quietly
    republishes what somebody chose to unpublish.
    """

    NEW = "new"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    HELD = "held"


class PlanRow(BaseModel):
    """One (GTIN, language) unit of work in a :class:`Plan` (§2.2)."""

    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    classification: PlanClassification
    title: str
    slug: str
    content_hash: str
    target_url: str
    diff: dict[str, tuple[str, str]] | None = None
    product: ProductRecord
    #: The page the operator listed for this GTIN in the process list's
    #: ``target_url_column`` — the default language's page, on a product whose page this tool
    #: did not make. Deliberately **not** folded into ``target_url``: that field is what this
    #: tool would publish at, and it is inside the content hash; this one is what already
    #: exists. One field answering both would make every listed product read CHANGED, and a
    #: pages run would believe it had published somewhere it never wrote. ``None`` = not listed.
    listed_url: str | None = None
    #: A CHANGED row of a page that is already live, planned with no generated copy for this run:
    #: it is written without the tagline and description fields, so the page keeps the text it
    #: already carries — see :func:`lib.state.keeps_live_copy`. ``False`` on every row that carries
    #: copy, and on every NEW row, which E21 still holds when it has none.
    keeps_live_copy: bool = False


class SkipReason(StrEnum):
    """Why a ``(GTIN, language)`` never became a :class:`PlanRow` at all (§8.2).

    Distinct from :class:`PlanClassification`, and deliberately so: a classification is a
    judgement about a unit that *is* in the plan, while these units are absent from it. They
    have no title, slug, hash or target URL to classify — that is precisely what is missing.
    """

    #: E18 — the product carries no ``product_name`` in this language.
    MISSING_PRODUCT_NAME = "missing_product_name"
    #: E21 — a generator is configured but this unit has no generated tagline yet.
    NO_GENERATED_COPY = "no_generated_copy"
    #: E22 — ``media.require_hero_image`` is set and the source ``image_url`` is blank.
    BLANK_HERO_IMAGE = "blank_hero_image"
    #: E23 — a ``gdsn_map`` field marked ``required`` (or a ``required_group``) has no value.
    #: Held for the whole product, in every language, so a SKU is never half-published.
    MISSING_MANDATORY_FIELD = "missing_mandatory_field"
    #: E24 — the product has no client-confirmed video in every configured language.
    NO_CONFIRMED_VIDEO = "no_confirmed_video"


class SkippedUnit(BaseModel):
    """One ``(GTIN, language)`` dropped before classification, and why (§8.2).

    These used to leave no trace but three ``WARNING SKIPPED …`` lines. Nothing counted them,
    nothing wrote them down, and ``Plan.total`` — being ``len(rows)`` — under-reported the work
    by exactly the units that had gone missing. A plan that dropped every row for want of
    generated copy (E21) looked identical to a plan with nothing to do, and the run that
    followed reported success having published nothing. That is the failure mode this whole
    project keeps designing against, so the drops are now part of the plan document.

    ``detail`` is the same sentence the warning log carries, kept verbatim so a reader of
    ``plan.json`` needs no second source.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    reason: SkipReason
    detail: str


class Plan(BaseModel):
    """A full run plan for one client (§2.2).

    ``total`` and ``counts`` describe ``rows`` only — the executable work — and mean exactly
    what they always meant. ``skipped`` sits beside them rather than inside them: a skipped
    unit is not a fifth classification but an absence, and folding it into the counts would
    change what every existing reader of a count believes it is reading.

    ``skipped`` defaults to empty so a ``plan.json`` or ``plan.confirmed.json`` written before
    it existed still validates.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    generated_at: datetime
    total: int
    counts: dict[PlanClassification, int]
    rows: list[PlanRow]
    skipped: list[SkippedUnit] = Field(default_factory=list)
    #: Built by ``run_plan --links-only``: the generated-copy rule (E21) was not applied,
    #: because a links run renders no page. Such a plan carries rows with no tagline, so
    #: ``run_execute`` refuses it for any mode that writes pages.
    links_only: bool = False


class PlanSummary(BaseModel):
    """Everything ``run_plan`` concluded about a plan that is not inside the plan (§8.2).

    The plan document says what *will* be executed. This says what happened on the way to it:
    how many products the gates removed, how many units were dropped and why, and — the one
    that changes the meaning of every other number here — whether prior state was reset from
    a corrupt file (E19), which silently turns an incremental re-run into a full rewrite.

    All of that existed only as prose on stderr, so the only way to read it was to be the
    process that ran the command. A UI, a later step, or an operator returning to a plan an
    hour old had nothing to go on. ``text`` carries the stderr line verbatim so a second
    reader renders the same words rather than a paraphrase of them.

    ``skipped`` and ``excluded`` are tallies, not lists: the per-unit detail lives in
    ``Plan.skipped``, and duplicating it here would create two records that can disagree.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    generated_at: datetime
    total: int
    counts: dict[PlanClassification, int]
    skipped: dict[SkipReason, int] = Field(default_factory=dict)
    excluded: dict[str, int] = Field(default_factory=dict)
    unmapped_categories: int = 0
    generated_issues: int = 0

    #: ``run_plan --include-published`` was used, so GTINs that are already published *and*
    #: resolvable were re-planned instead of dropped as finished. It rides here rather than
    #: only on stderr for the reason ``state_reset_from_corrupt`` does: it changes what every
    #: count below it means — a "changed" row is then a rewrite of a live page, not a first
    #: publish — and a reader an hour later has no other way to know which kind of plan this is.
    included_published: bool = False

    #: ``run_plan --links-only`` built this plan, so units with no generated copy are in it.
    #: Carried for the same reason as ``included_published``: it changes what the plan may be
    #: used for, and a reader later has no other way to tell.
    links_only: bool = False

    #: E19. Named in full rather than as ``reset``: a reader who skims must not have to
    #: guess which of several things was reset, or whether ``False`` is the alarming value.
    state_reset_from_corrupt: bool = False
    #: Where the corrupt file was quarantined — the evidence, and the only proof the reset
    #: was real. ``None`` unless a reset happened.
    state_corrupt_backup: str | None = None

    #: The stderr summary line, verbatim, including the E19 warning that leads it.
    text: str = ""


class ConfirmedPlan(BaseModel):
    """A :class:`Plan` plus the operator-confirmed subset to execute (§2.2)."""

    model_config = ConfigDict(frozen=True)

    plan: Plan
    confirmed_gtins_by_lang: set[tuple[str, str]]


# --- Run/state types (§2.3) — intentionally mutable --------------------------


class RunOutcome(BaseModel):
    """The result of processing one (GTIN, language) during a run (§2.3).

    ``failed_call`` names the request that failed — method, path, and the client's own label,
    e.g. ``POST /wp-json/wp/v2/media (upload media hero-a1b2c3d4e5f6)``. A row runs a page
    write, an ACF write, a URL verification and up to two media uploads, and ``error`` alone
    does not distinguish them: a live ``403`` reported as "failed: 403" took a re-run with the
    output captured to a file before anyone knew it was a video upload rather than the page.

    Optional because run logs written before the field exists have none, and because not every
    failure is a call (a template error, a blocked sibling). ``None`` means "not recorded", and
    readers omit it rather than guessing — the same back-compat move :class:`StateEntry` makes
    with ``title``.

    ``video_file`` is what the page was given this run: the mapping filename, ``""`` for none, and
    ``None`` when the row wrote no page or the client attaches no videos. It is what the run's
    data-quality note lists as "live without a video" — read from what was written, not from what
    the mapping said beforehand. ``video_failed`` names a confirmed file this run could not prepare,
    which the note lists separately: it is the operator's job (the file), not the client's.

    ``resolver_target`` is the page this language's GS1 link was pointed at, set when the record
    was written. It is not ``wp_url``: a links run writes no page, so ``wp_url`` stays empty, and
    a link can point at a page this tool did not make (``listed_url``). It is what the
    post-run scan check compares the resolver against.
    """

    gtin: str
    language: str
    ts: datetime
    status: str
    wp_page_id: int | None = None
    wp_url: str | None = None
    wp_featured_media_id: int | None = None
    gs1_set: bool = False
    resolver_target: str | None = None
    qr_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    failed_call: str | None = None
    video_file: str | None = None
    video_failed: str | None = None


class SourceIssue(BaseModel):
    """One defect in the source datapool, for the operator to fix upstream.

    The tool reports these rather than repairing them: the datapool is the authoritative
    record, so a value silently corrected here stays wrong in MyGS1 and comes back on the
    next export. Emitted to ``output/{client_id}/data/source_issues.json`` — a file rather
    than a log line, because the work of fixing them happens later, elsewhere, by a person.

    The eventual home for generated-content reporting too: when the LLM fills a gap the feed
    should have carried, that is the same kind of finding — a datapool gap with a suggested
    value. Success is this file shrinking to empty.

    Attributes:
        gtin: The product, so it can be found in MyGS1.
        field: Dotted path in *our* vocabulary, e.g. ``product_name.nl``. Useful for
            debugging the tool; useless for finding the field in the source system.
        source: The same field in the **source system's** vocabulary, e.g.
            ``MarketingInformation attr 1083``. This is what the operator searches MyGS1
            for — ``description_short`` exists nowhere but in this codebase, and a work
            queue naming fields nobody can find is not a work queue.
        issue: Machine-readable kind, e.g. ``brand_prefix_mismatch``.
        value: The current source value, verbatim.
        detail: One human-readable sentence: what is wrong and what to do.
        market_values: For ``value_inconsistent_across_markets`` only — the (market, value)
            pairs the field carries across GS1 target markets, highest-ranked first (so the
            first pair is the one copied into ``value``). Lets a reader compare the conflicting
            texts on the spot. Empty for every other issue kind.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str
    field: str
    source: str = ""
    issue: str
    value: str
    detail: str
    market_values: tuple[tuple[str, str], ...] = ()


class StateEntry(BaseModel):
    """Persisted state for one (GTIN, language) between runs (§2.3).

    ``title`` is the page title as last written. It is the one product field state
    keeps verbatim, so that a re-run can show a real before/after in a CHANGED row's
    diff (§10.6.2) — ``content_hash`` proves *that* something changed but, being a
    digest, can never say *what*. It is optional because state files written before
    titles were persisted have none; ``None`` means "not recorded", and the diff omits
    the title rather than guessing an old value.

    ``wp_status`` and ``retracted`` record a **deliberate take-down**, which the hashes
    cannot express: they describe what was written, not whether it is still serving.
    Without them an unpublished product is indistinguishable from a published one, so the
    next run reads its unchanged hashes, classifies it UNCHANGED, and leaves a drafted page
    carrying an enabled Digital Link — or, if the entry were deleted instead, silently
    republishes what somebody deliberately took down.

    ``retracted`` was called ``gs1_enabled`` and read as "is the resolver record enabled",
    which it never was. ``run_unpublish`` is the only thing that sets it, and
    ``lib.state._is_held`` is the only thing that reads it — both meaning *somebody took
    this down on purpose*. Under the old name a ``--only pages`` run recorded
    ``gs1_enabled: true`` with no GS1 record in existence, which was simply false; under
    this one the same entry says ``retracted: false``, which is true, because the product
    was never retracted. Whether a resolver record *exists* is already recorded, one field
    down: an empty ``gs1_link_set_hash``.

    Both default to the published condition so state files written before they existed
    load unchanged, the same back-compat move ``title`` makes — and a file carrying the old
    ``gs1_enabled`` key is translated on the way in rather than ignored, which is what
    :meth:`_accept_legacy_gs1_enabled` is for. ``retracted`` describes the GTIN, not the
    language, but is stored per-language because state is keyed ``(gtin, language)`` —
    mirroring ``gs1_link_set_hash``, which is already duplicated across an entry's
    languages for exactly that reason.

    ``video_file`` is the mapping filename the page was given, ``""`` for a page published with
    no video, and ``None`` for an entry written before it was recorded. The content hash does not
    cover the video, so this is the only thing that lets a page published without one come back
    when the client confirms a file — ``lib.state._video_arrived`` reads it. ``None`` and ``""``
    are different answers on purpose, the same distinction ``title`` draws. ``video_failed`` is a
    confirmed file the last write could not prepare (absent from its folder, or refused by ffmpeg):
    the page kept whatever video it had. ``video_failed_retry`` says whether to try that file again
    once it is in its folder: ``True`` when the failure was this machine's — the file was not in
    its folder, or ffmpeg was not installed — and ``False`` when ffmpeg refused a file it could
    read, which is retried only when the mapping names a different one, or every run would
    rewrite the live page and its GS1 record failing the same way. ``None`` predates the field and
    counts as ``True``: every such failure seen was a file not yet copied to the machine, and a
    file ffmpeg does refuse records ``False`` on that one retry.

    An **empty** ``gs1_link_set_hash`` means "page published, resolver link never written"
    — what ``run_execute --only pages`` leaves behind. It is a real value, not a missing
    one: ``lib.state._classify`` reads it and reports the row CHANGED so the links half can
    still be planned. Every state file written before ``--only`` existed carries a real
    digest, so nothing already live is affected.
    """

    wp_page_id: int
    wp_url: str
    wp_featured_media_id: int | None
    content_hash: str
    gs1_link_set_hash: str
    last_run: datetime
    title: str | None = None
    wp_status: str = "publish"
    retracted: bool = False
    video_file: str | None = None
    video_failed: str | None = None
    video_failed_retry: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_gs1_enabled(cls, data: Any) -> Any:
        """Translate the old ``gs1_enabled`` key, inverted, instead of ignoring it.

        This model sets no ``extra`` policy, so pydantic's default is to **drop** an
        unknown key. A state file written before the rename carries ``gs1_enabled``, and
        dropping it would let ``retracted`` fall to its default — turning every
        deliberately taken-down product back into an ordinary publishable one, silently,
        on the next run. That is the one migration failure with a live consequence, so it
        is translated here and asserted by a test rather than left to a default.

        An explicit ``retracted`` wins: it is the newer spelling, and a file carrying both
        was written by a newer version.
        """
        if isinstance(data, dict) and "gs1_enabled" in data:
            legacy = data.pop("gs1_enabled")
            data.setdefault("retracted", not legacy)
        return data


class State(BaseModel):
    """The full persisted state for a client (§2.3).

    ``entries`` is keyed ``entries[gtin][language]``.

    ``reset_from_corrupt`` is set by :func:`lib.state.load_state` when it recovered from a
    corrupt state file (edge E19) and is excluded from serialisation — it describes *this*
    load, not the persisted state. It exists so the reset reaches the operator in the plan
    summary they actually read: a reset silently turns an incremental re-run into a full
    rewrite (every row reclassifies as NEW), and an ERROR log line is too quiet for that.

    ``corrupt_backup`` is where the bad file was quarantined, carried alongside for the same
    reason and excluded for the same reason. The path is knowable only inside ``load_state``
    — it is stamped with the moment of the reset — so a caller that wants to *show* the
    evidence, rather than assert it exists, has to be handed it.
    """

    client_id: str
    entries: dict[str, dict[str, StateEntry]]
    reset_from_corrupt: bool = Field(default=False, exclude=True)
    corrupt_backup: str | None = Field(default=None, exclude=True)


# --- Flat single-sheet row parsing (§4.9) ------------------------------------


def _coerce_cell(value: object) -> str | None:
    """Coerce a raw spreadsheet cell to a trimmed string, or ``None`` if empty.

    Handles the openpyxl casting behaviours behind edge cases E1/E2: text GTINs
    keep their leading zeros verbatim, integer GTINs become their decimal string.

    Args:
        value: The raw cell value from openpyxl (``str``, ``int``, ``float``,
            ``bool``, ``datetime``, or ``None``).

    Returns:
        The normalised string, or ``None`` for empty/blank cells.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool):  # bool is a subclass of int — check it first
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)  # int and any other scalar


def parse_excel_row(
    row: dict[str, object],
    column_map: dict[str, str],
    extras_columns: list[str],
    default_language: str,
) -> ProductRecord:
    """Parse one flat export row into a :class:`ProductRecord` (§4.9).

    This is the flat single-sheet path. Rich GS1 Data Source / GDSN exports are
    handled by ``lib/gdsn.py`` instead.

    Args:
        row: Mapping of Excel column name to raw cell value.
        column_map: Mapping of Excel column name to a canonical target path (§3.2).
        extras_columns: Excel column names carried verbatim into ``extras`` under
            the column name as spelled.
        default_language: The language whose ``product_name`` is required (§3.3).

    Returns:
        The parsed, validated record.

    Raises:
        ExportParseError: If a required field is missing (E5) or the row fails
            record validation (e.g. a malformed GTIN). The GTIN, when known, is
            included in the message.
    """
    scalars: dict[str, str] = {}
    localised: dict[str, dict[str, str]] = {}
    extras: dict[str, str] = {}

    for col, target in column_map.items():
        val = _coerce_cell(row.get(col))
        if val is None:
            continue
        if "." in target:
            head, _, tail = target.partition(".")
            if head in LOCALISED_TARGETS:
                localised.setdefault(head, {})[tail] = val
            elif head == _EXTRAS_PREFIX:
                extras[tail] = val
            else:  # defensive — normally rejected at config load (E6)
                raise ExportParseError(f"unknown target path {target!r} in column map")
        elif target in SCALAR_TARGETS:
            scalars[target] = val
        else:  # defensive — normally rejected at config load (E6)
            raise ExportParseError(f"unknown target path {target!r} in column map")

    for name in extras_columns:
        val = _coerce_cell(row.get(name))
        if val is not None:
            extras[name] = val

    gtin = scalars.get("gtin")
    product_name = localised.get("product_name")
    if not product_name or default_language not in product_name:
        raise ExportParseError(f"GTIN {gtin or '?'}: missing product_name.{default_language}")

    return build_product_record(
        gtin=gtin,
        scalars=scalars,
        localised=localised,
        extras=extras,
    )


def build_product_record(
    *,
    gtin: str | None,
    scalars: dict[str, str],
    localised: dict[str, dict[str, str]],
    extras: dict[str, str],
    extras_localised: dict[str, dict[str, str]] | None = None,
) -> ProductRecord:
    """Assemble a :class:`ProductRecord` from collected field values.

    Shared by the flat parser and the GDSN builder so both surface a typed
    :class:`ExportParseError` (with the GTIN) instead of a raw pydantic trace.

    Args:
        gtin: The product GTIN, used only for the error message here.
        scalars: Language-agnostic field values keyed by field name.
        localised: Per-language field values keyed by field then language code.
        extras: Free-form pass-through values, language-agnostic.
        extras_localised: Per-language pass-through values keyed by name then language code.
            Optional: the flat parser has no per-language extras to collect.

    Returns:
        The validated record.

    Raises:
        ExportParseError: If record validation fails.
    """
    fields: dict[str, object] = dict(scalars)
    for field_name, values in localised.items():
        if values:
            fields[field_name] = LocalisedText(values=values)
    if extras:
        fields["extras"] = extras
    localised_extras = {
        name: LocalisedText(values=values)
        for name, values in (extras_localised or {}).items()
        if values
    }
    if localised_extras:
        fields["extras_localised"] = localised_extras
    try:
        return ProductRecord.model_validate(fields)
    except ValidationError as exc:
        raise ExportParseError(f"GTIN {gtin or '?'}: invalid product record: {exc}") from exc
