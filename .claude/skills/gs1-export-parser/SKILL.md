---
name: gs1-export-parser
description: "Parse a client's GS1 Data Source / GDSN datapool workbook into the normalised products.json the pipeline reads, surfacing source-data issues to fix in MyGS1. Use when the operator says 'parse the export for {client}' or drops an .xlsx in chat."
---

# GS1 Export Parser

## When to load

Trigger phrases: **"parse the export for {client}"**, **"parse {client}'s export"** (§10.1) — e.g.
"parse the export for democlient", or when the operator drops an `.xlsx` in chat. Load this skill to
turn the client's GS1 Data Source / GDSN datapool workbook into the normalised
`output/{client}/data/products.json` the rest of the pipeline reads, and to surface the source-data
issues that need fixing at MyGS1.

## What this skill does

Runs `scripts/parse_export.py`, which reads the client's export config from `clients.yml` and parses
the workbook at `export.path` via `lib/gdsn.py` (for `format: gdsn`; a flat parser handles simple
exports). It resolves each `(GTIN, language)` field by walking `market_priority` and taking the first
non-blank value, then writes `output/{client}/data/products.json` (an array of `ProductRecord`) and
**always** `output/{client}/data/source_issues.json` — the work list of datapool defects
(inconsistencies across markets, blanks, over-length values) to fix in MyGS1. Nothing here is
published: this is the read step that feeds `run_generate`/`run_plan`. Tone is **concise and
business-like, not conversational** — the operator is checking counts, not reading prose.

## Inputs

- `client_id` (from the trigger phrase; ask if unclear).
- `clients.yml` config for the client — `export.format`, `export.path`, `export.market_priority`,
  `export.gdsn_map`, `export.gdsn_extras`, and `wordpress.languages` / `wordpress.default_language`.
- The export workbook at `export.path` (e.g. `input/{client}/products.xlsx`).

## Steps

1. **Resolve the client.** Determine `client_id` from the request; ask if ambiguous.

2. **Parse the export.** Run `python -m scripts.parse_export {client}`. Use `--dry-run` to validate
   without writing either file, or `--output PATH` to override the products destination. It writes
   `output/{client}/data/products.json` and `output/{client}/data/source_issues.json`.

3. **Summarise counts and warnings (§10.1).** Read the stderr line `Parsed {N} products ({W}
   warnings)` and present verbatim:
   ```
   Parsed democlient: 127 products, 4 warnings.
   9 source-data issue(s) need fixing at MyGS1 — see output/democlient/data/source_issues.json.
   ```
   Drop the second line when `source_issues.json` is `[]`. An empty list means "checked, clean"; a
   **missing** file means no run has looked yet — do not read the absence as clean.

4. **Point at the fixes.** If issues exist, summarise the classes present (e.g. cross-market
   inconsistency, blank required field, length overflow) so the operator can fix them at the source.
   These are the datapool's future work, not a reason to block the run.

## How the work is done

Python. This skill drives `scripts/parse_export.py`, which uses `lib/gdsn.py`. There is no MCP
server for parsing and no `.mcp.json` — the export is read locally from `export.path`.

## Failure modes

- **Exit 2 — config error.** Bad `client_id`, a missing or corrupt/unreadable workbook, or an
  unmapped required target: `scripts/parse_export.py` prints `config error: {detail}` to stderr and
  writes nothing. Surface the stderr line and stop.
- **Exit 1 — parse errors, no output.** When rows fail validation — e.g. a GTIN missing
  `product_name.{default_language}` — the script logs each error, prints `{N} parse errors`, and
  **writes no `products.json`**. Fix the flagged rows at the source and re-run; do not proceed to
  `run_generate`/`run_plan` against a stale or absent products file.
- **Silent GDSN skips.** `lib/gdsn.py` silently drops reference/metadata sheets, rows with no
  digit GTIN, and rows whose unit is not `CONSUMER_UNIT`. A lower-than-expected product count is
  usually one of these — not an error the exit code will show.
- **`--dry-run` writes neither file.** It validates only. Do not expect `products.json` to change
  after a dry run.
- **This pipeline fails silently.** `Parsed N products` means the JSON was written, not that the
  values are right. Eyeball a sample of `products.json` (nl **and** fr) against the real export
  before continuing — never trust the count alone.
