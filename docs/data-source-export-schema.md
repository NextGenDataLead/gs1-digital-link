# Data Source export schema

How a GS1 Data Source export becomes `products.json`, and how to map an export you have never seen
before.

Derived from `lib/gdsn.py`, `lib/records.py`, `lib/config.py`, and `scripts/parse_export.py`.

## Two formats

`export.format` in `clients.yml` selects the reader:

| Format | Shape | Reader | Mapped with |
|---|---|---|---|
| `flat` | One sheet, one row per product, header row on top | `lib.records.parse_excel_row` | `column_map` |
| `gdsn` | Multi-worksheet GDSN datapool export | `lib.gdsn.read_workbook` + `build_records` | `gdsn_map` |

**Start by assuming `gdsn`.** A real MyGS1 export from a client publishing to a datapool is a
multi-sheet GDSN file, not a flat article list — that assumption cost this project a phase. Run
`inspect_export` and let the file tell you.

Either way the output is the same: `output/{client_id}/data/products.json`, a bare JSON array of
`ProductRecord`s.

## Inspect first

```bash
python -m scripts.inspect_export input/{client_id}/products.xlsx
```

For each worksheet this prints the attributes with their GDSN attribute id, the languages present,
and the first few sample values — plus a suggested `export` block. It is the fastest way to see what
you actually have. Example output:

```
### CatalogueItem  (359 rows)
  Targetsector (4638)  (attr=4638)
      e.g. DIY
  GPC classification category code (3122)  (attr=3122)
      e.g. 10005844 | 10005426 | 10000546
  Name of information provider (3090)  (attr=3090)
      e.g. Democlient B.V.
```

## The GDSN structure

Worth understanding, because the mapping only makes sense once you do:

- **One worksheet per GDSN module** — `TradeItemDescription`, `MarketingInformation`,
  `TradeItemMeasurements`, `ReferencedFileDetailInformation`, and so on.
- **Seven header rows per sheet; data starts on the eighth.** Each column's identity is a nested
  attribute *path* (`TradeItemDescriptionInformation > DescriptionShort[0] > Value`) plus a human
  *label* carrying the stable GDSN attribute number — `"Short product name (3297)"`. The number in
  parentheses is what you map against; the label text is not stable.
- **Every sheet is keyed on `Gtin` + `TargetMarketCountryCode` + `TradeItemUnitDescriptorCode`.** The
  same GTIN therefore recurs **once per target market**.
- **Localised text is adjacent `LanguageCode` / `Value` column pairs** inside a repeated group.
  Measurements are `MeasurementUnitCode` / `Value` pairs.
- Reference and metadata sheets with no digit-keyed data rows are skipped automatically.

`build_records` joins across sheets by GTIN and produces one canonical record per product.

## Mapping

```yaml
export:
  format: gdsn
  path: "input/{client_id}/products.xlsx"
  market_priority: ["528", "056", "276", "442"]
  gdsn_map:
    product_name:  {attribute: "3297"}
    brand:         {attribute: "3336"}
    image_url:     {attribute: "2485"}
  gdsn_extras:
    material:      {attribute: "..."}
```

- **`gdsn_map`** — `ProductRecord` field → source attribute. A `GdsnSource` identifies either a GDSN
  attribute number or a path segment.
- **`gdsn_extras`** — named pass-through attributes carried into `extras`, reachable from a template
  as `{{extras.material}}` or from `acf_map` as `extras.material`. Use this for anything that is not
  a first-class `ProductRecord` field.
- **`extras_columns`** — the `flat` equivalent.
- **`column_map`** — for `flat` exports: `{column_header: product_record_field}`. A target that is not
  a real `ProductRecord` field raises `ExportParseError` **at config load** (E6), not mid-parse.

`lib/config.py` (`ExportConfig`) is the authoritative field list.

### `market_priority` — how multi-market rows resolve

Because a GTIN recurs once per target market, a field can have several candidate values. For each
field **and each language**, the first market in `market_priority` that supplies a non-blank value
wins. The same list picks scalars.

