"""Every product this tool has put live, from the ledger.

Live means published and not retracted (``wp_status == "publish"``, ``retracted`` false). The ledger
records what *this machine* wrote, so it is the claim and the site is the check: the complete
data-quality report (:mod:`lib.complete_scope`) joins this with the site listing. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from lib.records import State


@dataclass(frozen=True)
class LivePage:
    """One published page, as the ledger records it."""

    language: str
    url: str
    published: str
    video_file: str | None
    gs1_link: bool


@dataclass(frozen=True)
class LiveProduct:
    """A product with at least one live page."""

    gtin: str
    title: str
    pages: tuple[LivePage, ...]


def live_products(state: State) -> list[LiveProduct]:
    """Every product with a published, unretracted page, ordered by barcode."""
    found = []
    for gtin, by_language in sorted(state.entries.items()):
        pages = tuple(
            LivePage(
                language=language,
                url=entry.wp_url,
                published=entry.last_run.date().isoformat(),
                video_file=entry.video_file,
                gs1_link=bool(entry.gs1_link_set_hash),
            )
            for language, entry in sorted(by_language.items())
            if entry.wp_status == "publish" and not entry.retracted
        )
        if pages:
            title = next((e.title for e in by_language.values() if e.title), "") or ""
            found.append(LiveProduct(gtin=gtin, title=title, pages=pages))
    return found


@dataclass(frozen=True)
class PublishedTotals:
    """What this tool has published, counted from the ledger and the QR files on this machine.

    A GS1 record is per product and carries one link per language whose page it knew when it was
    written, so a live page with a link-set hash *is* a link in that product's record. The ledger
    is what this machine wrote — the records themselves are not re-read here.
    """

    products: int
    pages: dict[str, int] = field(default_factory=dict)
    records: int = 0
    links: dict[str, int] = field(default_factory=dict)
    without_record: int = 0
    qr_codes: int = 0
    records_without_qr: int = 0
    taken_down: int = 0


def published_totals(
    state: State, languages: Collection[str], qr_gtins: Collection[str]
) -> PublishedTotals:
    """Count live products, pages and links per language, GS1 records, and QR codes.

    Args:
        state: The ledger.
        languages: The configured languages; each gets a count, zero included.
        qr_gtins: The barcodes (14 digits) with a QR file on this machine.
    """
    live = live_products(state)
    linked = {p.gtin for p in live if any(page.gs1_link for page in p.pages)}
    qr = set(qr_gtins)
    return PublishedTotals(
        products=len(live),
        pages={
            lang: sum(page.language == lang for p in live for page in p.pages) for lang in languages
        },
        records=len(linked),
        links={
            lang: sum(page.language == lang and page.gs1_link for p in live for page in p.pages)
            for lang in languages
        },
        without_record=len(live) - len(linked),
        qr_codes=len(linked & qr),
        records_without_qr=len(linked - qr),
        taken_down=len(state.entries) - len(live),
    )
