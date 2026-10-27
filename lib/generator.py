"""Content-generator core: request/result contract, per-run results seam, deterministic merge.

The generator writes the copy WordPress shows for a product — the tagline and the
``Eigenschappen`` benefit bullets — while everything else on the page stays deterministic
assembly. This module is **producer-agnostic and network-free**: it defines the
:class:`GenerationRequest`/:class:`GenerationResult` contract both producers (the in-session
producer and the headless API backend) fill, the :class:`ResultsFile` they write it into, and
the pure :func:`merge_generated` step that folds that copy onto :class:`ProductRecord` before
classification (mirroring ``run_plan._assign_categories``). See
``docs/clients/democlient-generator-spec.md``.

**Nothing is stored between runs.** Copy arrives in one ``generation_results.json`` per run and
is read once; there is no cache, so no run can skip generating. What used to be reuse is now
idempotency from the other end: generated copy is excluded from the content hash, so writing it
again does not reclassify a page.

**Scope is not reuse.** A caller may narrow generation to the units a run will actually publish
(``pending_requests(..., units=...)``, which ``run_generate`` fills from the NEW/CHANGED
classification). That looks like the removed cache and is its opposite: a unit is left out because
nothing will be written for it, never because copy for it already exists. Existing copy has no
vote anywhere in this module.

The ``input_fingerprint`` survives that change as a **validity check only, never a reuse key**.
The results file outlives the producer session that wrote it, so a ``parse_export`` re-run
between the two would otherwise publish copy describing data the feed no longer holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Collection, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lib.errors import GeneratorError
from lib.gdsn import SCALAR_SEPARATOR, GdsnSource, source_label, suspect_language
from lib.records import LocalisedText, ProductRecord, SourceIssue
from lib.units import decode_net_content, decode_unit

_log = logging.getLogger(__name__)

#: The run's copy: what the producer writes and what ``run_plan`` reads. One file per run, never
#: accumulated and never reused to skip work.
RESULTS_FILENAME: Final = "generation_results.json"

#: Provenance recorded when a results file does not name its producer — the in-session producer
#: hand-writes the file, and it is the only one that would omit it.
DEFAULT_PROVENANCE: Final = "in-session"

#: Provenance for copy taken verbatim from the feed's attr 1067, which no producer wrote.
FEED_PROVENANCE: Final = "feed:1067"

#: Default prompt version — part of every input fingerprint. Lives here (not in a script) so
#: ``run_generate`` and ``run_plan`` fingerprint identically.
DEFAULT_PROMPT_VERSION: Final = "v1"

#: Longest a feed USP (attr 1067) may be to use verbatim; a longer one is tightened by the
#: producer instead. Roughly one readable line — the live taglines run ~30-60 chars.
MAX_VERBATIM_USP_CHARS: Final = 80

#: How a unit's copy came to be — drives what the report says about it. Derived from the feed at
#: read time (see :func:`_resolve_unit`), never declared by the producer.
ORIGIN_FEED: Final = "feed"  # 1067 used verbatim (short); authoritative, not reported
ORIGIN_TIGHTENED: Final = "tightened"  # 1067 shortened by the producer; reported as adjusted
ORIGIN_GENERATED: Final = "generated"  # produced from 1083 (no usable 1067); reported as generated

#: What a pending request asks the producer to do.
MODE_TIGHTEN: Final = "tighten"  # 1067 present but too long — shorten and rank it
MODE_GENERATE: Final = "generate"  # no usable 1067 — write from 1083

#: Placeholder material values the feed carries in lieu of a real one — treated as absent.
_PLACEHOLDER_PREFIX: Final = "zzz"

#: The ``gdsn_map`` field :attr:`GenerationInputs.marketing_message` comes from. Named so the
#: "is this a missing input or a pending translation?" test can ask about the right field.
_MARKETING_MESSAGE_FIELD: Final = "description_short"

#: Per-language labels for the assembled description. Falls back to the default language's
#: labels for any language not listed (only nl/fr exist in the pilot data).
_LABELS: Final[dict[str, dict[str, str]]] = {
    "nl": {
        "eigenschappen": "Eigenschappen",
        "technische": "Technische details",
        "afmetingen": "Afmetingen",
        "materiaal": "Materiaal",
    },
    "fr": {
        "eigenschappen": "Caractéristiques",
        "technische": "Détails techniques",
        "afmetingen": "Dimensions",
        "materiaal": "Matériau",
    },
}


# --- Contract: inputs, requests, results -------------------------------------


class TranslatableField(BaseModel):
    """One source a client has opted into language-gap filling with ``translate: true``.

    Derived from ``clients.yml`` rather than listed in code, so which values are worth filling
    stays a property of the client's page — the same reasoning that keeps ``required`` and
    ``in_matrix`` in config.
    """

    model_config = ConfigDict(frozen=True)

    #: The ``gdsn_map`` field name or ``gdsn_extras`` key.
    field: str
    #: The source in the words MyGS1 uses, e.g. ``"MarketingInformation attr 1083"``. What the
    #: operator searches for when putting the filled value back.
    source_label: str
    #: Whether GS1 has a per-language slot for this attribute at all. ``False`` for a
    #: language-agnostic one (attr 4.012 Material): the translation still fixes the page, but it
    #: cannot be put back into the datapool, and a work queue that says it can wastes a trip.
    has_language_slot: bool = True


class TranslationGap(BaseModel):
    """One value the feed carries in another language but not in this one.

    Carries the source text, not just its name: the producer translates from it in the same call
    it writes the copy, and it enters the fingerprint so editing the source language supersedes
    the translation.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    source_language: str
    source_value: str
    source_label: str
    has_language_slot: bool = True


class GenerationContext(BaseModel):
    """The client facts every generation step needs, gathered once by the caller.

    Bundled rather than passed as four more arguments to each of ``prefill_from_feed`` /
    ``pending_requests`` / ``merge_generated``: they are one fact — "what this client publishes,
    in which languages, and what may be filled" — and they must always travel together. Build it
    with :func:`generation_context`.
    """

    model_config = ConfigDict(frozen=True)

    languages: list[str]
    default_language: str
    prompt_version: str
    #: Sources marked ``translate: true``, in config order. Empty means nothing is ever filled.
    translatable: list[TranslatableField] = Field(default_factory=list)