This is deliberately *not* a `{market: language}` map. An earlier design used one and it baked in a
1:1 market↔language constraint the real export contradicts: **every market row carries every
language**, so which market happens to hold a given value varies product by product. A priority list
handles that; a map cannot.

Multiple market rows for one GTIN are **aggregated into one record** — they are not duplicates and are
not reported as such.

## Language resolution

```yaml
wordpress:
  default_language: nl
  languages: ["nl", "fr"]
```

`languages` is passed to `build_records` explicitly (it is not derivable from the market list), and
`default_language` must be one of them.

- `product_name` in the **default language** is **required** — a GTIN without it is an
  `ExportParseError` naming the GTIN (E5).
- A language with no `product_name` for a given GTIN gets its row classified **SKIPPED** in the plan
  and surfaced in the chat summary (E18) — it never publishes a half-empty page.

`REQUIRED_FIELDS` is `{brand, product_name}`.

## Consumer units only

Records are built from rows whose `TradeItemUnitDescriptorCode` is `BASE_UNIT_OR_EACH`. Cases,
pallets, and display units are not products with consumer-facing pages.

## Images

Image URLs come from the `ReferencedFileDetailInformation` module: a `ReferencedFileHeader` group
whose `ReferencedFileTypeCode` is `PRODUCT_IMAGE`, read from its `UniformResourceIdentifier` leaf,
honouring `IsPrimaryFile`.

Expect **print masters**. In the pilot, 92% were `image/tiff` at 10–45 MB — WordPress rejects those
outright, which is why every image is converted to a web JPEG at upload. See
[`wordpress-onboarding.md`](wordpress-onboarding.md#images).

## Parse and check

```bash
python -m scripts.parse_export --dry-run    # validate, write nothing
python -m scripts.parse_export              # write products.json
```

`--dry-run` reports warnings without producing a file. Exit codes: **0** ok, **1** parse errors, **2**
config errors. On any parse error the script writes **nothing** — you never get a partial
`products.json`.

Then review data quality properly:

```bash
python -m scripts.report_quality
# -> output/{client_id}/data-quality-report.md
```

The report surfaces blanks, cross-market disagreements, suspected brand typos, wrong-language values,
and generation-inference gaps. **Fix what it finds in MyGS1, at the source.** Do not invent values
downstream — a blank marketing message must stay blank rather than become fabricated copy, and the
generator's held-row behaviour (E21) depends on that discipline.

## Parse-time edge cases

| # | Condition | Behaviour |
|---|---|---|
| E1 | GTIN with leading zeros | Preserved, never stripped |
| E2 | GTIN as an integer (openpyxl cast) | Coerced to string, zero-padded |
| E3 | Duplicate GTINs | First wins; the rest WARN and are skipped |
| E4 | Empty or key-less row | Skipped silently |
| E5 | No `product_name` in the default language | `ExportParseError` naming the GTIN |
| E6 | `column_map` target is not a `ProductRecord` field | `ExportParseError` at config load |
| E16 | More columns than mapped | WARN per unmapped column |
| E17 | Fewer columns than expected | `ExportParseError` if required, WARN if optional |

GTINs are handled as **strings throughout**. A 13-digit GTIN is zero-padded to 14 where the API needs
it (`{gtin14}`); leading zeros are never lost.

## GPC categories

`GPC classification category code` (attribute `3122`) gives each product its brick. Bricks do not map
cleanly onto a site's marketing categories — one brick can hold both garden tools and a nutcracker,
and a client's own categorisation is not purely semantic. So the mapping is a
`categories.brick_category_map` **plus** a per-GTIN `overrides` list, reviewed by the client:

```bash
python -m scripts.build_brick_map --datamodel diy-datamodel.xlsx   # draft
python -m scripts.build_brick_map --check                          # exit 1 if unmapped
```

Unmapped bricks **warn**; the tool never guesses a category.

## See also

- [`setup.md`](setup.md) — the pipeline and the onboarding walkthrough.
- [`template-variables.md`](template-variables.md) — where these fields land on a page.
- [`troubleshooting.md`](troubleshooting.md) — every parse error.
- `IMPLEMENTATION_SPEC.md` §2 (type definitions), §3 (column mapping), §3.6 (GDSN join).
