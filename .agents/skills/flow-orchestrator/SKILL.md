---
name: flow-orchestrator
description: "Publish a client's products to GS1 Digital Link and WordPress end-to-end — generate content, plan, confirm, then execute pages, GS1 resolver entries and QR, with step-by-step operator gates. Use when the operator says 'publish {client} to GS1', 'run the GS1 pipeline for {client}', 'run for {client}', or 'process {client}', and for any request to create only the pages or only the Digital Links: this skill classifies which of the three publish modes is meant and confirms it. This is the only sanctioned path for publishing: it is what enforces the review gates."
---

# Flow Orchestrator

## When to load

Trigger phrases (§10.5), most to least specific:

- **"publish {client} to GS1"** ← preferred
- **"run the GS1 pipeline for {client}"**
- **"run for {client}"**, **"process {client}"** — short forms, kept for continuity

e.g. "publish democlient to GS1, test env". Load this skill to drive a full client run end-to-end
from chat: parse → plan → present → confirm → execute → summarise.

Also load it for any phrasing that asks for **one leg only** — *"create the pages for democlient but
don't touch GS1"*, *"just set the Digital Links, the pages already exist"*. Those are the same
sequence with a different mode, and step 0 is where the mode gets pinned down.

> **Prefer the GS1-qualified phrasings.** A bare *"run for X"* is generic: in a coding session it
> competes with every other meaning of "run" — including a built-in `run` skill that launches a
> project's app. If this skill is not loaded, the operator gates below **do not happen** and a
> publish can proceed unreviewed. When in doubt, say "publish {client} to GS1".

## Publish modes

One sequence, three modes. The mode decides which leg of `run_execute` runs and how loud the gates
have to be:

| Mode | Slash command | Does | Reversibility |
|---|---|---|---|
| `pages` | `/gs1-pages` | WordPress pages only | Reversible — edit or delete the page |
| `links` | `/gs1-links` | Digital Links only, pointing at pages that already exist | **PERMANENT** |
| `both` | `/gs1-publish` | Pages first, then links pointing at them | **PERMANENT** |

Reached by slash command, the mode is **fixed** and you state it rather than derive it. Reached by
natural language, you classify it at step 0 and the operator confirms. When the phrasing is genuinely
ambiguous — *"publish the webpages and digital links"* names all three vocabularies — ask; do not
guess toward the more destructive mode.

## What this skill does

Orchestrates the generate/plan/confirm/execute pipeline for one client, in whichever of the three
modes above applies. For a client with a
`generator` config it first writes and reviews this run's generated content (review gate 1), then
runs `scripts/run_plan.py` to classify each `(GTIN, language)` — which merges that copy — and presents
the plan (review gate 2), collects the operator's confirmation in chat, writes a `ConfirmedPlan` to
`output/{client}/plan.confirmed.json`, and invokes `scripts/run_execute.py` on the confirmed subset
— then reports the outcome. Generated content is reviewed **twice before it can reach a page** (the
results file, then `plan.json`); execute then writes at `wordpress.post_status`, which ships as `publish`,
so a page is **live the moment it is written**. Tone is **concise and business-like, not
conversational** (§10.6): verbose text creates fatigue during batch runs.

The flow is **create-only by default**: `run_plan.py` gates products through the
**process list**, so only the GTINs the operator listed are candidates. Every GTIN in
that file is processed — the tool reads no status columns, and the operator prepares the
file by deleting the rows that should not run. A GTIN that is already published *and*
resolvable is then dropped as finished, so ordinarily every candidate is NEW.

**When source data changes after a product goes live, that default is wrong**, and it fails
quietly: the finished GTIN is removed before classification, the plan comes back empty, and a run
reports success having written nothing — which reads exactly like "there was nothing to do".
`python -m scripts.run_plan {client} --include-published` re-admits those GTINs and lets the
content hash decide; an untouched product still classifies UNCHANGED and is never executed, so the
flag widens what is *considered*, not what is published.

Do not reach for it by default. A CHANGED row in such a plan **rewrites a live page**, so it is
the operator's choice, and `run_plan` prints a warning above the counts and records
`included_published` in `plan.summary.json`. Surface that warning at step 5 exactly as you would
the E19 reset: above the counts, before the menu.

## Inputs

- `client_id` (from the trigger phrase; ask if unclear).
- `clients.yml` config for the client (languages, environment, `process_list`, `flow`,
  `generator`).
