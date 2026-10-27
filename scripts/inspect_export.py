"""Inspect a GDSN export and suggest a ``gdsn_map`` (IMPLEMENTATION_SPEC §8.5).

Usage:
    python -m scripts.inspect_export EXCEL_PATH

Prints, for each worksheet, the resolved GDSN attributes (label, attribute id,
whether per-language, sample values), then a ready-to-paste ``export`` block with a
suggested ``gdsn_map`` for the recognised product-page attributes. An onboarding aid:
the operator tunes the suggestion into ``clients.yml``.
"""

from __future__ import annotations

import sys
from typing import NamedTuple

import yaml
from openpyxl.utils.exceptions import InvalidFileException

from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.gdsn import GdsnColumn, GdsnSheet, read_workbook
from lib.records import _coerce_cell

_USAGE = "usage: python -m scripts.inspect_export EXCEL_PATH"

_KEY_SEGMENTS = frozenset(
    {
        "Gtin",
        "TargetMarketCountryCode",
        "InformationProviderOfTradeItem",
        "TradeItemUnitDescriptorCode",
    }
)

#: Recognised GDSN attributes → (ProductRecord field, GdsnSource extra kwargs).
#:
#: **Attribute ids, never labels.** 3297's label in a real export is *"Short product
#: name"*, which reads exactly like the page title and is not: it is DescriptionShort, an
#: internal logistics string ("Schroefverwijderaar metaal grs"). This table mapped it to
#: ``product_name`` and 3318 to ``description_long`` — both wrong, both fixed in the tuned
#: config at c76492b, and both still suggested here afterwards. Since ``clients.yml``
#: points operators at this script for column discovery, it re-proposed the bug the
#: project had already paid to find. Verify a mapping against real values before adding
#: one; a plausible label is what caused this.
#:
#: 3297 is deliberately absent: it belongs in ``gdsn_extras`` as a pass-through
#: (``logistics_name``), not in ``gdsn_map`` as a product-page field.
_KNOWN_ATTRIBUTES: dict[str, tuple[str, dict[str, object]]] = {
    "3318": ("product_name", {"localised": True}),  # TradeItemDescription — the page title
    "1083": ("description_short", {"localised": True}),  # TradeItemMarketingMessage
    "1067": ("description_long", {"localised": True}),  # TradeItemFeatureBenefit
    "3336": ("brand", {}),
    "3510": ("net_content", {"with_unit": True}),
    "GpcCategoryCode": ("gpc_brick_code", {}),
    "2485": ("image_url", {"primary_file": True}),
}

#: Per-field tuning this script cannot infer, printed alongside the suggestion. The values
#: are client-specific (a brand prefix, a theme's slot width), so they are named rather
#: than guessed — but naming them beats omitting them silently, which is how a suggestion
#: gets pasted in as if it were complete.
_TUNING_HINTS: tuple[str, ...] = (
    "strip_prefix: <str>  — on product_name, when the feed repeats the brand in the name.",
    "max_length: <int>    — flags values too long for their slot (reported, never truncated).",
    "gdsn_extras:         — pass-through attributes (e.g. 3297 DescriptionShort) that are",
    "                       not product-page fields but are worth carrying.",
)

_SAMPLE_LIMIT = 3
_SAMPLE_ROWS = 25
_SAMPLE_WIDTH = 60


class _Attribute(NamedTuple):
    key: str
    leaf: str
    label: str
    localised: bool
    languages: list[str]
    samples: list[str]


def _summarise_sheet(sheet: GdsnSheet) -> list[_Attribute]:
    """Group a sheet's non-key columns into per-attribute summaries."""
    groups: dict[str, list[GdsnColumn]] = {}
    for column in sheet.columns:
        if column.path and column.path[0] in _KEY_SEGMENTS:
            continue
        key = column.attr_id or column.leaf_name
        if key:
            groups.setdefault(key, []).append(column)

    rows = list(sheet.rows_by_key.values())[:_SAMPLE_ROWS]
    attributes: list[_Attribute] = []
    for key, columns in groups.items():
        localised = any(c.leaf_name == "LanguageCode" for c in columns)
        label = next((c.label for c in columns if c.label), key) or key
        value_cols = [c for c in columns if c.leaf_name == "Value"] or [
            c for c in columns if c.leaf_name not in ("LanguageCode", "MeasurementUnitCode")
        ]
        leaf = value_cols[0].leaf_name if value_cols else ""
        lang_cols = [c for c in columns if c.leaf_name == "LanguageCode"]
        samples: list[str] = []
        languages: set[str] = set()
        for row in rows:
            for col in value_cols:
                value = _coerce_cell(row[col.index]) if col.index < len(row) else None
                if value and value not in samples:
                    samples.append(value[:_SAMPLE_WIDTH])
            for col in lang_cols:
                lang = _coerce_cell(row[col.index]) if col.index < len(row) else None
                if lang:
                    languages.add(lang)
            if len(samples) >= _SAMPLE_LIMIT:
                break
        attributes.append(
            _Attribute(key, leaf, label, localised, sorted(languages), samples[:_SAMPLE_LIMIT])
        )
    return attributes


def _suggest_map(sheets: dict[str, GdsnSheet]) -> dict[str, object]:
    """Build a suggested ``export`` block from recognised attributes."""
    gdsn_map: dict[str, dict[str, object]] = {}
    markets: set[str] = set()
    for name, sheet in sheets.items():
        markets.update(market for (_gtin, market) in sheet.rows_by_key)
        for attr in _summarise_sheet(sheet):
            known = _KNOWN_ATTRIBUTES.get(attr.key) or _KNOWN_ATTRIBUTES.get(attr.leaf)
            if known is None:
                continue
            field, kwargs = known
            if field in gdsn_map:
                continue
            gdsn_map[field] = {"sheet": name, "attribute": attr.key, **kwargs}
    return {
        "format": "gdsn",
        "market_language": {market: "??" for market in sorted(markets)},
        "gdsn_map": gdsn_map,
    }


def _print_report(sheets: dict[str, GdsnSheet]) -> None:
    for name, sheet in sheets.items():
        attributes = _summarise_sheet(sheet)
        if not attributes:
            continue
        print(f"\n### {name}  ({len(sheet.rows_by_key)} rows)")
        for attr in attributes:
            flag = " [localised]" if attr.localised else ""
            langs = f" langs={attr.languages}" if attr.languages else ""
            sample = " | ".join(attr.samples)
            print(f"  {attr.label}  (attr={attr.key}){flag}{langs}")
            if sample:
                print(f"      e.g. {sample}")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    if len(args) != 1:
        print(_USAGE, file=sys.stderr)
        return 2
    try:
        sheets = read_workbook(args[0])
    except (FileNotFoundError, OSError, InvalidFileException) as exc:
        # InvalidFileException is openpyxl's own and does NOT subclass OSError, so
        # without it a non-.xlsx argument (a typo, a .xls, or a stray flag) escapes
        # as an unhandled traceback instead of this message.
        print(f"cannot read export: {exc}", file=sys.stderr)
        return 1

    _print_report(sheets)
    print("\n# Suggested clients.yml export block (map market codes to languages, then tune):")
    print(yaml.safe_dump({"export": _suggest_map(sheets)}, sort_keys=False, allow_unicode=True))
    print("# A starting point, not a mapping. Check each attribute against its sample values")
    print('# above — labels lie (3297 is called "Short product name" and is not one).')
    print("# This script cannot infer:")
    for hint in _TUNING_HINTS:
        print(f"#   {hint}")
    return 0


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