class GenerationInputs(BaseModel):
    """The source-data inputs one generation is derived from, for one language.

    These feed both the producer's prompt and the cache fingerprint, so any change to them
    invalidates the cached copy. Language-agnostic feed values (dimensions, material) are
    shared across a product's languages; the localised ones are already language-specific.
    """

    model_config = ConfigDict(frozen=True)

    functional_name: str | None = None
    marketing_message: str | None = None  # attr 1083
    feature_benefit: str | None = None  # attr 1067
    net_content: str | None = None
    dim_height: str | None = None
    dim_width: str | None = None
    dim_depth: str | None = None
    material: str | None = None
    #: Field → the other language's text this unit's translations are derived from. In the
    #: fingerprint because it is a real input: without it, editing the Dutch 1083 left the French
    #: entry looking fresh, so the translation of a value that had changed survived the edit.
    translation_sources: dict[str, str] = Field(default_factory=dict)


class GenerationResult(BaseModel):
    """The copy a producer generated for one ``(gtin, language)``.

    ``usps`` is one ranked list: ``usps[0]`` is the tagline (the page headline, the header-video
    caption, and the description's opening line), and ``usps[1:]`` are the Eigenschappen bullets.
    The Technische-details block is not here — it is assembled deterministically from feed data
    (net content, dimensions, material). ``translations`` answers the request's
    :class:`TranslationGap` list: field name → the value rendered in this language.
    """

    model_config = ConfigDict(frozen=True)

    usps: list[str] = Field(min_length=1)
    #: Field → the value translated into this request's language. Only fields the request asked
    #: for are kept; see :func:`apply_result`.
    translations: dict[str, str] = Field(default_factory=dict)
    #: Claims the producer wrote that go *beyond* the literal feed text (e.g. "snoerloos"
    #: inferred from "batterie rechargeable"). Reported as ``generation_inference`` findings
    #: so a human verifies each before publishing. Never participates in the fingerprint.
    inferences: list[str] = Field(default_factory=list)


class GenerationRequest(BaseModel):
    """One unit of copy to generate: a ``(gtin, language)`` plus its inputs and fingerprint.

    ``translations`` lists the values the feed carries in another language and not this one, each
    with the text to translate from. It replaced a single ``needs_name`` flag: the title was never
    the only value that goes missing in one language, it was only the one that stopped a page
    publishing.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    inputs: GenerationInputs
    input_fingerprint: str
    translations: list[TranslationGap] = Field(default_factory=list)
    mode: str = MODE_GENERATE  # MODE_TIGHTEN to shorten a long 1067, else MODE_GENERATE
    candidates: list[str] = Field(default_factory=list)  # the 1067 USPs to tighten (MODE_TIGHTEN)


class LLMClient(Protocol):
    """A copy producer: turns one :class:`GenerationRequest` into a :class:`GenerationResult`.

    The seam both producers satisfy — the headless API backend (``lib.llm``) and test fakes —
    so ``scripts/run_generate.py`` can drive either through one loop. The Protocol takes no
    network dependency itself, and a producer is only ever called for the units
    :func:`pending_requests` reports — which is every unit this run will publish whose copy the
    feed does not already supply, on every run.
    """

    def generate_copy(self, request: GenerationRequest) -> GenerationResult:
        """Produce copy for ``request`` (its ``mode`` says tighten a 1067 or generate from 1083)."""
        ...


class ResultItem(BaseModel):
    """One ``(gtin, language)`` of copy in a run's results file.

    This is the shape the in-session producer hand-writes, so it holds only what a producer can
    honestly answer: the copy, the translations it was asked for, and the fingerprint of the
    request it answered. How the copy came to be (:data:`ORIGIN_TIGHTENED` vs
    :data:`ORIGIN_GENERATED`) is **not** here — the feed already answers that, and recomputing it
    at read time is what stops a hand-edited file mislabelling its own copy.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    usps: list[str] = Field(min_length=1)
    #: Field → the value this language was missing, as the producer rendered it.
    translations: dict[str, str] = Field(default_factory=dict)

    @field_validator("usps")
    @classmethod
    def _clean_usps(cls, value: list[str]) -> list[str]:
        """Strip each USP and drop the blanks, requiring at least one to survive.

        Cleaned in the model rather than at each reader, so ``usps`` is clean everywhere it is
        read — including in a file a human wrote by hand, which is the one path that does not go
        through :func:`result_item`. A list of nothing but whitespace passes ``min_length`` and
        would otherwise reach the page as a tagline that is not there.
        """
        cleaned = [stripped for stripped in (item.strip() for item in value) if stripped]
        if not cleaned:
            raise ValueError("at least one non-blank USP is required")
        return cleaned

    #: The request's fingerprint, echoed back. Optional because a hand-written file may omit it;
    #: when present it is checked on read, and a mismatch drops the copy rather than publishing
    #: text about data the feed no longer holds. **Never a reuse key.**
    input_fingerprint: str | None = None
    #: Claims written beyond the literal feed text, surfaced as ``generation_inference``
    #: findings in the generated-content report. Never participates in the fingerprint.
    inferences: list[str] = Field(default_factory=list)


class ResultsFile(BaseModel):
    """One run's generated copy — what a producer writes and ``run_plan`` reads.

    Replaced the accumulating cache. It is not a store: nothing merges a previous run's file into
    this one, and holding copy for a unit never lets that unit skip generation next time.

    ``provenance`` sits on the file rather than on each item because one producer writes one file
    per run, so a per-item field could not carry anything the file does not.
    """

    client_id: str
    provenance: str = DEFAULT_PROVENANCE
    #: When the producer wrote it. Optional — the in-session producer hand-writes this file.
    generated_at: datetime | None = None
    results: list[ResultItem] = Field(default_factory=list)


class GeneratedUnit(BaseModel):
    """One unit's copy as this run resolved it, ready for the merge and the report.

    Assembled in memory by :func:`_resolve_unit` from either the feed (short attr 1067, used
    verbatim) or the run's results file. Never serialised: ``origin``, ``provenance`` and
    ``source_input`` are derived, so there is nowhere for them to drift out of step with the feed.
    """

    model_config = ConfigDict(frozen=True)

    usps: list[str]
    translations: dict[str, str] = Field(default_factory=dict)
    origin: str  # ORIGIN_FEED | ORIGIN_TIGHTENED | ORIGIN_GENERATED
    provenance: str
    source_input: str
    inferences: list[str] = Field(default_factory=list)


# --- Results IO (mirrors lib.state atomic write) -----------------------------


def results_path(client_id: str) -> Path:
    """Return the results path (``output/{client_id}/data/generation_results.json``)."""
    return Path("output") / client_id / "data" / RESULTS_FILENAME


