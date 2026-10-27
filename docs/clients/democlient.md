# Democlient — client notes

Client-specific quirks and decisions. Expanded during the Phase 9 pilot; started in
Phase 7 to record the create-only gate.

## Process list (which GTINs a run may touch)

Democlient's run is **create-only**, and which products are in scope comes from an
operator-maintained file, **not** the datasource export:

- **Location:** `input/democlient/process/selection/selections.xlsx` (git-ignored, operator-provided).
- **Required column:** the GTIN column only — `Barcode` by default, relabelable via
  `process_list.gtin_column`. Every other column is ignored, so the operator can keep
  their own working notes (article number, description, existing URL) beside it.
- **Join key:** the barcode, normalised to GTIN-14, so a 13-digit value joins to
  `ProductRecord.gtin14` regardless of the leading zero.
- **Rule:** **every GTIN in the file is processed.** Products absent from it are excluded
  and reported in the `run_plan` summary. Nothing else is read and no cell value is
  interpreted.

**Preparing the file is the operator's job.** Delete the rows that should not run,
applying whatever rule the business uses — already live, not yet in GS1, seasonal, on
hold. The tool deliberately holds no opinion, because the previous version did: it read
"already on website" / "already in GS1" columns by presence, so any non-blank cell meant
*true* and a file saying `no` silently meant the opposite. See `lib/process_list.py`.

Configured under `clients.yml` → `democlient.process_list`. Because every listed row is
new, the hash-based CHANGED/diff detection (built per §8.2/§4.8) stays dormant at runtime —
it is exercised by unit tests and reserved for future product updates.

This file is **not part of the original spec** — a deliberate, user-approved extension for
the pilot workflow (see CHANGELOG, Phase 7).
