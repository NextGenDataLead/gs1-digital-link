"""Which mandatory source values a product is missing — the E23 hold, as a pure function.

A page assembled from an incomplete record is worse than no page: it publishes, the QR resolves,
and the product looks finished until someone reads it. So a SKU missing any mandatory value is held
out of the plan entirely rather than published thin, and the gap is *reported* — the fix belongs in
MyGS1, never downstream. ``docs/setup.md`` states the invariant: never invent product data.

**The hold is per product, not per language.** A localised field must carry a value in every
configured language, and a product missing one language is held in all of them. The alternative —
publishing nl while fr is missing — leaves a SKU half-live, which reads as success on every surface
that counts pages.

Mandatory-ness is declared in ``clients.yml`` on each source — ``gdsn_map`` **and**
``gdsn_extras`` alike — not hard-coded here: which fields a client's page cannot do without is a
property of that client's template. ``required_group`` covers the either-or case — Democlient's
generator writes from attr 1083 *or* 1067 and needs only one, so neither is individually
mandatory but the pair is.

Nothing here reads a file, and it never mutates a record.
"""

from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from lib.gdsn import GdsnSource
from lib.records import LocalisedText, ProductRecord


class MandatoryGap(NamedTuple):
    """One missing mandatory value, in the words the report and the skip reason both use."""

    #: The ``gdsn_map`` field name, or the group name when a whole either-or group is empty.
    field: str
    #: The language it is missing in, or ``""`` for a language-agnostic field.
    language: str
    #: The GDSN attribute(s) an operator must fill in MyGS1 — the actionable half.
    attribute: str

    @property
    def label(self) -> str:
        """``field.lang (attr N)`` — one cell in the report, one line in a log."""
        where = f"{self.field}.{self.language}" if self.language else self.field
        return f"{where} (attr {self.attribute})" if self.attribute else where


def value_for(product: ProductRecord, field: str, language: str) -> str:
    """The product's value for ``field`` in ``language``, as a string ("" when absent).

    Public because the coverage matrix asks the same question of the same fields: whether a
    ``required_group`` is satisfied in a language is one rule, and the report saying a SKU is held
    while the plan publishes it (or the reverse) is the failure that rule exists to prevent.

    Falls through to the pass-through extras, which :meth:`ProductRecord.extra` resolves for
    the asked-for language — a per-language extra read flat would report present in every
    language on the strength of one, which is the direction that publishes a half-translated
    page.
    """
    value = getattr(product, field, None) or product.extra(field, language)
    if isinstance(value, LocalisedText):
        return str(value.values.get(language) or "").strip() if language else ""
    return str(value or "").strip()


def _present(product: ProductRecord, field: str, source: GdsnSource, language: str) -> bool:
    """Whether ``field`` carries a value for ``language`` (ignoring language when not localised)."""
    return bool(value_for(product, field, language if source.localised else ""))


def missing_mandatory(
    product: ProductRecord,
    sources: dict[str, GdsnSource],
    languages: list[str],
) -> list[MandatoryGap]:
    """Every mandatory value ``product`` lacks. Empty means it may publish (E23).

    Args:
        product: The record to check.
        sources: The client's declared sources — :attr:`ExportConfig.all_sources`, *both* maps.
            Named for what it is rather than for one of the two, because this took the
            ``gdsn_map`` alone and so ignored ``required`` on every ``gdsn_extras`` entry: a
            client could mark one mandatory and watch nothing happen. Pass one map and the
            report and the plan check different sets, which holds a SKU on one surface while the
            other publishes it.
        languages: The configured site languages; a localised field is checked in each.

    Returns:
        One :class:`MandatoryGap` per missing value, in ``sources`` order then language order, so
        two runs over the same data report the same thing in the same sequence. A field mandatory
        in two languages and missing in both yields two gaps — the operator fixes two cells.
    """
    gaps: list[MandatoryGap] = []
    groups: dict[str, list[tuple[str, GdsnSource]]] = defaultdict(list)

    for field, source in sources.items():
        if source.required_group:
            groups[source.required_group].append((field, source))
            continue
        if not source.required:
            continue
        for language in languages if source.localised else [""]:
            if not _present(product, field, source, language):
                gaps.append(MandatoryGap(field, language, source.attribute))

    for name, members in groups.items():
        attributes = "/".join(source.attribute for _, source in members if source.attribute)
        localised = any(source.localised for _, source in members)
        for language in languages if localised else [""]:
            if any(_present(product, field, source, language) for field, source in members):
                continue
            gaps.append(MandatoryGap(name, language, attributes))

    return gaps