def load_results(client_id: str, path: Path | None = None) -> ResultsFile:
    """Load a client's results for this run, or an empty one when none exists.

    Args:
        client_id: The client whose results to load.
        path: Where to read from, for ``run_generate --validate --results PATH``. Defaults to
            this client's canonical location. The client check below applies either way — that
            is the point of routing the override through here rather than around it.

    Returns:
        The results file, or an empty one when no file is present. Absent is not an error: it
        means no copy this run, and every unit is then held out of the plan under E21. The
        doctor's ``generation_results`` check is what makes that loud *before* a wave.

    Raises:
        GeneratorError: If the file exists but cannot be read or parsed, or names another
            client. The client check lived in ``run_generate --ingest``; it belongs here so
            every reader gets it, ``run_plan`` included — that one publishes.
    """
    path = path or results_path(client_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ResultsFile(client_id=client_id)
    except OSError as exc:
        raise GeneratorError(f"cannot read results for {client_id!r} at {path}: {exc}") from exc
    try:
        results = ResultsFile.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GeneratorError(f"results for {client_id!r} at {path} are corrupt: {exc}") from exc
    if results.client_id != client_id:
        raise GeneratorError(
            f"results at {path} are for {results.client_id!r}, not {client_id!r}; "
            "copy is not interchangeable between clients"
        )
    return results


def save_results(results: ResultsFile) -> None:
    """Atomically persist ``results`` to ``output/{client_id}/data/generation_results.json``.

    Writes to a temporary file in the destination directory and ``os.replace``s it over the
    target, so a crash mid-write leaves either the old file or the whole new one.

    Args:
        results: The results to persist; their ``client_id`` determines the path.

    Raises:
        GeneratorError: If the directory or file cannot be written.
    """
    path = results_path(results.client_id)
    payload = json.dumps(results.model_dump(mode="json"), ensure_ascii=False, indent=2)
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
        raise GeneratorError(
            f"cannot write results for {results.client_id!r} at {path}: {exc}"
        ) from exc


# --- Fingerprint & input gathering -------------------------------------------


def _fingerprint(inputs: GenerationInputs, language: str, prompt_version: str) -> str:
    """Return a stable SHA-256 over the inputs, language, and prompt version.

    Canonical (sorted keys, fixed separators) like ``lib.state.compute_content_hash``, so it is
    deterministic across runs. The producer/model is deliberately excluded: the in-session and
    API backends are interchangeable producers of the same logical copy, so which one wrote a
    results file must not decide whether that file is still about this product.
    """
    canonical = json.dumps(
        {
            "inputs": inputs.model_dump(mode="json"),
            "language": language,
            "prompt_version": prompt_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_placeholder(value: str | None) -> bool:
    """Whether a material value is a datapool placeholder rather than a real material."""
    return value is not None and value.strip().casefold().startswith(_PLACEHOLDER_PREFIX)


def _without_placeholders(value: str | None) -> str | None:
    """``value`` minus any placeholder or empty slot; ``None`` when no real slot is left.

    ``Material`` repeats in the feed and the parser joins its slots, so testing the whole string
    for a ``zzz…`` prefix stopped answering the question it was asked: ``kunststof, zzzanders``
    reads as an ordinary material, which would put the junk on the page *and* report it in §4 as
    a value to paste into MyGS1 — a blank turned into fabricated master data, the exact failure
    the placeholder rule exists to prevent.

    Slots are dropped by the same rule whether or not the value contains a placeholder. Skipping
    the walk for a value that has none would be the cheaper branch, but it would make an empty
    slot survive in ``a, , b`` and vanish in ``a, , zzzanders`` — one input's rendering deciding
    another's. Nothing legitimate is at risk from always walking: ``sep.join(s.split(sep))`` is an
    identity, so a value whose slots are all real comes back unchanged, punctuation included, and
    a single-slot value that merely *contains* a comma is never split into anything droppable.
    """
    if value is None:
        return None
    kept = [
        part for part in value.split(SCALAR_SEPARATOR) if part.strip() and not _is_placeholder(part)
    ]
    return SCALAR_SEPARATOR.join(kept) or None


def _material(product: ProductRecord, language: str, fallback: str) -> str | None:
    """The product's material for ``language``, or ``None`` when absent or a placeholder.

    Falls back to the default language: until the producer has translated it, a French page
    showing the Dutch word beats one showing no material at all.
    """
    return _without_placeholders(product.extra("material", language, fallback))


def generation_context(  # noqa: PLR0913 — each argument is a distinct client fact
    languages: list[str],
    default_language: str,
    prompt_version: str,
    gdsn_map: dict[str, GdsnSource],
    gdsn_extras: dict[str, GdsnSource],
) -> GenerationContext:
    """Bundle the client facts the generation steps need, reading ``translate`` from config.

    Args:
        languages: The site's configured languages, in config order.
        default_language: The language the feed is authored in.
        prompt_version: The active prompt version (part of every fingerprint).
        gdsn_map: The client's ``export.gdsn_map``.
        gdsn_extras: The client's ``export.gdsn_extras``.

    Returns:
        The context. ``translatable`` holds every source marked ``translate: true``, mapped fields
        before extras, each carrying the MyGS1 words for its attribute.
    """
    translatable = [
        TranslatableField(
            field=name,
            source_label=source_label(src),
            # A per-language GDSN attribute has a slot the filled value can go back into; a
            # language-agnostic one (attr 4.012 Material) does not.
            has_language_slot=src.localised,
        )
        for sources in (gdsn_map, gdsn_extras)
        for name, src in sources.items()
        if src.translate
    ]
    return GenerationContext(
        languages=list(languages),
        default_language=default_language,
        prompt_version=prompt_version,
        translatable=translatable,
    )


def _is_foreign(language: str, value: str) -> bool:
    """Whether ``value``, sitting in ``language``'s slot, reads as a different language.

    **The one predicate behind both halves of the wrong-language rule**, and it has to be: it is
    read once to decide the slot needs filling (:func:`_carried`, via :func:`_field_values`) and
    again to decide the feed does not get to overwrite what was filled
    (:func:`_stored_values`, via :func:`_apply_translations`). Answer it in one place and not the
    other and the producer writes a French name that is then replaced by the Dutch one it was
    written to correct — a fix that runs, reports success, and changes nothing.

    The detection itself is :func:`lib.gdsn.suspect_language`, the same function the report's §3b
    flag comes from, so the value the operator is told about is exactly the value that gets filled.
    """
    return suspect_language(value, language) is not None


def _carried(values: dict[str, str]) -> dict[str, str]:
    """The languages this field really has a value in — blank, placeholder and foreign dropped.

    Three ways a slot can hold no usable value for its language:

    * **Blank.** Nothing there.
    * **A ``zzz…`` placeholder** — the feed saying it has no value. :func:`_material` already reads
      it that way for the prompt. Reading it as text to translate would ask the producer to render
      a placeholder into French and then tell the operator to paste that into MyGS1, turning a
      blank into fabricated master data. Cleaned rather than merely filtered, because a repeated
      attribute can be part real and part placeholder: ``kunststof, zzzanders`` is a language the
      feed *does* carry, and what it carries is ``kunststof`` (:func:`_without_placeholders`).
    * **Another language's text**, e.g. a French ``product_name`` reading ``Schoonmaakdoek``. The
      slot is full and the language is still missing, so treating it as carried leaves the page
      permanently untranslated: no gap, no request, nothing to report as filled. Dropping it makes
      it an ordinary gap and the authoritative language fills it (:func:`_is_foreign`).

    Note what the third one is *not*: it is not "translate the wrong-language text". The value is
    discarded and the other language is rendered instead, so nl and fr end up naming the same
    product. Where the two slots disagreed about more than language, that disagreement is a source
    defect and stays visible in the report rather than being laundered into French.
    """
    cleaned = {lang: _without_placeholders(text) for lang, text in values.items()}
    return {
        lang: text
        for lang, text in cleaned.items()
        if text and text.strip() and not _is_foreign(lang, text)
    }


def _field_values(product: ProductRecord, field: str, default_language: str) -> dict[str, str]:
    """Every language ``product`` carries ``field`` in, whatever shape it is stored in.

    A language-agnostic extra counts as carrying its one value in the default language: that is
    the language the feed is authored in, and treating it as language-less would mean no other
    language could ever be seen as missing it.
    """
    localised = product.extras_localised.get(field)
    if localised is not None:
        return _carried(localised.values)
    flat = product.extras.get(field)
    if flat is not None:
        return _carried({default_language: flat})
    value = getattr(product, field, None)
    if isinstance(value, LocalisedText):
        return _carried(value.values)
    return {}


def translation_gaps(
    product: ProductRecord, language: str, context: GenerationContext
) -> list[TranslationGap]:
    """Every translatable value ``product`` lacks in ``language`` but carries in another.

    **Never fills from nothing.** A field blank in every configured language yields no gap: there
    is nothing to derive from, so it stays a source finding for MyGS1 (E23) rather than becoming
    invented product data. That line is the whole reason this is defensible — rendering a value
    the feed already holds into a second language is translation; writing one that exists nowhere
    is invention, and this tool does not do it.

    The source language is the default language when it carries the value, else the first
    configured language that does — so the translation is made from the feed's authored text
    rather than from another translation, wherever there is a choice.

    Args:
        product: The record to inspect (pre-merge — the feed's own values).
        language: The language being generated for.
        context: The client context; its ``translatable`` list is what may be filled at all.

    Returns:
        One gap per missing value, in ``context.translatable`` order. Empty when nothing is missing,
        nothing is translatable, or every candidate is blank everywhere.
    """
    gaps: list[TranslationGap] = []
    for candidate in context.translatable:
        values = _field_values(product, candidate.field, context.default_language)
        if language in values:
            continue
        ranked = [context.default_language, *context.languages]
        source_language = next((lang for lang in ranked if lang in values), None)
        if source_language is None:
            # Blank in every configured language — the guard rail. There is nothing to derive
            # from, so this stays a source finding for MyGS1 (E23) rather than becoming an
            # invented value. Also covers a value carried only in a language nobody publishes.
            continue
        gaps.append(
            TranslationGap(
                field=candidate.field,
                source_language=source_language,
                source_value=values[source_language],
                source_label=candidate.source_label,
                has_language_slot=candidate.has_language_slot,
            )
        )
    return gaps


def _functional_name(product: ProductRecord, language: str, default_language: str) -> str | None:
    """What the product *is*, for the copy: ``extras.functional_name`` where a client declares one.

    The same as the page title until a client titles its pages with something else. Democlient
    did (operator, 2026-10-22): the title became the short name (3297, "Dizzy Dog"), while the
    copy is still written about the functional name (3301, "Hondenspeelgoed") — a brand name says
    nothing a tagline can be written from. Without an ``extras.functional_name`` it is the title,
    as it always was, so every other client's fingerprints are unchanged.
    """
    declared = product.extra("functional_name", language, fallback=default_language)
    return declared or product.product_name.get(language, default_language)


def _gather_inputs(
    product: ProductRecord, language: str, context: GenerationContext, gaps: list[TranslationGap]
) -> GenerationInputs:
    """Assemble the generation inputs for one ``(gtin, language)`` from the record."""
    return GenerationInputs(
        # Named for the producer's vocabulary — the prompt and the content-generator skill both
        # call it the functional name — but read from ``product_name``, which *is* attr 3301. It
        # used to read an ``extras.functional_name`` declared against that same attribute, with
        # the mapped field as a fallback: one value under two names, and this was the read that
        # made the duplication look load-bearing. The default-language fallback is the part worth
        # keeping, and is now a stated rule rather than a side effect of how extras were stored: a
        # unit with no name in its own language is seeded with the Dutch one, because that is what
        # the copy describes.
        functional_name=_functional_name(product, language, context.default_language),
        marketing_message=(
            product.description_short.get(language) if product.description_short else None
        ),
        feature_benefit=(
            product.description_long.get(language) if product.description_long else None
        ),
        net_content=product.net_content,
        dim_height=product.extras.get("dim_height"),
        dim_width=product.extras.get("dim_width"),
        dim_depth=product.extras.get("dim_depth"),
        material=_material(product, language, context.default_language),
        translation_sources={gap.field: gap.source_value for gap in gaps},
    )


def _prepare_unit(
    product: ProductRecord, language: str, context: GenerationContext
) -> tuple[GenerationInputs, list[TranslationGap], str]:
    """The inputs, translation gaps, and fingerprint for one ``(gtin, language)``.

    One function because the three are computed together everywhere: the gaps feed the inputs,
    and the inputs are what the fingerprint is over.
    """
    gaps = translation_gaps(product, language, context)
    inputs = _gather_inputs(product, language, context, gaps)
    return inputs, gaps, _fingerprint(inputs, language, context.prompt_version)


def _feature_candidates(inputs: GenerationInputs) -> list[str]:
    """Split the joined 1067 feature/benefit text into candidate USPs (newline-separated)."""
    if not inputs.feature_benefit:
        return []
    return [line.strip() for line in inputs.feature_benefit.split("\n") if line.strip()]


def _all_short(candidates: list[str]) -> bool:
    """Whether every candidate USP is short enough to use verbatim."""
    return all(len(c) <= MAX_VERBATIM_USP_CHARS for c in candidates)


def _feed_verbatim(inputs: GenerationInputs) -> list[str] | None:
    """The feed's 1067 USPs when they are short enough to publish as they stand, else ``None``.

    One helper for both sides of the decision: :func:`pending_requests` skips a unit because of it,
    and :func:`merge_generated` materialises that unit's copy because of it. They must agree — a
    unit skipped here and not materialised there publishes nothing at all — and #96's lesson is
    that one rule answered independently on two paths drifts into meaning two things.

    This is what became of ``prefill_from_feed``. It used to write these units into the cache; with
    no cache there is nowhere to write them, so the rule is applied where the copy is read instead.
    The feed's own short 1067 *is* the ranked USP list, so no producer is needed and no tokens are
    spent — 8 of 254 units in the pilot catalogue, none of them currently in scope.
    """
    candidates = _feature_candidates(inputs)
    if candidates and _all_short(candidates):
        return candidates
    return None


def pending_requests(
    products: list[ProductRecord],
    context: GenerationContext,
    units: Collection[tuple[str, str]] | None = None,
) -> list[GenerationRequest]:
    """Return the ``(gtin, language)`` units needing a producer this run, each with its mode.

    **Every unit the caller asks about is pending, every run.** There is no store, so nothing can
    report a unit as already done. Two things narrow the answer, and neither is reuse:

    - A unit whose 1067 is short enough to publish verbatim never needs a producer at all, and
      :func:`merge_generated` derives its copy from the feed instead (see :func:`_feed_verbatim`).
    - ``units``, when the caller supplies it, is **scope, not reuse**: the units a run will
      actually write, which ``run_generate`` takes from the classification (NEW and CHANGED — an
      UNCHANGED row is never executed, so copy for it is text nothing will read). A unit is
      excluded because nothing will be published for it, *never* because copy for it exists
      somewhere. That distinction is the whole difference between this and the cache that was
      removed, and it is worth re-reading whenever this argument looks like a freshness check.

    ``mode`` is :data:`MODE_TIGHTEN` when the feed carries 1067 copy that is too long to use as-is
    (the producer shortens and ranks it), else :data:`MODE_GENERATE` (the producer writes from
    1083).

    Args:
        products: The parsed products, already narrowed to this run's scope by the caller.
        context: The client context — languages, prompt version, and what may be translated.
        units: The ``(gtin, language)`` pairs this run will publish, or ``None`` to ask for every
            unit. ``None`` keeps this function ignorant of run state, which is where that
            knowledge belongs; an **empty** collection is a decision, not an absent one, and asks
            for nothing.

    Returns:
        The pending requests, each carrying its inputs, fingerprint, mode, 1067 candidates, and
        the language gaps the producer must translate.
    """
    requests: list[GenerationRequest] = []
    for product in products:
        for language in context.languages:
            if units is not None and (product.gtin, language) not in units:
                continue
            inputs, gaps, fingerprint = _prepare_unit(product, language, context)
            if _feed_verbatim(inputs) is not None:
                continue  # the feed already wrote it; merge_generated materialises it
            candidates = _feature_candidates(inputs)
            mode = MODE_TIGHTEN if candidates else MODE_GENERATE
            requests.append(
                GenerationRequest(
                    gtin=product.gtin,
                    language=language,
                    inputs=inputs,
                    input_fingerprint=fingerprint,
                    translations=gaps,
                    mode=mode,
                    candidates=candidates,
                )
            )
    return requests


def _requested_translations(request: GenerationRequest, result: GenerationResult) -> dict[str, str]:
    """The result's translations, narrowed to the fields the request actually asked for.

    The write-side half of "the feed always wins": a producer that volunteers a value for a
    language the feed already carries has it dropped here, before it can reach the results file.
    :func:`merge_generated` enforces the same rule again on read — which is not belt and braces,
    because the in-session producer hand-writes that file and never passes through here at all.
    """
    asked = {gap.field for gap in request.translations}
    return {
        field: value.strip()
        for field, value in result.translations.items()
        if field in asked and value.strip()
    }


def _source_input(inputs: GenerationInputs) -> str:
    """The source-language text a generation was derived from, for the report's audit column."""
    return inputs.feature_benefit or inputs.marketing_message or inputs.functional_name or ""


def result_item(request: GenerationRequest, result: GenerationResult) -> ResultItem:
    """Validate a producer's result into the :class:`ResultItem` a results file holds.

    Pure: it builds a value rather than upserting into a store, because there is no store. Used by
    the API backend and by tests; the in-session producer writes the same shape by hand.

    Args:
        request: The request this result answers (supplies gtin/language/fingerprint).
        result: The producer's copy.

    Returns:
        The item to write into this run's :class:`ResultsFile`.

    Raises:
        GeneratorError: If the result has no usable USPs. The rule lives on
            :class:`ResultItem`; this only restates the failure in terms of the unit it
            happened to, which a bare pydantic error does not name.
    """
    try:
        return ResultItem(
            gtin=request.gtin,
            language=request.language,
            usps=list(result.usps),
            translations=_requested_translations(request, result),
            input_fingerprint=request.input_fingerprint,
            inferences=list(result.inferences),
        )
    except ValidationError as exc:
        raise GeneratorError(
            f"empty generation result for {request.gtin}/{request.language}: usps={result.usps!r}"
        ) from exc


# --- Deterministic assembly --------------------------------------------------


def _labels(language: str) -> dict[str, str]:
    """Return the per-language labels, defaulting to Dutch for an unlisted language."""
    return _LABELS.get(language, _LABELS["nl"])


def _combine_title(name: str, variation: str | None) -> str:
    """Combine a functional name with a product variation, avoiding duplication.

    Blind concatenation produces "Snoeischaar snoeischaar"; a variation already contained in the
    name (case-insensitively) is dropped, otherwise it is appended.
    """
    if not variation:
        return name
    if variation.casefold() in name.casefold():
        return name
    return f"{name} {variation}"


def _bullets_block(heading: str, bullets: list[str]) -> str:
    """Render a ``<p><strong>heading</strong><br />• …</p>`` block."""
    lines = "<br />".join(f"• {b}" for b in bullets)
    return f"<p><strong>{heading}</strong><br />{lines}</p>"


def _dimensions_bullet(product: ProductRecord, language: str, fallback: str) -> str | None:
    """Render the "H × W × D unit" dimensions bullet, or ``None`` when incomplete.

    Every pilot product carries all three dimensions in the same unit (``MMT``); the bullet is
    emitted only when all three are present so a partial measurement never renders half a size.
    """
    raw = [product.extras.get(k) for k in ("dim_height", "dim_width", "dim_depth")]
    if not all(raw):
        return None
    values: list[str] = []
    unit_word: str | None = None
    for cell in raw:
        assert cell is not None  # guarded by all(raw)
        number, separator, code = cell.rpartition(" ")
        if not separator:
            return None  # no unit code — do not guess a dimension line
        values.append(number)
        unit_word = decode_unit(code, language, fallback_language=fallback) or code
    label = _labels(language)["afmetingen"]
    return f"{label}: {' × '.join(values)} {unit_word}"


def _technische_details(product: ProductRecord, language: str, fallback: str) -> list[str]:
    """Assemble the deterministic Technische-details bullets from feed data.

    Net content and dimensions decode their unit codes per language. Material is one
    language-agnostic value in the feed (attr 4.012 has no ``LanguageCode`` pair), so it renders
    in whatever language it was authored in until the generator has translated it — with
    ``translate: true`` set on it, this reads the translation once one exists, and falls back to
    the authored word until then.
    """
    bullets: list[str] = []
    net_content = decode_net_content(product.net_content, language, fallback_language=fallback)
    if net_content:
        bullets.append(net_content)
    dimensions = _dimensions_bullet(product, language, fallback)
    if dimensions:
        bullets.append(dimensions)
    material = _material(product, language, fallback)
    if material:
        bullets.append(f"{_labels(language)['materiaal']}: {material}")
    return bullets


def _assemble_description(
    usps: list[str], product: ProductRecord, language: str, fallback: str
) -> str:
    """Assemble the three-part ``product_description`` HTML blob from the USP list and feed data.

    ``usps[0]`` is the tagline (``<p><strong>…</strong></p>``); ``usps[1:]`` are the generated
    Eigenschappen bullets; the Technische-details bullets are assembled deterministically from net
    content, dimensions, and material. Matches the live page shape.
    """
    labels = _labels(language)
    parts = [f"<p><strong>{usps[0]}</strong></p>"]
    if len(usps) > 1:
        parts.append(_bullets_block(labels["eigenschappen"], usps[1:]))
    technische = _technische_details(product, language, fallback)
    if technische:
        parts.append(_bullets_block(labels["technische"], technische))
    return "\n".join(parts)


def _missing_input_issue(gtin: str, language: str, inputs: GenerationInputs) -> SourceIssue:
    """Flag a blank marketing message (attr 1083) — the primary input for USP generation.

    Reported even when 1067 lets generation proceed, because 1083 is the field the datapool
    should carry; the detail notes whether the feature/benefit fallback exists.
    """
    has_fallback = bool(inputs.feature_benefit)
    detail = (
        f"No marketing message (attr 1083) for {language}; "
        + (
            "USPs can still be seeded from feature/benefit (1067), but "
            if has_fallback
            else "there is nothing to generate USPs from — "
        )
        + "add marketing copy in MyGS1."
    )
    return SourceIssue(
        gtin=gtin,
        field=f"description_short.{language}",
        source="MarketingInformation attr 1083",
        issue="missing_generation_input",
        value="",
        detail=detail,
    )


def _content_issue(gtin: str, language: str, unit: GeneratedUnit) -> SourceIssue | None:
    """Report generated or adjusted copy; verbatim feed copy needs no report.

    Feed copy (attr 1067 used as-is) is authoritative and reported nowhere. Tightened copy — the
    feed's 1067 was too long and the producer shortened it — is flagged so a human confirms the
    shortening and fixes 1067 at source. Fully generated copy (no usable 1067) is flagged for
    review with its source-language input.
    """
    if unit.origin == ORIGIN_FEED:
        return None
    if unit.origin == ORIGIN_TIGHTENED:
        return SourceIssue(
            gtin=gtin,
            field=f"generated_description.{language}",
            source="adjusted from TradeItemFeatureBenefit attr 1067",
            issue="content_adjusted",
            value=unit.source_input,
            detail=(
                f"1067 copy for {language} was too long and was shortened ({unit.provenance}); "
                "review the adjusted copy and tighten attr 1067 at the source."
            ),
        )
    return SourceIssue(
        gtin=gtin,
        field=f"generated_description.{language}",
        source="generated (usps: tagline + Eigenschappen)",
        issue="content_generated",
        value=unit.source_input,
        detail=(
            f"Tagline and Eigenschappen for {language} were generated ({unit.provenance}); "
            "review the copy before publishing."
        ),
    )


def _inference_issues(gtin: str, language: str, unit: GeneratedUnit) -> list[SourceIssue]:
    """One :class:`SourceIssue` per claim the producer inferred beyond the feed text.

    Inferences are honest-but-derived claims (e.g. "snoerloos" from "batterie rechargeable"):
    plausible, but not literally in attr 1083/1067. Each is surfaced so a human confirms it is
    true for this product before the copy goes live — a claim that turns out false is a defect
    to fix at source, exactly like the other generated-content findings.
    """
    return [
        SourceIssue(
            gtin=gtin,
            field=f"generated_description.{language}",
            source="inferred beyond feed copy (attr 1083/1067)",
            issue="generation_inference",
            value=claim,
            detail=(
                f"'{claim}' was inferred for {language} beyond the literal feed copy; "
                "verify it is true for this product before publishing."
            ),
        )
        for claim in unit.inferences
    ]


def _translated_issue(gtin: str, language: str, gap: TranslationGap, value: str) -> SourceIssue:
    """Report one value the tool rendered into a language the feed did not carry it in.

    The point of the finding is the feedback loop, not the confession: ``value`` is the text to
    paste into MyGS1, so the next export carries it for real and the tool stops writing it. A
    language-agnostic attribute has nowhere to paste it, and the detail says so rather than
    sending the operator looking for a field that does not exist.
    """
    destination = (
        f"add it to {gap.source_label} for {language} in MyGS1"
        if gap.has_language_slot
        else f"{gap.source_label} is language-agnostic in GS1, so there is no {language} slot "
        "to hold this — it stays a page-only translation"
    )
    # The caveat rides on `source` as well as in the detail, because the report's table renders
    # the source and not the detail: a row saying only "attr Material" sends the operator into
    # MyGS1 looking for a French field that does not exist.
    source = (
        gap.source_label
        if gap.has_language_slot
        else f"{gap.source_label} — no per-language slot in GS1"
    )
    return SourceIssue(
        gtin=gtin,
        field=f"{gap.field}.{language}",
        source=source,
        issue="value_translated",
        value=value,
        detail=(
            f"{gap.field} was written for {language} by translating the {gap.source_language} "
            f"value ('{gap.source_value}'), which the feed carries; {destination}."
        ),
    )


class _UnitRead(NamedTuple):
    """What one ``(gtin, language)`` contributed: the copy resolved for it, and its gaps."""

    unit: GeneratedUnit | None
    gaps: list[TranslationGap]
    inputs: GenerationInputs


class _Copy(NamedTuple):
    """This run's copy, indexed for lookup, with the provenance of the file it came from."""

    by_unit: dict[tuple[str, str], ResultItem]
    provenance: str


def _index(results: ResultsFile) -> _Copy:
    """Index a results file by ``(gtin, language)``.

    A later duplicate wins, which is the same rule a dict literal follows and the only one that
    does not depend on how a hand-written file happens to be ordered.
    """
    return _Copy(
        by_unit={(item.gtin, item.language): item for item in results.results},
        provenance=results.provenance,
    )


def _resolve_unit(
    gtin: str, language: str, inputs: GenerationInputs, fingerprint: str, copy: _Copy
) -> GeneratedUnit | None:
    """The copy for one unit: the feed's own if it is usable, else this run's result, else none.

    The feed is consulted first and wins. That is the order ``prefill_from_feed`` established by
    running before ``pending_requests``, and keeping it means a stray result for a verbatim unit
    cannot quietly replace the client's own words.

    A result whose echoed fingerprint no longer matches is dropped, loudly. It is the only thing
    the fingerprint still does: the results file outlives the producer session that wrote it, so
    without the check a ``parse_export`` re-run in between would publish copy about data the feed
    no longer holds — silently, which is how wrong pages used to happen.
    """
    verbatim = _feed_verbatim(inputs)
    if verbatim is not None:
        return GeneratedUnit(
            usps=verbatim,
            origin=ORIGIN_FEED,
            provenance=FEED_PROVENANCE,
            source_input=inputs.feature_benefit or "",
        )
    item = copy.by_unit.get((gtin, language))
    if item is None:
        return None
    if item.input_fingerprint is not None and item.input_fingerprint != fingerprint:
        _log.warning(
            "stale copy for %s/%s: written for inputs the feed no longer has, so it is dropped "
            "and the unit is held out of the plan (E21); re-run the generate cycle",
            gtin,
            language,
        )
        return None
    return GeneratedUnit(
        # Already stripped and non-empty: `ResultItem` cleans on validation, so there is no
        # second cleaning step here that could disagree with it.
        usps=list(item.usps),
        translations=dict(item.translations),
        # Derived, not declared: the feed already answers how this copy came to be.
        origin=ORIGIN_TIGHTENED if _feature_candidates(inputs) else ORIGIN_GENERATED,
        provenance=copy.provenance,
        source_input=_source_input(inputs),
        inferences=list(item.inferences),
    )


def missing_copy(
    products: list[ProductRecord],
    results: ResultsFile,
    context: GenerationContext,
    units: Collection[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """The ``(gtin, language)`` units this run still has no usable copy for.

    The read-side twin of :func:`pending_requests`: the same question asked of a results file
    rather than of the producer. ``pending_requests`` says what must be written; this says what is
    still missing once it has been — units with no result, with a result written for older inputs,
    or with a result that cleans to nothing.

    Both answers come from :func:`_resolve_unit`, so the doctor's verdict and what ``run_plan``
    actually publishes cannot disagree. That is the #96 lesson applied before it can bite: one
    rule answered independently on two paths drifts into meaning two things.

    ``units`` narrows it exactly as it narrows :func:`pending_requests`, and must be given the same
    set: narrowing what is generated and not what is checked reports every already-live unit as
    uncovered, which is a FAIL on every run after the first.
    """
    copy = _index(results)
    missing: list[tuple[str, str]] = []
    for product in products:
        for language in context.languages:
            if units is not None and (product.gtin, language) not in units:
                continue
            inputs, _, fingerprint = _prepare_unit(product, language, context)
            if _resolve_unit(product.gtin, language, inputs, fingerprint, copy) is None:
                missing.append((product.gtin, language))
    return missing


def _read_copy(
    product: ProductRecord, copy: _Copy, context: GenerationContext
) -> tuple[dict[str, _UnitRead], dict[str, dict[str, str]], list[SourceIssue]]:
    """Resolve one product's copy, collecting its filled values and their findings.

    Returns the per-language reads, the filled values as ``field → language → value``, and one
    ``value_translated`` finding per filled value plus one ``missing_generation_input`` per
    language whose marketing message is blank **and not about to be filled** — a value the feed
    carries in another language is not a missing input, it is a pending translation.
    """
    reads: dict[str, _UnitRead] = {}
    filled: dict[str, dict[str, str]] = {}
    issues: list[SourceIssue] = []
    for language in context.languages:
        inputs, gaps, fingerprint = _prepare_unit(product, language, context)
        unit = _resolve_unit(product.gtin, language, inputs, fingerprint, copy)
        reads[language] = _UnitRead(unit, gaps, inputs)

        for gap in gaps:
            value = unit.translations.get(gap.field) if unit is not None else None
            if value:
                filled.setdefault(gap.field, {})[language] = value
                issues.append(_translated_issue(product.gtin, language, gap, value))

        pending = {gap.field for gap in gaps}
        if not inputs.marketing_message and _MARKETING_MESSAGE_FIELD not in pending:
            issues.append(_missing_input_issue(product.gtin, language, inputs))
    return reads, filled, issues


def _apply_translations(
    product: ProductRecord, filled: dict[str, dict[str, str]], default_language: str
) -> ProductRecord:
    """Return ``product`` with each filled value merged in, the feed's own values winning.

    The read-side half of "the feed always wins". :func:`_requested_translations` is the other and
    the one every real input hits first, so no current input reaches this one — it is kept as the
    guard that still holds if the write side ever changes, not as live logic.
    """
    if not filled:
        return product
    update: dict[str, object] = {}
    localised_extras = dict(product.extras_localised)
    for field, by_language in filled.items():
        current = _stored_values(product, field, default_language)
        merged = {**by_language, **current}  # feed values overwrite the filled ones
        if field in type(product).model_fields:
            update[field] = LocalisedText(values=merged)
        else:
            localised_extras[field] = LocalisedText(values=merged)
    if localised_extras != product.extras_localised:
        update["extras_localised"] = localised_extras
    return product.model_copy(update=update) if update else product


def _stored_values(product: ProductRecord, field: str, default_language: str) -> dict[str, str]:
    """The record's own per-language values for ``field`` — the ones allowed to beat a fill.

    A flat extra counts as carrying its one value in the default language — the same reading
    :func:`_field_values` uses to decide it is missing elsewhere. Returning nothing for it instead
    dropped the authored value the moment another language was filled: translating `material` into
    French took `Materiaal: kunststof` off the *Dutch* page, because the per-language mapping that
    replaced the flat value held only French.

    **A slot holding another language's text is left out**, and only that slot. "The feed always
    wins" is the rule this function serves, and a wrong-language value is the one case where the
    feed is what we are correcting — keep it here and it overwrites the very translation
    :func:`_carried` asked for, so the fix runs, is reported as filled, and never reaches the page.
    Dropping *whole* fields here is what caused the bug in the paragraph above, so this drops one
    language and leaves every correct sibling exactly where it was.
    """
    stored = _raw_stored(product, field, default_language)
    return {lang: text for lang, text in stored.items() if not _is_foreign(lang, text)}


def _raw_stored(product: ProductRecord, field: str, default_language: str) -> dict[str, str]:
    """``field``'s values exactly as the record holds them, before any usability rule."""
    localised = product.extras_localised.get(field)
    if localised is not None:
        return dict(localised.values)
    flat = product.extras.get(field)
    if flat is not None:
        return {default_language: flat}
    value = getattr(product, field, None)
    return dict(value.values) if isinstance(value, LocalisedText) else {}


def merge_generated(
    products: list[ProductRecord],
    results: ResultsFile,
    context: GenerationContext,
) -> tuple[list[ProductRecord], list[SourceIssue]]:
    """Fold this run's generated copy and filled language gaps onto each record, reporting both.

    Pure and network-free. For each product it fills every translatable value the feed lacks in a
    configured language, overwrites ``product_name`` with the variation-combined title, sets
    ``generated_tagline`` (the first USP), and — when copy resolved for the current inputs — sets
    ``generated_description`` to the assembled three-part HTML. A stale or missing result simply
    yields no description for that language; the run_plan E18/E21 backstop handles the gap.

    Called before ``diff_against_state``, but **not** because the copy is hashed — it is not. Since
    #97 the content hash is taken over the pre-generator record, so re-generating unchanged copy
    does not reclassify a page. The order still matters for the *skip* decisions: E21 needs a
    tagline to exist, and a translated French name is what stops E18 holding the unit.

    The fill runs before the title is combined and before the description is assembled, so a
    translated variation suffixes the translated title and a translated material reaches the
    Technische-details block, rather than each being one step too late.

    Args:
        products: The parsed products.
        results: This run's copy, as the producer wrote it.
        context: The client context — languages, default language, prompt version, and what
            may be translated.

    Returns:
        The products with generated and filled fields materialised, and one :class:`SourceIssue`
        per generated value and per filled value.
    """
    copy = _index(results)
    merged: list[ProductRecord] = []
    issues: list[SourceIssue] = []
    for product in products:
        reads, filled, product_issues = _read_copy(product, copy, context)
        issues.extend(product_issues)
        record = _apply_translations(product, filled, context.default_language)
        record, copy_issues = _apply_copy(record, reads, context)
        issues.extend(copy_issues)
        merged.append(record)
    return merged, issues


def _apply_copy(
    product: ProductRecord, reads: dict[str, _UnitRead], context: GenerationContext
) -> tuple[ProductRecord, list[SourceIssue]]:
    """Combine each language's title and assemble its generated copy from the resolved units."""
    issues: list[SourceIssue] = []
    names = dict(product.product_name.values) if product.product_name else {}
    taglines: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for language in context.languages:
        unit = reads[language].unit
        base = names.get(language)
        if base is not None:
            variation = product.extra("product_variation", language)
            names[language] = _combine_title(base, variation)
        if unit is None or not unit.usps:
            continue
        taglines[language] = unit.usps[0]
        descriptions[language] = _assemble_description(
            unit.usps, product, language, context.default_language
        )
        issue = _content_issue(product.gtin, language, unit)
        if issue is not None:
            issues.append(issue)
        issues.extend(_inference_issues(product.gtin, language, unit))

    update: dict[str, object] = {}
    if product.product_name is None or names != product.product_name.values:
        update["product_name"] = LocalisedText(values=names)
    if taglines:
        update["generated_tagline"] = LocalisedText(values=taglines)
    if descriptions:
        update["generated_description"] = LocalisedText(values=descriptions)
    return (product.model_copy(update=update) if update else product), issues


def withhold_published_copy(
    products: list[ProductRecord],
    published_at: Mapping[tuple[str, str], datetime],
    generated_at: datetime | None,
) -> list[ProductRecord]:
    """Take back copy that is older than the page it would go on — it is already live, or replaced.

    The results file outlives the run that wrote it. Left there from an earlier Content run, its
    copy still matches the feed (the fingerprint says so) and would be sent again with a page
    whose only change this time is, say, its title. That rewrites live text the operator chose not
    to touch, and any edit made in WordPress since. So copy counts for a page only when it was
    written **after** that page was last published (operator, 2026-10-22: skipping Content
    updates only what changed). Writing copy again — Content's "include already-published pages"
    — makes it newer, and it counts.

    Only the two copy fields are taken back, per language. A translated name or material stays:
    that is feed data the page needs, not text the page keeps. A file with no ``generated_at``
    cannot be dated against anything, so nothing is taken back from it.

    Args:
        products: The records with this run's copy merged on.
        published_at: ``(gtin, language) -> last_run`` for every page already published.
        generated_at: When the results file was written.

    Returns:
        The records, with copy older than its page removed for that language.
    """
    if generated_at is None:
        return products
    out: list[ProductRecord] = []
    for product in products:
        stale = {
            language
            for (gtin, language), when in published_at.items()
            if gtin == product.gtin and when >= generated_at
        }
        update: dict[str, object] = {}
        for field in ("generated_tagline", "generated_description"):
            text = getattr(product, field)
            if text is None or not stale & text.values.keys():
                continue
            kept = {lang: value for lang, value in text.values.items() if lang not in stale}
            update[field] = LocalisedText(values=kept) if kept else None
        out.append(product.model_copy(update=update) if update else product)
    return out