- Parsed products at `output/{client}/data/products.json` (run `parse_export` if absent).
- For a client with a `generator` config, this run's generated content at
  `output/{client}/data/generation_results.json` (written in step 3; `run_plan` reads it). It is
  written fresh each run and never reused — there is no cache.

## Gate index

The nine operator touchpoints, with the id each carries in `lib/gates.py`. **This table is
checked by a test** (`tests/lib/test_gates.py`), in both directions: a gate here with no entry
there, or there with no entry here, fails CI. It exists because the gates are prose and prose
drifts — and a gate that quietly stops being shown raises nothing.

`lib/gates.py` holds the structure (which gates exist, at which step, which are non-negotiable,
which apply in which mode) so a second consumer can render them as forms. This file keeps the
verbatim prompt text. Neither is the copy; both are checked against the table.

It also records **what each answer does** — `advances`, `stops`, or `redisplays`. The third is not
a nicety: an answer like `show-full-diff` prints more and asks again, so it neither carries the
flow on nor stops the run, and a surface that had only the first two to choose from read it as a
cancellation. Here that distinction is carried by the prose ("then re-prompts"); a form has to be
told.

| Gate id | Step | Required | Modes |
|---|---|---|---|
| `intent` | 0 | **yes** | all |
| `languages` | 2 | no | all |
| `content_review` | 3 | **yes** | all, when a `generator` is configured |
| `missing_field` | 4 | no | all, when a unit was dropped for a missing `product_name` |
| `plan_review` | 5 | **yes** | all |
| `row_diff` | 6 | no | all |
| `production` | 8 | **yes — never skippable** | `links`, `both`, on production only |
| `dry_run` | 8.5 | **yes — always runs** | all |
| `post_run` | 11 | no | all |

## Steps

Numbered from **0**, and the numbering is load-bearing: step 0 was added after the rest and the
other eleven keep their numbers so every cross-reference to "step 8" — here, in
`IMPLEMENTATION_SPEC.md` §8.3, in `docs/setup.md` — still points at the same gate.

