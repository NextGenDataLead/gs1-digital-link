"""Gather what the complete data-quality report is about: the list, the ledger, and the site.

:mod:`lib.complete_report` renders; this reads. The scope is **every product on the client's
product list that the export carries, plus every product that is live** — live meaning published
and not retracted in the ledger, *or* listed by the site, because the site is the authority and a
page made from another machine is live all the same.

Read-only throughout: the ledger through :func:`lib.state.peek_state` (never quarantines), the site
through GETs (:func:`lib.site_read.read_tool_pages`). A site that cannot be asked leaves every page
"not checked" and says why; it does not fail the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lib.complete_report import ListedRow, PageState, StatusRow, listed_rows, status_rows
from lib.config import ProcessListConfig
from lib.eligibility import eligibility
from lib.errors import ConfigError, MissingCredentialError, ProcessListError, WordPressAPIError
from lib.input_layout import archive_path
from lib.live_inventory import PublishedTotals, live_products, published_totals
from lib.process_list import read_process_list
from lib.site_read import SitePage, read_tool_pages
from lib.state import peek_state
from lib.wp_client import WordPressClient

if TYPE_CHECKING:
    from lib.config import ClientConfig
    from lib.records import ProductRecord

#: The ACF sources that are generated text — what "text" means on a live page.
_GENERATED_PREFIX = "generated_"


@dataclass(frozen=True)
class Complete:
    """Everything the complete report adds, gathered once."""

    scope: frozenset[str]
    listed: list[ListedRow]
    exported: dict[str, str]
    rows: list[StatusRow]
    site_note: str
    languages: list[str]
    published: PublishedTotals
    default_language: str


def gather(cfg: ClientConfig, products: dict[str, ProductRecord]) -> Complete:
    """Read the list, the ledger and the site, and decide every product's status.

    Raises:
        StateError: The ledger will not read.
    """
    languages = list(cfg.wordpress.languages)
    listed = _listed(cfg)
    exported = {gtin: _name(product, languages) for gtin, product in products.items()}
    pages, site_note = _site(cfg)

    state = peek_state(cfg.client_id)
    ledger = {
        product.gtin: {page.language for page in product.pages} for product in live_products(state)
    }
    live_pages: dict[str, set[str]] = {gtin: set(langs) for gtin, langs in ledger.items()}
    for (gtin, language), page in (pages or {}).items():
        if page.status == "publish":
            live_pages.setdefault(gtin, set()).add(language)

    video_field = cfg.media.video_file_field if cfg.media is not None else ""
    text_fields = [
        f for f, src in cfg.wordpress.acf_map.items() if src.startswith(_GENERATED_PREFIX)
    ]
    live = {
        gtin: {
            language: _state(pages, gtin, language, video_field, text_fields)
            for language in sorted(langs)
        }
        for gtin, langs in live_pages.items()
    }

    named = {row.gtin for row in listed if row.gtin}
    verdict = eligibility(cfg, [products[g] for g in sorted(named) if g in products])
    rows = status_rows(
        listed=listed,
        exported=exported,
        held=verdict.not_eligible,
        missing_video=verdict.missing_video,
        live=live,
        languages=languages,
    )
    scope = frozenset((named & set(exported)) | (set(live) & set(exported)))
    published = published_totals(state, languages, _qr_gtins(cfg.client_id))
    return Complete(
        scope,
        listed,
        exported,
        rows,
        site_note,
        languages,
        published,
        cfg.wordpress.default_language,
    )


def _qr_gtins(client_id: str) -> set[str]:
    """The barcodes with a QR file on this machine, from the folder ``run_execute`` renders into."""
    folder = Path("output") / client_id / "qr"
    return {path.stem for path in folder.glob("*") if path.stem.isdigit()}


def _state(
    pages: dict[tuple[str, str], SitePage] | None,
    gtin: str,
    language: str,
    video_field: str,
    text_fields: list[str],
) -> PageState:
    if pages is None:
        return PageState(on_site=None, video=None, text=None)
    page = pages.get((gtin, language))
    if page is None or page.status != "publish":
        return PageState(on_site=False, video=None, text=None)
    return PageState(
        on_site=True,
        video=bool(video_field) and video_field in page.filled,
        text=all(field in page.filled for field in text_fields),
    )


def _site(cfg: ClientConfig) -> tuple[dict[tuple[str, str], SitePage] | None, str]:
    fields = [*cfg.wordpress.acf_map]
    if cfg.media is not None:
        fields.append(cfg.media.video_file_field)
    try:
        with WordPressClient(cfg.wordpress) as client:
            return read_tool_pages(client, cfg.wordpress, fields), "Checked against the site."
    except (ConfigError, MissingCredentialError, WordPressAPIError) as exc:
        return None, f"The site could not be asked ({exc}), so no live page is confirmed."


def _listed(cfg: ClientConfig) -> list[ListedRow]:
    """The product list as the client sent it — the upload, not the batch saved from it."""
    if cfg.process_list is None:
        return []
    control = Path(cfg.process_list.path)
    upload = archive_path(control)
    path = upload if upload.is_file() else control
    try:
        sheet = read_process_list(
            ProcessListConfig(path=str(path), gtin_column=cfg.process_list.gtin_column)
        )
    except ProcessListError:
        return []
    return listed_rows(sheet)


def _name(product: ProductRecord, languages: list[str]) -> str:
    for language in languages:
        if value := (product.product_name.values.get(language) or "").strip():
            return value
    return ""
