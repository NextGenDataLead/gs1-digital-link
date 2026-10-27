"""The header grid of a GS1 Data Source / GDSN export, written rather than read.

The mirror of :func:`lib.gdsn._parse_columns`. That function reconstructs a column's attribute
path from the header rows above it; this one lays a path back out across those rows. They are
the same fact in opposite directions, which is why a fixture built here is a real test of the
reader rather than a restatement of it — ``tests/lib/test_gdsn_layout.py`` round-trips one
through the other.

Its only caller today is :mod:`lib.demo_export`. It is separate from that module because the two
answer different questions — *what a GDSN sheet looks like* is not *what our demo products are* —
and because everything here is derived from the real export's structure, while everything there
is invented.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: Every module sheet a GS1 Data Source export carries, in the order the real one lists them.
MODULES: Final[tuple[str, ...]] = (
    "AccessControl",
    "Access Control Group",
    "CatalogueItem",
    "BatteryInformation",
    "BrickGPCCommercialData",
    "ConsumerInstructions",
    "DangerousSubstanceInformation",
    "DeliveryPurchasingInformation",
    "DutyFeeTaxInformation",
    "LightingDevice",
    "MarketingInformation",
    "PackagingInformation",
    "PackagingMarking",
    "PlaceOfItemActivity",
    "ProductInformation",
    "ReferencedFileDetailInformation",
    "RegulatedTradeItem",
    "SalesInformation",
    "TradeItemDescription",
    "TradeItemHandling",
    "TradeItemMeasurements",
    "TransportationHazardousClass",
    "VariableTradeItemInformation",
    "WarrantyInformation",
)

#: Header rows before the first data row. Fixed by the export format.
HEADER_DEPTH: Final = 7


@dataclass(frozen=True)
class Column:
    """One column: its nested attribute path, and the label carrying the GDSN number."""

    path: tuple[str, ...]
    label: str


#: The four key columns every module sheet opens with. Every sheet is keyed on ``Gtin`` +
#: ``TargetMarketCountryCode`` + ``TradeItemUnitDescriptorCode``, which is what lets the reader
#: join across sheets by GTIN and skip rows above the consumer unit.
KEY_COLUMNS: Final[tuple[Column, ...]] = (
    Column(("Gtin",), "GTIN (Global Trade Item Number) (3059)"),
    Column(("TargetMarketCountryCode",), "Country of sale code (3179)"),
    Column(("InformationProviderOfTradeItem", "Gln"), "Information provider (GLN) (gln) (3088)"),
    Column(("TradeItemUnitDescriptorCode",), "Product hierarchy level code (3074)"),
)


def header_rows(columns: Sequence[Column]) -> list[list[object]]:
    """The seven header rows, with each column's path spread down them.

    The placement rule, read off a real export: the **first** path segment sits on row 0, the
    remaining segments are **bottom-aligned** so the leaf lands on the last header row before the
    label, and the label is the final row. A two-segment path therefore uses rows 0 and 5, a
    three-segment path 0, 4 and 5, and a four-segment path 0, 3, 4 and 5 — the alignment is to
    the leaf, not to the root, which is why depth varies between sheets without the leaf moving.

    The reader does not care: it collects a column's non-blank header cells top to bottom and
    takes the last as the label. Mirroring the real placement anyway is what makes an
    ``inspect_export`` run against a synthetic file look like one against a client's.
    """
    grid: list[list[object]] = [[None] * len(columns) for _ in range(HEADER_DEPTH)]
    for index, column in enumerate(columns):
        head, *rest = column.path
        grid[0][index] = head
        for offset, segment in enumerate(reversed(rest)):
            grid[HEADER_DEPTH - 2 - offset][index] = segment
        grid[HEADER_DEPTH - 1][index] = column.label
    return grid


def localised_columns(
    prefix: tuple[str, ...], group: str, slots: int, value_label: str, language_label: str
) -> list[Column]:
    """Columns for a repeated ``LanguageCode``/``Value`` group, ``slots`` slots wide.

    ``language_label`` is separate because the real export spells it both ways: under
    ``TradeItemDescription`` both columns of a pair carry the same label, while under
    ``MarketingInformation`` the language column reads "Language (1083)" against the value
    column's "Product marketing message (1083)".
    """
    return [
        Column((*prefix, f"{group}[{slot}]", leaf), label)
        for slot in range(slots)
        for leaf, label in (("LanguageCode", language_label), ("Value", value_label))
    ]


def localised_cells(
    values: Mapping[str, str], languages: Sequence[str], slots: int, carried: Sequence[str]
) -> list[object]:
    """One slot per language, in ``languages`` order, padded with blanks out to ``slots``.

    A language absent from ``carried`` — this market does not hold it — or one the product has no
    value for leaves **both** cells of its slot empty, rather than writing a language code beside
    nothing. A dangling ``LanguageCode`` is not what an absent value looks like in the feed, and
    it would make the blank reachable by anything that pairs on the language column alone.
    """
    cells: list[object] = []
    for language in languages:
        value = values.get(language) if language in carried else None
        cells.extend([language, value] if value else [None, None])
    cells.extend([None, None] * (slots - len(languages)))
    return cells


def multislot_cells(
    values: Mapping[str, Sequence[str]],
    languages: Sequence[str],
    slots: int,
    carried: Sequence[str],
) -> list[object]:
    """A repeated group where one language occupies several slots — attribute 1067's shape.

    Each language gets a contiguous block of ``slots // len(languages)`` slots, which is how the
    feed spreads a product's features across ``TradeItemFeatureBenefit[n]``. Reading the block
    back whole is what ``multivalue`` is for; reading only slot 0 is the bug it was added for.
    """
    per_language = slots // len(languages)
    cells: list[object] = []
    for language in languages:
        entries = list(values.get(language, ())) if language in carried else []
        block = entries[:per_language]
        for entry in block:
            cells.extend([language, entry])
        cells.extend([None, None] * (per_language - len(block)))
    cells.extend([None, None] * (slots - per_language * len(languages)))
    return cells


def measurement_columns(prefix: tuple[str, ...], group: str, label: str) -> list[Column]:
    """Columns for a ``MeasurementUnitCode``/``Value`` pair."""
    return [Column((*prefix, group, leaf), label) for leaf in ("MeasurementUnitCode", "Value")]


def measurement_cells(value: str | None, unit: str) -> list[object]:
    """A measurement's two cells, both blank when there is no value for the unit to qualify."""
    return [unit, value] if value else [None, None]