0. **Intent confirmation (gate 0).** Before running anything, present what is about to happen and
   require a choice. Five things, in this order:

   - **Mode** — `pages`, `links`, or `both`, and what each does *not* touch.
   - **Export file cross-check.** `parse_export` has **no input-path override**: the path comes from
     `clients.yml` → `export.path`. So when the operator names a file (*"/gs1-publish for
     @products-2026-q3.xlsx"*) that filename **verifies the config** rather than driving the run.
     State the configured path and how long ago it was modified, and ask whether it is the same
     file. This catches the likeliest real error — a fresh export dropped somewhere new while
     config still points at the old one — which nothing downstream would notice.
   - **Scope** — how many products this run could touch, from
     `python -m scripts.doctor {client} --json --offline`, the check named `scope`
     (`data.in_scope` of `data.total`). Lead with it and carry its `detail` sentence, which names
     what removed the rest. Give the catalogue total as the *second* number, not the first: it
     used to be the only one here, and on a run scoped to one product it read **127**. Gate 0 is
     where the operator forms their picture of what they are about to do, so the prominent figure
     must describe this run.

     Still **not** the number of rows this run will write — that arrives at the plan gate
     (step 5), and the gap is real rather than rounding: scope counts what the process list and
     the video allowlist admit, and deliberately cannot subtract the units that are already
     published, because deciding that needs `state.json` and an idle read of a corrupt one
     quarantines it (E19). Say "could touch", never "will publish". On the live pilot the two
     read 15 and 5.
   - **Environment** — `test` or `production`, resolved from `clients.yml`.
   - **Permanence**, for `links` and `both` only.

   For `links` / `both`, present verbatim:
   ```
   About to run the GS1 publish flow for democlient.
     Mode:        both — WordPress pages, then Digital Links pointing at them
     Export:      input/democlient/products.xlsx (modified 12 days ago)
     In scope:    15 of 127 parsed products, after the process list and
                  media.restrict_to_mapped_gtins (confirmed video in every language).
                  That is the ceiling on what this run could touch, not the row count.
     Environment: production

   A GS1 Digital Link record can never be deleted. Retraction only disables it; the
   record stays on the account permanently.

   Proceed?
   [confirm | change-mode | cancel]
   ```
   For `pages`, the same block with the permanence paragraph replaced by:
   ```
   Pages only — no GS1 record is written, so this run is reversible.
   ```
   If the operator named a file that does not match `export.path`, add above the menu:
   ```
   You said products-2026-q3.xlsx. Config points at input/democlient/products.xlsx,
   modified 12 days ago. Same file?
   ```
   If the `scope` check came back **`fail`** — nothing in scope — say so above the menu and do
   not present the run as ready:
   ```
   Nothing is in scope: 0 of 127 parsed products survive the process list and
   media.restrict_to_mapped_gtins. This run would write nothing and report success.
   ```
   `change-mode` → re-present with the chosen mode; `cancel` → abort, run nothing.

   **This gate is asymmetric on purpose.** `pages` is reversible, so it **also stands in for the
   step-8 environment confirmation** — gate 0 has already named the environment and nothing
   irreversible follows. `links` and `both` still take step 8 as well.

1. **Resolve the client.** Determine `client_id` from the request; ask if ambiguous. If
   `output/{client}/data/products.json` is missing or stale, run
   `python -m scripts.parse_export {client}` first (the `gs1-export-parser` skill).

2. **Language selection (§10.6.6).** Present verbatim:
   ```
   Client democlient supports [nl, fr]. Which languages should this run cover?
   [all | nl | fr | nl,fr]
   ```
   Default `all`. Remember the chosen subset for step 6.

3. **Generate copy & review (gate 1 of 2).** Skip this step for a client with no `generator`
   config. Otherwise write this run's copy, then review it before planning — the tagline
   and Eigenschappen are LLM-written, so they are reviewed *before* they can reach a page:
   - **In-session (no API key):** run `python -m scripts.run_generate {client} --emit`, then invoke the
     `content-generator` skill to write the copy and `--validate` it; that skill presents the review.
   - **Headless:** run `python -m scripts.run_generate {client} --backend api` (needs the API key).
   **Copy is written fresh every run and never reused**, and the batch is this run's rows — the
   `(GTIN, language)` units that classify NEW or CHANGED. An already-live, unchanged page is not
   republished, so no copy is written for it; `--emit` says how many it set aside for that reason.
   The file is not *pruned* to the batch either, so one written against a longer process list or an
   earlier wave still holds those units. `--validate` counts them as **surplus**, which is not a
   rejection — a rejection is copy the run wanted and cannot use (stale fingerprint). Intersect
   with the in-scope GTINs (`doctor --json`, check `scope`, `data.in_scope_gtins`) before
   concluding anything from the file's size. The shell's Content screen does exactly that.
   Then eyeball a sample of `output/{client}/data/generation_results.json` (nl **and** fr) and the
   `output/{client}/data/generated_issues.json` work list. **This pipeline fails silently — verify
   the copy against the real product, not the "validated N" count.** Generation never publishes; the
   second gate is `plan.json` (step 5), and there is no third — execute writes each page at
   `wordpress.post_status`, `publish` by default, so it is live immediately.

   This step runs in **`links` mode too**, even though no page is written. Not for the copy itself —
   for the plan: with a `generator` configured, `run_plan` omits any **NEW or CHANGED**
   `(GTIN, language)` that has no generated tagline (E21), so a missing or stale results file
   yields an empty plan and the run publishes nothing while reporting success. (An UNCHANGED row
   has no copy by design and keeps its row — it is not a skip and not a work item.)

4. **Plan.** Run `python -m scripts.run_plan {client}` and read
   `output/{client}/plan.json`. run_plan omits any `(GTIN, language)` with a missing
   `product_name` and logs a `SKIPPED …` warning to stderr; for each such warning, present
   the **missing-field prompt (§10.6.5)** verbatim:
   ```
   GTIN 8712345678905 is missing `product_name_fr` (required for language fr).
   [skip-row | ask-me-later | fail-run]
   ```
   - `skip-row` — accept the omission; other languages proceed.
   - `ask-me-later` — batch the prompts, present at end.
   - `fail-run` — abort before execute.
   Default `flow.on_missing_field: prompt`.
   **When run_plan logs no such warning this gate does not happen at all** — do not present it,
   do not mention it, do not ask about it. `lib/gates.py` now enforces that for the
   form-rendering surface (`needs_missing_product_name`), and it is written here so both
   surfaces skip it for the same stated reason rather than by coincidence: a prompt asking
   whether to skip a unit that was never skipped offers a button naming no unit, and of its
   three answers only `fail-run` does anything — so the one live control on a question about
   nothing is the destructive one. That teaches answering a gate without reading it, and the
   cost lands at the gates that matter.

5. **Plan summary (§10.6.1).** Present verbatim (the actionable total is NEW + CHANGED;
   UNCHANGED rows are never executed):
   ```
   Plan for democlient (test env):
     New:       38
     Unchanged:  7
     Changed:    2

   Proceed with all 40 to execute?
   [all | new-only | changed-review | cancel]
   ```
   - `all` — confirm every NEW and CHANGED row; execute.
   - `new-only` — confirm NEW rows only, skip CHANGED.
   - `changed-review` — walk each CHANGED row's diff and confirm individually (step 6).
   - `cancel` — abort, write nothing.
   Off-menu reply → reply verbatim: `Please pick one of the listed options, or specify a
   filter (e.g. 'only GTIN 87123...').`
   When run_plan reported process-list exclusions, add one line beneath the counts, e.g.
   `Excluded: 89 not on the process list.` That number is products in the catalogue the
   operator did not list — it is expected, not a warning.
   `output/{client}/plan.summary.json` carries all of this as data — counts, exclusions, the
   skip tally, the E19 flag and the quarantine path, and the stderr line verbatim under `text`.
   Read it rather than re-deriving any of it, and if the stderr has scrolled away, read it
   instead of re-running run_plan.
   When `plan.json` carries a non-empty **`skipped`** array, add a line beneath the counts
   naming each reason and its count, e.g. `Skipped: 6 no generated copy, 2 missing
   product_name (not in the plan at all).` These are units that never became rows — E18 (no
   `product_name` in that language), E21 (generator on, no generated copy yet) or E22
   (`require_hero_image`, blank source image) — so they are **not** in the totals above and
   `all` will not publish them. Never present the counts without this line when the array is
   non-empty: an operator reading `New: 0` alone concludes there is nothing to do, when in
   fact there is copy to generate.
   When run_plan's stderr leads with the **state-reset warning** (E19 — prior state was
   corrupt and has been reset), put it **above** the counts, not below, and say what it
   means before offering the menu:
   ```
   WARNING: prior state was corrupt and has been reset (backup: output/democlient/state.json.corrupt.20260713T031200Z).
   Every row therefore re-plans as NEW. Re-running them is idempotent — pages are matched by
   slug/meta.gtin and updated in place, not duplicated — but it will rewrite live pages and
   resolver targets rather than skip them.
   ```
   Then present the counts as normal. Do not suppress or soften this: the counts alone read
   as a routine first run, and `all` would rewrite the whole catalogue.

