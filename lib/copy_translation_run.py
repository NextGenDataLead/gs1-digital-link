"""The translation step of a ``translate_copy`` run: find each source, translate, keep going.

Runs after the default language's copy is written (:func:`scripts.run_generate`), because that
copy is what most translations translate. A product whose default-language page this run does not
rewrite is translated from that page as it is live (:mod:`lib.live_default_copy`) — read here,
with GETs.

One unit's failure does not cost the others: a call the API refuses, or an answer with a different
number of lines than its source (a merged or split line no longer lines up with its original), is
reported and its page left as it is; it is still out of date, so the next run asks again. A
refusal that is the same for every unit — a rejected key, a missing model — still stops the run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from typing import Final, NamedTuple

from lib.config import ClientConfig
from lib.copy_translation import default_copy, translation_requests
from lib.errors import (
    GeneratorError,
    LLMAPIError,
    MissingCredentialError,
    StateError,
    WordPressAPIError,
)
from lib.generator import (
    GenerationContext,
    LLMClient,
    ResultItem,
    ResultsFile,
    _labels,
    is_translated,
    result_item,
)
from lib.live_default_copy import read_live_copy
from lib.records import ProductRecord
from lib.state import peek_state
from lib.wp_client import WordPressClient

_log = logging.getLogger(__name__)

#: Statuses that fail every call alike; going on would only pay for the same refusal again.
_FATAL_STATUSES: Final = frozenset({401, 403, 404})

#: ``gtin -> page id`` in, ``gtin -> usps`` out: how the live default-language copy is read.
LiveReader = Callable[[Mapping[str, int]], dict[str, list[str]]]


class TranslationRun(NamedTuple):
    """What the translation step wrote, and every unit it did not, with why."""

    items: list[ResultItem]
    #: ``(gtin, language, reason)`` for each unit left without copy.
    not_written: list[tuple[str, str, str]]


def site_reader(cfg: ClientConfig) -> LiveReader:
    """Read the live default-language copy from the client's site (GET only)."""

    def read(pages: Mapping[str, int]) -> dict[str, list[str]]:
        with WordPressClient(cfg.wordpress) as client:
            return read_live_copy(
                client, cfg.wordpress, pages, _labels(cfg.wordpress.default_language)
            )

    return read


def _live_pages(client_id: str, gtins: Collection[str], language: str) -> dict[str, int]:
    """``gtin -> page id`` of each live default-language page the ledger records among ``gtins``."""
    entries = peek_state(client_id).entries
    pages: dict[str, int] = {}
    for gtin in gtins:
        entry = entries.get(gtin, {}).get(language)
        if entry is not None and entry.wp_status == "publish" and not entry.retracted:
            pages[gtin] = entry.wp_page_id
    return pages


def _sources(  # noqa: PLR0913 — the run's scope, what it wrote, and where else to look
    cfg: ClientConfig,
    context: GenerationContext,
    products: list[ProductRecord],
    units: Collection[tuple[str, str]] | None,
    results: ResultsFile,
    read_live: LiveReader,
) -> tuple[dict[str, list[str]], str]:
    """``gtin -> the default language's copy`` to translate, and why the live read failed if it did.

    This run's copy where this run writes the default language; the live page where it does not.
    Never this run's copy for a page this run leaves alone: that would translate text the Dutch
    page does not carry.
    """
    default = context.default_language
    wanted = {
        product.gtin
        for product in products
        for language in context.languages
        if is_translated(context, language) and (units is None or (product.gtin, language) in units)
    }
    in_run = {gtin for gtin in wanted if units is None or (gtin, default) in units}
    sources: dict[str, list[str]] = {}
    for product in products:
        if product.gtin in in_run and (usps := default_copy(product, context, results)):
            sources[product.gtin] = usps
    live = wanted - in_run
    if not live:
        return sources, ""
    try:
        sources.update(read_live(_live_pages(cfg.client_id, live, default)))
    except (MissingCredentialError, WordPressAPIError, StateError) as exc:
        _log.warning("the live %s copy could not be read: %s", default, exc)
        return sources, f"the live {default} page could not be read ({exc})"
    return sources, ""


def translate_units(  # noqa: PLR0913 — the run's scope, what it wrote, and who translates
    cfg: ClientConfig,
    context: GenerationContext,
    products: list[ProductRecord],
    units: Collection[tuple[str, str]] | None,
    results: ResultsFile,
    client: LLMClient,
    read_live: LiveReader | None = None,
) -> TranslationRun:
    """Translate the default language's copy into every other language this run writes.

    Args:
        cfg: The client config.
        context: The generation context, ``translate_copy`` on.
        products: The products in scope.
        units: The ``(gtin, language)`` units this run writes, or ``None`` for all.
        results: The default-language copy this run has written.
        client: The producer.
        read_live: How to read the live default-language copy; the site, by default.

    Raises:
        LLMAPIError: A refusal that fails every call alike (401, 403, 404).
    """
    sources, live_failure = _sources(
        cfg, context, products, units, results, read_live or site_reader(cfg)
    )
    requests, unsourced = translation_requests(products, context, sources, units)
    reason = live_failure or f"no {context.default_language} copy to translate"
    not_written = [(gtin, language, reason) for gtin, language in unsourced]
    items: list[ResultItem] = []
    for request in requests:
        try:
            result = client.generate_copy(request)
        except LLMAPIError as exc:
            if exc.status_code in _FATAL_STATUSES:
                raise
            not_written.append((request.gtin, request.language, str(exc)))
            continue
        try:
            # Cleaned first, then counted: a blank line the cleaning drops would otherwise pass
            # the count and shift every bullet after it against its original.
            item = result_item(request, result)
        except GeneratorError as exc:
            not_written.append((request.gtin, request.language, str(exc)))
            continue
        if len(item.usps) != len(request.source_usps):
            not_written.append(
                (
                    request.gtin,
                    request.language,
                    f"the translation has {len(item.usps)} lines for "
                    f"{len(request.source_usps)} in the original",
                )
            )
            continue
        items.append(item)
    return TranslationRun(items, not_written)
