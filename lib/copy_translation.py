"""``generator.translate_copy``: one language's copy, and every other language its translation.

Without it each language's copy is written from that language's own GS1 text, and where the client
filled the languages in differently the pages of one product say different things. With it the
default language is written as before and every other language's copy is that copy translated.

Three questions follow, and this module answers them:

* **What is the copy written from?** :func:`copy_basis` — a fingerprint of the default language's
  GS1 text and nothing else. Another language's GS1 text has no say in its copy any more, so
  editing it rewrites nothing; the report's §4e is where it still counts.
* **When is a live page's copy out of date?** :func:`due_languages`, from the basis recorded on each
  state entry. When the default language's text has changed since a page's copy was written,
  every language is written again. A translated page with no basis recorded was written before
  this setting — its own-language copy is out of date, the default language's is not.
* **What does a translation translate?** The default language's copy as this run writes it, or —
  when this run does not write it — as the live page carries it (:mod:`lib.live_default_copy`),
  so the two pages say the same thing.

Pure and network-free.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

from lib.generator import (
    MODE_TRANSLATE,
    GenerationContext,
    GenerationRequest,
    ResultsFile,
    _fingerprint,
    _gather_inputs,
    _index,
    _prepare_unit,
    _resolve_unit,
    is_translated,
)
from lib.records import ProductRecord, State, StateEntry

#: In place of the prompt version in :func:`copy_basis`: a new prompt does not make live copy out
#: of date, any more than it does without translate_copy.
_BASIS_SALT: Final = "copy-basis"
_PUBLISHED: Final = "publish"


def copy_basis(product: ProductRecord, context: GenerationContext) -> str:
    """The fingerprint of what the default language's copy is written from.

    The default language's generation inputs — functional name, 1083, 1067, net content,
    dimensions, material — and nothing another language carries: not even the values the default
    language borrows from one (its translation gaps), which would let another language's edit
    rewrite every page.
    """
    inputs = _gather_inputs(product, context.default_language, context, gaps=[])
    return _fingerprint(inputs, context.default_language, _BASIS_SALT)


def _is_live(entry: StateEntry) -> bool:
    return entry.wp_page_id is not None and entry.wp_status == _PUBLISHED and not entry.retracted


def due_languages(
    entries: Mapping[str, StateEntry], basis: str, default_language: str
) -> frozenset[str]:
    """The live languages of one product whose copy is out of date.

    * The default-language page written from other text than ``basis`` → every live language:
      its copy is written again, and every translation of it.
    * Otherwise each translated page not written from ``basis`` → that page only, translated from
      the default-language page as it is live. That is the ``translate_copy`` switch (no basis
      yet: written from its own GS1 text), and a translation that failed while the default
      language's did not — which must not send the default-language page round again.

    A default-language page with no basis is left alone: its text predates the setting, and
    stays (operator, 2026-11-10). Its first translation records that basis on it
    (``run_execute``), so a later change to its GS1 text is seen.
    """
    live = {language: entry for language, entry in entries.items() if _is_live(entry)}
    default = live.get(default_language)
    if default is not None and default.copy_basis not in (None, basis):
        return frozenset(live)
    return frozenset(
        language
        for language, entry in live.items()
        if language != default_language and entry.copy_basis != basis
    )


def due_units(
    products: list[ProductRecord], state: State, context: GenerationContext
) -> set[tuple[str, str]]:
    """Every ``(gtin, language)`` among ``products`` whose live copy is out of date."""
    if not context.translate_copy:
        return set()
    return {
        (product.gtin, language)
        for product in products
        for language in due_languages(
            state.entries.get(product.gtin, {}),
            copy_basis(product, context),
            context.default_language,
        )
    }


def default_copy(
    product: ProductRecord, context: GenerationContext, results: ResultsFile
) -> list[str] | None:
    """The default language's copy as this run has it: the feed's short 1067, or ``results``."""
    language = context.default_language
    inputs, _, fingerprint = _prepare_unit(product, language, context)
    unit = _resolve_unit(product.gtin, language, inputs, fingerprint, _index(results))
    return list(unit.usps) if unit is not None else None


def translation_requests(
    products: list[ProductRecord],
    context: GenerationContext,
    sources: Mapping[str, list[str]],
    units: Collection[tuple[str, str]] | None = None,
) -> tuple[list[GenerationRequest], list[tuple[str, str]]]:
    """One :data:`MODE_TRANSLATE` request per translated unit with a source, and those without.

    ``sources`` is ``gtin -> the default language's copy`` to translate. A unit whose product has
    none is returned in the second list rather than written from anything else: a translation of
    some other text would make the two pages disagree, which is what this setting exists to stop.
    """
    requests: list[GenerationRequest] = []
    unsourced: list[tuple[str, str]] = []
    for product in products:
        for language in context.languages:
            unit = (product.gtin, language)
            if not is_translated(context, language) or (units is not None and unit not in units):
                continue
            source = sources.get(product.gtin)
            if not source:
                unsourced.append(unit)
                continue
            inputs, gaps, fingerprint = _prepare_unit(product, language, context)
            requests.append(
                GenerationRequest(
                    gtin=product.gtin,
                    language=language,
                    inputs=inputs,
                    input_fingerprint=fingerprint,
                    translations=gaps,
                    mode=MODE_TRANSLATE,
                    source_usps=list(source),
                    source_language=context.default_language,
                )
            )
    return requests, unsourced