6. **Build the confirmed subset.** From the plan rows and the menu choice, build
   `confirmed_gtins_by_lang`, then intersect it with the step-2 language subset:
   - `all` → every row with classification NEW or CHANGED.
   - `new-only` → NEW rows only.
   - `changed-review` → all NEW rows **plus** each CHANGED row walked via the **per-row
     diff (§10.6.2)**, presented verbatim:
     ```
     GTIN 8712345678905 (nl) — Cable Organiser Pro
     Changes:
       title:      "Cable Organiser" → "Cable Organiser Pro"
       target_url: /democlient/cable-organiser/ → /democlient/cable-organiser-pro/

     [apply | skip | show-full-diff]
     ```
     `show-full-diff` prints all fields, then re-prompts `[apply | skip]`. Confirm only the
     rows the operator `apply`s. Show only the fields present in the row's `diff`; never
     invent an "old" value (see Failure modes).
     A CHANGED row's `diff` carries `title` and/or `target_url` — the fields `StateEntry`
     records. When it is empty, the change is in the product body; say so plainly
     (`Changes: product content (no title or URL change)`) rather than printing a bare
     `Changes:` header.
     A `gs1_link` key means something different from a content change: the page is published
     but its resolver link was never written (a previous `pages` run). Present it as
     `Changes: resolver link not written yet` — nothing about the page is changing.
     **Walk every CHANGED row, not only the ones whose `diff` has fields in it.** State keeps
     the prior `title` and `wp_url` and nothing else, so a row changed in the product body
     has an empty `diff` — on the pilot plan that is 19 of 20 CHANGED rows. A walk keyed on
     the diff presents one row and confirms twenty, which is the defect the operator shell
     shipped with. **Both surfaces do this walk now**: the shell renders every CHANGED row
     with its own `apply`/`skip`, holds the answers for the run, and confirms the applied
     subset at step 7. A row left undecided is not confirmed, on either surface.

