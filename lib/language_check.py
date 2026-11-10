"""Do a product's languages say the same thing? The cross-language check behind report §4e.

Every GS1 attribute a client maps with ``localised: true`` has one value per language, and the
client fills each language in separately. Nothing forced them to agree, and on the pilot export
they often do not: a French 1083 describing a hair brush where the Dutch one describes a pet brush,
an English word in a French slot, a French functional name that is a weed *killer* where the Dutch
one is a weed brush. Those values reach the site twice over — some verbatim (the title), the rest
through the copy generator — so a disagreement in the source is a disagreement on the page.

Two kinds of finding, decided in two different ways:

* **Missing** — one language carries a value and another does not. Decided here, from the
  record, at report time: no judgment is involved, so nothing is stored and it is never stale.
* **Differs** / **wrong language** — both carry a value and they do not say the same thing, or a
  value is not in the language of its slot. That is a judgment, made by a producer
  (:class:`LanguageChecker`, the headless API backend in :mod:`lib.llm`) and kept in
  ``language_check.json``.

**Kept, and reused while the values are unchanged.** That is a deliberate difference from the copy
generator, which keeps nothing between runs: copy is published, so stale copy is a wrong page,
whereas a check result is a report finding, and each one carries the fingerprint of exactly the
values it judged. Any edit to any compared value changes the fingerprint, and a result whose
fingerprint no longer matches is not shown — the product counts as not yet compared instead.

The verdict is a model's reading of two texts, so the report presents it as something to look at,
never as a fact about the product.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lib.config import ClientConfig, ExportConfig
from lib.errors import GeneratorError, LLMAPIError
from lib.gdsn import source_label
from lib.records import LocalisedText, ProductRecord

_log = logging.getLogger(__name__)

#: The file the producer writes and the report reads, beside ``products.json``.
RESULTS_FILENAME: Final = "language_check.json"

#: Part of every fingerprint: changing what the check asks invalidates every kept result.
CHECK_VERSION: Final = "v1"

#: The verdicts. ``missing`` is decided here; the other two come from a producer.
VERDICT_MISSING: Final = "missing"
VERDICT_DIFFERS: Final = "differs"
VERDICT_WRONG_LANGUAGE: Final = "wrong_language"
PRODUCER_VERDICTS: Final = frozenset({VERDICT_DIFFERS, VERDICT_WRONG_LANGUAGE})


class ComparedField(BaseModel):
    """One localised source: the field name and the words MyGS1 uses for it."""

    model_config = ConfigDict(frozen=True)

    field: str
    label: str


class FieldValues(BaseModel):
    """One field of one product as each language carries it (blank languages left out)."""

    model_config = ConfigDict(frozen=True)

    field: str
    label: str
    values: dict[str, str]


class CheckRequest(BaseModel):
    """One product to compare: every field carried in two or more languages, and its fingerprint."""

    model_config = ConfigDict(frozen=True)

    gtin: str
    default_language: str
    fields: list[FieldValues]
    fingerprint: str


class Finding(BaseModel):
    """One disagreement: ``field`` in ``language`` is missing, differs, or is the wrong language.

    ``language`` is the slot the finding is about — the one that is blank, that says something
    else than the default language, or that holds another language's text. ``note`` says what
    differs, in a sentence a client can act on; empty for ``missing``, which says it all.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    language: str
    verdict: str
    note: str = ""


class CheckResult(BaseModel):
    """A producer's answer for one :class:`CheckRequest`: only the disagreements it found."""

    model_config = ConfigDict(frozen=True)

    findings: list[Finding] = Field(default_factory=list)


class CheckedProduct(BaseModel):
    """One product as last judged: the fingerprint of the values judged, and what was found."""

    model_config = ConfigDict(frozen=True)

    gtin: str
    fingerprint: str
    findings: list[Finding] = Field(default_factory=list)


class CheckFile(BaseModel):
    """``language_check.json``: every product checked, written by one producer run."""

    client_id: str
    provenance: str
    checked_at: datetime | None = None
    results: list[CheckedProduct] = Field(default_factory=list)


