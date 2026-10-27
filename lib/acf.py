"""Assemble the ACF field payload for one (product, language).

Some WordPress themes render a page from **ACF fields** rather than ``post_content`` —
Democlient's Oxygen template is one, and its ``post_content`` is empty on every published
page. For those clients the Phase 5 model (render HTML into the body) produces a page that
returns 200, passes ``verify_url``, reports ``ok``, and shows the customer nothing. This
module supplies what such a client needs instead: a mapping from ACF field name to the
:class:`~lib.records.ProductRecord` field that feeds it.

The mapping lives in ``clients.yml`` (``wordpress.acf_map``), not here: field names are the
client's, and a tool that hardcodes ``product_title`` is a tool that only serves Democlient.

See ``docs/clients/democlient-page-adapter.md`` §3–§4.1 for how the fields were established
against the live site.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lib.records import LocalisedText

if TYPE_CHECKING:
    from lib.records import ProductRecord

_log = logging.getLogger(__name__)

#: Separator for reaching into ``ProductRecord.extras``, e.g. ``extras.material``.
_EXTRAS_PREFIX = "extras."


def _resolve(product: ProductRecord, field: str, language: str) -> str | None:
    """Resolve one ``acf_map`` source field to a plain string for ``language``.

    Localised fields yield the value for this language (``None`` when that language is
    absent — the caller omits the field rather than falling back to another language, which
    would put Dutch text on a French page).
    """
    if field.startswith(_EXTRAS_PREFIX):
        # No fallback, for the same reason as the localised branch below: an ACF field is
        # written on its own, so omitting it beats writing another language's text into it.
        return product.extra(field[len(_EXTRAS_PREFIX) :], language)
    value = getattr(product, field, None)
    if value is None:
        return None
    if isinstance(value, LocalisedText):
        return value.values.get(language)
    return str(value)


def build_acf_payload(
    product: ProductRecord, language: str, acf_map: dict[str, str]
) -> dict[str, object]:
    """Build the ACF payload for one page from the client's ``acf_map``.

    Args:
        product: The product being published.
        language: The page's language; selects the value from localised source fields.
        acf_map: ``{acf_field_name: product_record_field}``, from
            ``clients.yml`` ``wordpress.acf_map``. Reach into extras with
            ``extras.{name}``. Several ACF fields may share one source — Democlient's
            tagline feeds both ``product_title`` and ``product_header_video_text``.

    Returns:
        ``{acf_field_name: value}``, omitting fields whose source is absent or empty for
        this language. An empty payload is returned as ``{}``; the caller skips the ACF
        write entirely rather than sending nothing.

    Raises:
        ConfigError: Never — an unresolvable field is a warning, not a failure. A missing
            tagline should not stop a page being published with its title and image.
    """
    payload: dict[str, object] = {}
    for acf_field, source in acf_map.items():
        value = _resolve(product, source, language)
        if value:
            payload[acf_field] = value
        else:
            _log.warning(
                "no value for acf.%s (from %r) on %s (%s); field omitted",
                acf_field,
                source,
                product.gtin,
                language,
            )
    return payload