7. **Write the ConfirmedPlan.** Serialise `ConfirmedPlan{plan, confirmed_gtins_by_lang}`
   to `output/{client}/plan.confirmed.json`, with `confirmed_gtins_by_lang` as a list of
   `[gtin, language]` pairs (the shape `run_execute --confirmed` consumes).

8. **Environment confirmation (§10.6.7).** In `links` and `both` mode, if the client's resolved
   GS1 environment is `production`, present verbatim and require a choice before executing:
   ```
   About to execute against PRODUCTION environment (gs1nl-api.gs1.nl).
   This will make live changes to https://www.democlient.nl.
   Continue?
   [confirm | switch-to-test | cancel]
   ```
   Mandatory and non-overridable; enforced here per run (not per session). `confirm` →
   proceed; `switch-to-test` → re-resolve to the test environment; `cancel` → abort.
   **Skipped in `pages` mode** — gate 0 already named the environment and nothing irreversible
   follows, so a second production prompt for a page you can delete only trains the operator to
   click through them.

8.5. **Dry run (mandatory).** Before the real invocation, run the *same* command with `--dry-run`
   added and every other flag identical — same `--confirmed` path, same `--only`. It builds no
   clients, writes nothing, and needs no `--i-understand-production`. Show the operator what it
   says it would mutate, then proceed to step 9.

   Numbered 8.5 rather than 9 on purpose: the numbering is load-bearing (see the note above
   step 0), and renumbering would break every cross-reference to "step 9".

   This is the step that catches a plan pointing at the wrong rows, the wrong leg, or the wrong
   URLs — while it still costs nothing. Two things it cannot catch, so do not read a clean dry run
   as more than it is: in `links` mode it does **not** verify that the targets serve (the real run
   does that, and refuses), and it does not prove the ACF fields will land.

9. **Execute.** Invoke
   `python -m scripts.run_execute {client} --confirmed output/{client}/plan.confirmed.json`.
   - **Append `--only pages` or `--only links`** unless the mode is `both`, which is what omitting
     the flag means. The operator never types this — you supply it, on the strength of gate 0.
   - When the resolved environment is `production`, **append `--i-understand-production`** — the
     confirmation that authorises it is step 8 in `links`/`both` mode and gate 0 in `pages` mode.
     Without the flag, `run_execute` refuses a live production run (exit 2), so a production
     execute that omits it will not proceed.
   In `links` mode, `run_execute` verifies every resolver target serves before writing anything,
   and refuses the GTINs whose targets do not. Those come back as errors in step 11 — read them as
   "the page is not where the plan says it is", not as a GS1 fault.

10. **Progress (§10.6.3).** For runs over 20 rows, surface progress every 10 rows;
   otherwise only at the end. Not per-row (per-row detail goes to the JSONL log):
   ```
   Progress: 10/40 rows processed. 10 ok, 0 error, 0 skipped.
   ```

11. **Post-execute summary (§10.6.4).** Read the run JSONL and present verbatim:
    ```
    Run finished for democlient (test env, 2026-05-27T14:32:11Z).
      Ok:       38
      Error:     2
      Skipped:   0

    Errors:
      GTIN 8712345678912 (fr): WP 422 — invalid taxonomy term "outdoor_dier-fr" not found
      GTIN 8712345678919 (nl): image_url returned 404

    Log: output/democlient/runs/20260527T143211Z.jsonl
    QR files: output/democlient/qr/

    Retry the 2 failures? [yes | no | detail]
    ```
    - `yes` — re-run execute filtered to the failed GTINs.
    - `no` — done.
    - `detail` — read the JSONL entries and explain each.