class LanguageChecker(Protocol):
    """A producer: judges one :class:`CheckRequest`. :class:`lib.llm.AnthropicClient` is one."""

    def compare_languages(self, request: CheckRequest) -> CheckResult:
        """Return the disagreements between the request's languages."""
        ...


# --- Reading the record --------------------------------------------------------


def compared_fields(export: ExportConfig) -> list[ComparedField]:
    """Every source with a value per language, mapped fields first, in config order.

    All of them, not only the ones that reach a page: a value the site does not print today is
    still the client's data, and the next mapping change may print it.
    """
    return [
        ComparedField(field=name, label=source_label(src))
        for name, src in export.all_sources.items()
        if src.localised
    ]


def field_values(product: ProductRecord, field: str) -> dict[str, str]:
    """The non-blank values ``product`` carries for ``field``, per language, as the feed has them.

    Raw on purpose: :mod:`lib.generator` drops a slot holding another language's text, which is
    right for writing copy and wrong here — that slot is one of the things being reported.
    """
    node = getattr(product, field, None) if field in ProductRecord.model_fields else None
    if node is None:
        node = product.extras_localised.get(field)
    if not isinstance(node, LocalisedText):
        return {}
    return {lang: value.strip() for lang, value in node.values.items() if value and value.strip()}


def missing_findings(
    product: ProductRecord, fields: Iterable[ComparedField], languages: list[str]
) -> list[Finding]:
    """Each configured language that lacks a field another configured language carries."""
    found: list[Finding] = []
    for spec in fields:
        carried = field_values(product, spec.field)
        if not any(lang in carried for lang in languages):
            continue
        found.extend(
            Finding(field=spec.field, language=lang, verdict=VERDICT_MISSING)
            for lang in languages
            if lang not in carried
        )
    return found


def check_request(
    product: ProductRecord,
    fields: Iterable[ComparedField],
    languages: list[str],
    default_language: str,
) -> CheckRequest | None:
    """The request comparing ``product``'s languages, or ``None`` when no field has two.

    A field is sent when it carries at least two configured languages. A field with one value has
    nothing to be compared against — it is a :data:`VERDICT_MISSING` finding instead.
    """
    sent = []
    for spec in fields:
        carried = field_values(product, spec.field)
        values = {lang: carried[lang] for lang in languages if lang in carried}
        if len(values) >= 2:  # noqa: PLR2004 — a comparison needs two sides
            sent.append(FieldValues(field=spec.field, label=spec.label, values=values))
    if not sent:
        return None
    return CheckRequest(
        gtin=product.gtin14,
        default_language=default_language,
        fields=sent,
        fingerprint=_fingerprint(sent, default_language),
    )


