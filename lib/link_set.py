"""The GS1 resolver link set for one GTIN, and the hash that says whether it moved (§4.3, §5.4).

Built in one place because two callers ask for it at different times. ``run_execute`` builds it
to write the record, and records its hash in ``state.json``. ``run_plan`` rebuilds it from what
the ledger says is published, to ask whether the record still says that.

That second question had no answer until 2026-10-22, and the gap was silent. A ``pages`` run
records the page's new title and its new content hash, and keeps the old link-set hash, because
it wrote no link. The next ``links`` plan compared the content hash, found it current, and called
every row UNCHANGED: renaming 38 products left 38 GS1 records carrying the old names, and a
links run reported nothing to do. Comparing the link set itself is what closes that.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Protocol

from lib.config import ClientConfig, GS1LinkConfig
from lib.gs1_dl_client import LinkInput

if TYPE_CHECKING:
    from lib.records import ProductRecord, StateEntry

DEFAULT_LINK_TYPE: Final = "gs1:pip"


class LinkTarget(Protocol):
    """What a link needs to know about one language's page: where it is and what it is called."""

    @property
    def url(self) -> str: ...

    @property
    def title(self) -> str: ...


def link_title(
    link: GS1LinkConfig,
    cfg: ClientConfig,
    product: ProductRecord,
    language: str,
    fallback: str,
) -> str:
    """Resolve a resolver link's title from its ``title_pattern`` (§2.4).

    Takes a product and a language rather than a plan row: a link is built for every language
    of the GTIN, including ones whose row was not confirmed this run and so has no row at all.
    """
    if not link.title_pattern:
        return fallback
    name = product.product_name.get(language, cfg.wordpress.default_language) or fallback
    return link.title_pattern.format(
        product_name=name, title=fallback, gtin=product.gtin, brand=product.brand
    )


def build_links(
    cfg: ClientConfig, product: ProductRecord, pages: Mapping[str, LinkTarget]
) -> list[LinkInput]:
    """Build the resolver link set for one GTIN, spanning every known language (§4.3).

    This is the whole record's link set, not one language's: GS1's CreateOrUpdate replaces the
    ``links`` array wholesale, so whatever is omitted here is deleted from the record.

    Languages are emitted in sorted order so :func:`link_set_hash` is stable across runs
    regardless of plan order.
    """
    configs = cfg.gs1_links or [GS1LinkConfig(link_type=DEFAULT_LINK_TYPE, default=True)]
    return [
        LinkInput(
            link_type=link.link_type,
            language=language,
            link_title=link_title(link, cfg, product, language, pages[language].title),
            target_url=pages[language].url,
            # "standaardlink voor nl, niet voor fr": only the default language's link is the
            # default one, however many languages the record carries.
            default_link_type=link.default and language == cfg.wordpress.default_language,
            public=link.public,
            media_type=cfg.gs1.default_media_type,
        )
        for language in sorted(pages)
        for link in configs
    ]


def link_set_hash(links: list[LinkInput]) -> str:
    """Return a stable SHA-256 of the resolver link set for change detection (§5.4)."""
    canonical = json.dumps(links, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Recorded:
    """A page as the ledger records it, shaped as a :class:`LinkTarget`."""

    def __init__(self, entry: StateEntry) -> None:
        self.url = entry.wp_url
        self.title = entry.title or ""


def link_is_stale(
    cfg: ClientConfig, product: ProductRecord, entries: dict[str, StateEntry]
) -> bool:
    """Whether this GTIN's GS1 record no longer says what its published pages and data say.

    The link set is rebuilt from the ledger's pages (their URL and title) and today's product
    record, and hashed exactly as ``run_execute`` hashed what it wrote. A different hash means the
    record carries something else: an old name, most often, after a ``pages`` run.

    ``False`` when there is no link to be stale. A GTIN with no recorded link (an empty hash, or
    no entries) is already CHANGED for that reason (``lib.state._has_no_resolver_link``), and
    saying so twice would only blur which of the two it is.
    """
    if not entries or not all(entry.gs1_link_set_hash for entry in entries.values()):
        return False
    recorded = {entry.gs1_link_set_hash for entry in entries.values()}
    pages = {language: _Recorded(entry) for language, entry in entries.items()}
    return link_set_hash(build_links(cfg, product, pages)) not in recorded