12. **Record observations.** Review your own run (copy, plan, execution, verification) and write
    any qualitative "worth a glance" flags — the same heads-ups you'd give the operator in chat —
    to `output/{client}/data/observations.json` as `{"notes": ["…", "…"]}`, then regenerate the
    report: `python -m scripts.report_quality {client}`. They render in the report's
    **Observations** section, so they persist beyond the chat. This is deliberately *not*
    deterministic — the pipeline's own checks already run; this captures what only your review
    would notice. Write the observations in addition to your chat summary, not instead of it.
    Omit the file (or an empty `notes`) when there is genuinely nothing to flag.

    **Write each note for the person who has to act on it, not for the person who debugged it.**
    This report is read by the operator and forwarded to the client; neither runs the pipeline.
    Every note gives three things:

    - **what a customer or the business would notice** — a page, a scan, a product, in plain words
    - **what it means** — including "nothing is wrong" where that is the finding
    - **what to do about it**, concretely, or **"nothing to do"** when it is a clean record

    Name products the way the operator does — short GTIN plus the product name (`the
    microvezeldoek (…0527)`). Keep out status codes, redirect chains, attribute numbers, request
    verbs and internal edge codes: they belong in the run log. A section reference (`§3b`) is fine
    and is worth **re-checking against the report you just generated** — sections have been
    renumbered before, and a note pointing at a section that moved is worse than no pointer.

    | Instead of | Write |
    |---|---|
    | `…7496 resolved 200 only on a re-check — propagation lag, not failure. Verify with GET, never HEAD.` | **A brand-new product's QR code can take a few seconds to start working.** Right after publishing the onkruidverwijderaar (…7496), scanning it did not reach the page; moments later it did, on its own. **Fix:** if a code fails right after a publish, wait a minute and scan again. |
    | `8-GTIN batch ran 0-error; all 10 live GTINs resolve GET → 307 → 200 with copy present in the HTML.` | **All 10 published products are live and working end to end** — each QR opens its own product page, in the right language, with its description showing. **Nothing to do.** |

## How the work is done

Python, invoked as modules: `scripts/run_plan.py` and `scripts/run_execute.py`, plus
`scripts/parse_export.py` when products are missing and `scripts/run_generate.py` for copy. Those
scripts own the production guard, the `state.json` writes, and the run JSONL — so keep the work on
them rather than driving `lib/` directly, which would reimplement all three in prose.

There are MCP servers in `mcps/`, but they are **not** how anything here works and there is no
`.mcp.json`. They expose a strict subset of the Python clients, and OD-2 keeps them private.

## Failure modes

- **Create-only by default, so no diffs in an ordinary run.** Every candidate row is NEW, so the
  `changed-review` / per-row diff path (§10.6.2) does not fire. It fires under
  `run_plan --include-published`, where a CHANGED row rewrites a live page — and a row whose
  `diff` is empty means the change is in the product body, not the title or URL.
- **No fabricated "old" values.** `StateEntry` records the prior `title` and `wp_url`, so a
  CHANGED row's `diff` can show a real before/after for those two — and only those two.
  `content_hash` proves the rest of the product changed but, being a digest, cannot say how.
  Present only the fields actually in `diff`; never invent an old value. State written before
  titles were persisted has `title: null`, and the title row is then omitted, not guessed.
- **run_plan exits 2** (bad client id, unreadable products/state/control file, missing
  `slug_pattern`/`target_url_pattern`): surface the stderr `config error: …` and stop —
  do not attempt to execute against a missing or malformed plan.
- **Corrupt state is not an exit-2** (E19). run_plan moves the bad file aside, starts fresh,
  and exits 0 with the reset warning on stderr. The plan is valid and safe to execute; what
  changes is its *meaning* — an incremental re-run has become a full rewrite. Surface it per
  step 5 and let the operator decide. Never re-plan silently.
- **Nothing to execute.** If the confirmed subset is empty (e.g. everything excluded by the
  control file, or the operator picked `new-only` with zero NEW rows), report it and skip
  the execute step rather than invoking `run_execute` with an empty plan.
- **Missing process list.** If `process_list` is configured but the file is absent,
  run_plan exits 2 — ask the operator to place it at the configured path before retrying.
- **`links` mode refused a GTIN: "refusing to point a permanent GS1 record at it".** Its target URL
  did not serve. Do **not** work around it — that refusal is the whole reason the mode is safe to
  offer. Find out where the page actually is (the slug may not match `slug_pattern`, or the page may
  be drafted or gone), fix `wordpress.target_url_pattern` or publish the page, and re-run. The other
  GTINs in the batch already went through.
- **`pages` mode leaves rows CHANGED.** Expected, not a bug: a page published without its resolver
  link is not finished, and the plan says so until `/gs1-links` completes it. The row's diff carries
  `gs1_link`, not a content change.