def _fingerprint(fields: list[FieldValues], default_language: str) -> str:
    """SHA-256 over the check version, the default language and every compared value."""
    blob = json.dumps(
        {
            "version": CHECK_VERSION,
            "default_language": default_language,
            "fields": [[f.field, sorted(f.values.items())] for f in fields],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- Producing ---------------------------------------------------------------------


class Pending(NamedTuple):
    """What a check run has to ask for, and what it may keep."""

    requests: list[CheckRequest]
    kept: list[CheckedProduct]


def pending_checks(requests: Iterable[CheckRequest], existing: CheckFile | None) -> Pending:
    """Split ``requests`` into those still to judge and the kept results that still match."""
    by_gtin = {item.gtin: item for item in (existing.results if existing else [])}
    todo: list[CheckRequest] = []
    kept: list[CheckedProduct] = []
    for request in requests:
        previous = by_gtin.get(request.gtin)
        if previous is not None and previous.fingerprint == request.fingerprint:
            kept.append(previous)
        else:
            todo.append(request)
    return Pending(todo, kept)


def accepted(request: CheckRequest, result: CheckResult) -> CheckedProduct:
    """Keep only findings about a field and language the request actually sent.

    A producer naming a field it was never shown, or a verdict this module does not know, would
    otherwise put a row in the client's report about nothing. So would calling a name that is
    the same in every language the wrong language. Dropped findings are logged.
    """
    sent = {f.field: f.values for f in request.fields}
    kept = []
    for finding in result.findings:
        values = sent.get(finding.field, {})
        if (
            finding.verdict in PRODUCER_VERDICTS
            and finding.language in values
            and not _same_everywhere_named_wrong(finding, values)
        ):
            kept.append(finding)
        else:
            _log.warning("dropped a language-check finding for %s: %r", request.gtin, finding)
    return CheckedProduct(gtin=request.gtin, fingerprint=request.fingerprint, findings=kept)


class Failure(NamedTuple):
    """A product whose call failed, and why. It is asked again on the next run."""

    gtin: str
    reason: str


class Judged(NamedTuple):
    """The products a producer answered for, and the ones whose call failed."""

    results: list[CheckedProduct]
    failed: list[Failure]


#: A value of at most this many words, the same in every language, is a name.
_NAME_WORDS: Final = 4
#: Statuses that fail every product alike — a rejected key, no access, no such model.
_FATAL_STATUSES: Final = frozenset({401, 403, 404})
#: After this many failed calls in a row, the cause is not the product: stop paying for it.
_MAX_CONSECUTIVE_FAILURES: Final = 3


def _normalised(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _same_everywhere_named_wrong(finding: Finding, values: Mapping[str, str]) -> bool:
    """A short value spelled the same in every language is a brand or model name, not a mistake.

    Enforced here rather than left to the prompt, which says the same and was ignored live:
    "Desk lamp" and "Magnetic cable" — the product's name in both languages — came back as
    English in the French slot. Only for name-length values: a sentence pasted unchanged into
    every slot is the client's mistake, and that ``wrong_language`` finding stands.
    """
    if finding.verdict != VERDICT_WRONG_LANGUAGE:
        return False
    forms = {_normalised(value) for value in values.values()}
    return len(forms) == 1 and len(next(iter(forms)).split()) <= _NAME_WORDS


def judge_each(
    requests: Iterable[CheckRequest], checker: LanguageChecker
) -> Iterator[CheckedProduct | Failure]:
    """Ask ``checker`` about each request, in order, yielding each outcome as it arrives.

    A failed call is yielded as a :class:`Failure` and the run goes on, so one bad answer does not
    cost the others. A failure that is the same for every product — a rejected key, a missing
    model, or :data:`_MAX_CONSECUTIVE_FAILURES` in a row — raises instead: going on would only
    pay for the same refusal again.
    """
    in_a_row = 0
    for request in requests:
        try:
            result = checker.compare_languages(request)
        except LLMAPIError as exc:
            in_a_row += 1
            if exc.status_code in _FATAL_STATUSES or in_a_row >= _MAX_CONSECUTIVE_FAILURES:
                raise
            _log.warning("language check failed for %s: %s", request.gtin, exc)
            yield Failure(request.gtin, str(exc))
            continue
        in_a_row = 0
        yield accepted(request, result)


def run_checks(requests: Iterable[CheckRequest], checker: LanguageChecker) -> Judged:
    """Every outcome of :func:`judge_each`, split into answers and failures."""
    outcomes = list(judge_each(requests, checker))
    return Judged(
        [o for o in outcomes if isinstance(o, CheckedProduct)],
        [o for o in outcomes if isinstance(o, Failure)],
    )


def requests_for(cfg: ClientConfig, products: Iterable[ProductRecord]) -> list[CheckRequest]:
    """A request for every product with at least one field in two languages."""
    fields = compared_fields(cfg.export)
    languages, default = cfg.wordpress.languages, cfg.wordpress.default_language
    return [
        request
        for product in products
        if (request := check_request(product, fields, languages, default)) is not None
    ]


class CheckRun(NamedTuple):
    """What one check run did: judged now, kept from before, and the calls that failed."""

    asked: int
    kept: int
    failed: list[Failure]


def check_and_save(  # noqa: PLR0913 — each argument is one fact the run needs
    cfg: ClientConfig,
    products: Iterable[ProductRecord],
    path: Path,
    checker: LanguageChecker,
    provenance: str,
) -> CheckRun:
    """Judge every product whose values are new or changed, keep the rest, write the file.

    Nothing already in the file is dropped: a result for a product outside ``products`` stays,
    because the complete report reaches past the current product list, and its fingerprint
    already stops it showing once its values change. The file is written whenever something new
    was judged, also when the run stops partway — on a fatal API error or anything else — so a
    judgment once paid for is never paid for twice.
    """
    existing = load_checks(path)
    pending = pending_checks(requests_for(cfg, products), existing)
    asked = {request.gtin for request in pending.requests}
    carried = [item for item in (existing.results if existing else []) if item.gtin not in asked]
    results: list[CheckedProduct] = []
    failed: list[Failure] = []
    try:
        for outcome in judge_each(pending.requests, checker):
            if isinstance(outcome, Failure):
                failed.append(outcome)
            else:
                results.append(outcome)
    finally:
        if results:
            save_checks(
                path,
                CheckFile(
                    client_id=cfg.client_id,
                    provenance=provenance,
                    checked_at=datetime.now(UTC),
                    results=sorted([*carried, *results], key=lambda item: item.gtin),
                ),
            )
    return CheckRun(asked=len(results), kept=len(pending.kept), failed=failed)


# --- File IO -------------------------------------------------------------------


def results_path(data_dir: Path) -> Path:
    """Where ``language_check.json`` lives for a client's ``output/{client}/data`` folder."""
    return data_dir / RESULTS_FILENAME


def load_checks(path: Path) -> CheckFile | None:
    """Read a check file; ``None`` when absent.

    Raises:
        GeneratorError: The file exists but is not a check file — said loudly, because reading
            it as empty would silently re-judge, and re-bill, every product.
    """
    if not path.exists():
        return None
    try:
        return CheckFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise GeneratorError(f"{path} is not a readable language-check file: {exc}") from exc


def save_checks(path: Path, checks: CheckFile) -> None:
    """Write the check file atomically (temp file, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(checks.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- What the report shows -----------------------------------------------------------


def report_for(
    cfg: ClientConfig, products: Collection[ProductRecord], checks: CheckFile | None
) -> LanguageReport:
    """:func:`language_report` with the client's fields and languages filled in."""
    return language_report(
        products,
        compared_fields(cfg.export),
        cfg.wordpress.languages,
        cfg.wordpress.default_language,
        checks,
    )


class ReportRow(NamedTuple):
    """One (product, field) with at least one finding, ready to lay out."""

    gtin: str
    label: str
    values: dict[str, str]
    findings: list[Finding]


class LanguageReport(NamedTuple):
    """§4e's content: the rows, and how many products a judgment could be shown for."""

    rows: list[ReportRow]
    products: int
    judged: int


def language_report(  # noqa: PLR0913 — each argument is one fact the section is built from
    products: Collection[ProductRecord],
    fields: list[ComparedField],
    languages: list[str],
    default_language: str,
    checks: CheckFile | None,
) -> LanguageReport:
    """Join the missing-value findings to the kept judgments that still match the export.

    A product counts as *judged* when it had nothing to compare, or when a kept result's
    fingerprint matches its values today. A product whose values changed since it was judged
    shows its missing values only, and is counted as not judged, so the section never shows a
    verdict about text the export no longer holds.
    """
    kept = {item.gtin: item for item in (checks.results if checks else [])}
    labels = {spec.field: spec.label for spec in fields}
    rows: list[ReportRow] = []
    judged = 0
    for product in sorted(products, key=lambda p: p.gtin14):
        found = missing_findings(product, fields, languages)
        request = check_request(product, fields, languages, default_language)
        previous = kept.get(product.gtin14)
        if request is None:
            judged += 1
        elif previous is not None and previous.fingerprint == request.fingerprint:
            judged += 1
            found.extend(previous.findings)
        rows.extend(_rows(product, found, labels))
    return LanguageReport(rows, len(products), judged)


def _rows(
    product: ProductRecord, found: list[Finding], labels: Mapping[str, str]
) -> list[ReportRow]:
    """Group a product's findings by field, in the order the fields are configured."""
    by_field: dict[str, list[Finding]] = {}
    for finding in found:
        by_field.setdefault(finding.field, []).append(finding)
    return [
        ReportRow(product.gtin14, labels[field], field_values(product, field), by_field[field])
        for field in labels
        if field in by_field
    ]
