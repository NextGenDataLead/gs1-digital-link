"""The default language's copy as the live site carries it — the source of a translation.

``generator.translate_copy`` makes every other language's copy a translation of the default
language's. When a run writes the default language too, the translation is made from what it
writes. When it does not — the Dutch page is live and its text unchanged — it is made from the
Dutch page as it stands, so the two pages say the same thing; and the Dutch page is not touched.

The copy is read back into the shape the generator writes it in: ``usps[0]`` the tagline (the ACF
field fed by ``generated_tagline``), ``usps[1:]`` the bullets of the description's first block
(``generated_description``, assembled by :func:`lib.generator._assemble_description`). A page whose
description carries a block the generator does not write — edited by hand in WordPress — yields
nothing rather than a guess, and its translations are not written.

Read-only: one GET per page, by id and without a language parameter
(:meth:`lib.wp_client.WordPressClient.read_page`).
"""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING, Final

from lib.errors import WordPressAPIError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lib.config import WordPressConfig
    from lib.wp_client import WordPressClient

_log = logging.getLogger(__name__)

#: The record fields the copy lands in, as ``wordpress.acf_map`` names them for their source.
_TAGLINE_SOURCE: Final = "generated_tagline"
_DESCRIPTION_SOURCE: Final = "generated_description"
#: ``<p><strong>Heading</strong><br />• one<br />• two</p>`` — the generator's bullet block.
_BLOCK: Final = re.compile(r"<p>\s*<strong>([^<]*)</strong>\s*<br\s*/?>(.*?)</p>", re.DOTALL)
#: ``<p><strong>Tagline</strong></p>`` — the paragraph the description opens with.
_HEAD: Final = re.compile(r"<p>\s*<strong>[^<]*</strong>\s*</p>")
_BREAK: Final = re.compile(r"<br\s*/?>")
_TAG: Final = re.compile(r"<[^>]+>")
_BULLET: Final = "•"
#: Statuses that fail every read alike: the credentials, not the page.
_FATAL_STATUSES: Final = frozenset({401, 403})


def _acf_field(acf_map: Mapping[str, str], source: str) -> str | None:
    return next((field for field, src in acf_map.items() if src == source), None)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", fragment)).split())


def usps_from(tagline: str, description: str, labels: Mapping[str, str]) -> list[str] | None:
    """The copy in a live page's two fields, or ``None`` when they are not the generator's shape.

    ``labels`` are the default language's block headings (:data:`lib.generator._LABELS`). The
    ``eigenschappen`` block holds the bullets; the ``technische`` one is assembled from GS1 data and
    is never translated as copy. A block under any other heading means the description was edited
    by hand, and is not read: translating half of it would be worse than translating none. So is
    a description with anything besides the opening tagline paragraph and those blocks, and one
    without that paragraph: read as "tagline only", either would rewrite the translated page
    without its bullets.
    """
    head = _text(tagline)
    if not head or _HEAD.search(description) is None:
        return None
    known = {labels["eigenschappen"], labels["technische"]}
    bullets: list[str] = []
    for match in _BLOCK.finditer(description):
        heading = _text(match.group(1))
        if heading not in known:
            return None
        if heading == labels["eigenschappen"]:
            lines = [_text(line) for line in _BREAK.split(match.group(2))]
            bullets = [line.removeprefix(_BULLET).strip() for line in lines if line]
    if _text(_HEAD.sub("", _BLOCK.sub("", description), count=1)):
        return None
    return [head, *[bullet for bullet in bullets if bullet]]


def read_live_copy(
    client: WordPressClient,
    wordpress: WordPressConfig,
    pages: Mapping[str, int],
    labels: Mapping[str, str],
) -> dict[str, list[str]]:
    """``gtin -> usps`` for each default-language page in ``pages`` (``gtin -> page id``).

    A page that is gone, or whose text is not in the generator's shape, is left out and logged;
    the caller reports its translations as not written.
    """
    tagline_field = _acf_field(wordpress.acf_map, _TAGLINE_SOURCE)
    description_field = _acf_field(wordpress.acf_map, _DESCRIPTION_SOURCE)
    if tagline_field is None or description_field is None:
        _log.warning("acf_map names no field for the tagline or the description; nothing read")
        return {}
    found: dict[str, list[str]] = {}
    for gtin, page_id in pages.items():
        try:
            page = client.read_page(wordpress.post_type, page_id) or {}
        except WordPressAPIError as exc:
            # One page the site will not serve costs that page's translation, not the others'.
            # A refusal that is the same for every page — the credentials — still ends the read.
            if exc.status_code in _FATAL_STATUSES:
                raise
            _log.warning("live copy of %s (page %s) could not be read: %s", gtin, page_id, exc)
            continue
        acf = page.get("acf")
        values = acf if isinstance(acf, dict) else {}
        usps = usps_from(
            str(values.get(tagline_field) or ""), str(values.get(description_field) or ""), labels
        )
        if usps is None:
            _log.warning("live copy of %s (page %s) could not be read back", gtin, page_id)
            continue
        found[gtin] = usps
    return found
