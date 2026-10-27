"""Every page this tool made, as the site holds it — the one read both site checks share.

The Content screen's check ("which products have no tagline yet?") and the complete data-quality
report ("is each live page there, with its text and its video?") ask the same thing of the site.
Two readers would sooner or later disagree about what "there" means, so there is one.

**Listed per language, read by id without one.** An unscoped listing on this WPML site answers with
the default language only, so it is asked per language; a language-scoped *field* read answers
``acf: []`` for a page that has them, so each page is then read by id with no language parameter
(:meth:`lib.wp_client.WordPressClient.read_page`). Read-only: GETs and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lib.media_video import canon_gtin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lib.config import WordPressConfig
    from lib.wp_client import WordPressClient


@dataclass(frozen=True)
class SitePage:
    """One tool-made page: its id, status, and which of the asked-for ACF fields carry a value."""

    page_id: int
    status: str
    filled: frozenset[str]


def read_tool_pages(
    client: WordPressClient, wordpress: WordPressConfig, fields: Iterable[str]
) -> dict[tuple[str, str], SitePage]:
    """``(gtin14, language) -> SitePage`` for every page carrying a ``meta.gtin``.

    Args:
        client: An open client.
        wordpress: The client's WordPress config — post type and languages.
        fields: The ACF fields to report as filled or not.
    """
    wanted = tuple(fields)
    found: dict[tuple[str, str], SitePage] = {}
    for language in wordpress.languages:
        for listed in client.list_pages_with_gtin(wordpress.post_type, language):
            meta = listed.get("meta")
            gtin = str(meta.get("gtin", "")) if isinstance(meta, dict) else ""
            if not gtin:
                continue
            page_id = int(listed.get("id", 0))
            page = client.read_page(wordpress.post_type, page_id) or {}
            acf = page.get("acf")
            values = acf if isinstance(acf, dict) else {}
            found[canon_gtin(gtin), language] = SitePage(
                page_id=page_id,
                status=str(listed.get("status", "publish")),
                filled=frozenset(f for f in wanted if str(values.get(f) or "").strip()),
            )
    return found
