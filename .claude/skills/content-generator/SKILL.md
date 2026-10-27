---
name: content-generator
description: "Write the product tagline and Eigenschappen text as the in-session producer (no API key) for the (GTIN, language) units this run needs copy for, then validate them for the run. Use when the operator says 'generate content for {client}', 'generate copy for {client}', or 'write product copy for {client}' — the older 'copy' phrasings are kept as triggers so existing habits keep working."
---

# Content Generator

## When to load

Trigger phrases: **"generate content for {client}"** ← preferred, plus **"generate copy for
{client}"** and **"write product copy for {client}"**, kept so existing habits keep working — e.g.
"generate content for democlient". Load this skill to act as the in-session producer: read the
pending generation requests, write the tagline + Eigenschappen copy in the client's brand voice, and
validate the results for the run. No API key — generation happens in this session.

## What this skill does

Fills the handful of product slots that need *writing* — the tagline (`usps[0]`) and the
Eigenschappen bullets (`usps[1:]`) — for the `(GTIN, language)` units this run needs copy for.
It reads `output/{client}/data/generation_requests.json` (written by `run_generate --emit`), produces
per-language copy following the versioned voice template, and writes
`output/{client}/data/generation_results.json`, which `run_generate --validate` then checks and
`run_plan` reads. **That file is the run's copy, not a store**: nothing is kept between runs, so
every unit the next run publishes is written again then, and this producer stays interchangeable
with the headless API backend. The batch is this run's rows — the units that classify NEW or
CHANGED — not every unit in scope: an already-live, unchanged page is not republished, so writing
copy for it would be text nothing reads. Answer the requests file as it stands; it is already
narrowed. Tone is **concise and business-like, not
conversational** — the operator is reviewing copy, not reading prose. Generated content is reviewed
**twice before it can reach a page** — here (the results file) and again in `plan.json`. There is no third
look: execute writes each page at `wordpress.post_status`, which ships as `publish`, so the page is
**live the moment it is written**. `post_status: draft` is the opt-in staged variant, and it is a
config change the operator makes deliberately — see `docs/wordpress-onboarding.md`.

It also **translates**, which is a different job from writing copy and is held to a stricter rule.
A request's `translations` list names values the feed carries in one language and not this one —
the product name, the marketing message, the material. Render each one into this language and
nothing more: **translate the given `source_value`, never write a fresh value or embellish it.**
Every translation is reported in §4 of the data-quality report for the operator to paste back into
MyGS1, so an embellishment there becomes wrong data in the client's GS1 master record, not just on
a web page. A field the feed carries in **no** language is never in this list — that stays a source
gap for the client to fill, and inventing one would be exactly the "never invent product data" rule
this repo is built around.

## Inputs

- `client_id` (from the trigger phrase; ask if unclear).
- Pending requests at `output/{client}/data/generation_requests.json` (run
  `python -m scripts.run_generate {client} --emit` first if absent). It carries `prompt_version` and,
  per unit, `gtin`, `language`, `mode`, `translations`, `input_fingerprint`, `candidates`, and
  `inputs`.
- The voice template: `input/{client}/prompts/generation.{prompt_version}.md` in the data folder,
  or, when that does not exist, `prompts/{client}/generation.{prompt_version}.md` in the program folder.

## Steps

1. **Resolve the client and ensure requests exist.** Determine `client_id` from the request; ask if
   ambiguous. If `output/{client}/data/generation_requests.json` is missing, run
   `python -m scripts.run_generate {client} --emit` first (parse the export via the
   `gs1-export-parser` skill if `output/{client}/data/products.json` is missing too).

2. **Read the requests.** Load `generation_requests.json`. Note `prompt_version`, the unit count, and
   the split by `mode` (`tighten` vs `generate`) and how many units carry `translations`.

   **That file holds only the units in scope** — `run_generate` narrows through the process list and
   the confirmed-video allowlist before computing the units, so its count matches the doctor's
   `generation_results` pending figure. It used to be the whole catalogue: 224 units where 10 were in
   scope, which is copy nobody publishes and a review gate too long to read. If the count looks like
   the size of the export rather than the size of the batch, stop — the process list is probably not
   configured, and generating against it wastes real tokens.

   Present verbatim:
   ```
   democlient: 10 units to generate (3 tighten, 7 generate; 4 with a language gap to translate).
   Generate all, or a subset?
   [all | only-tighten | only GTIN … | cancel]
   ```
   Default `all`. Off-menu reply → reply verbatim: `Please pick one of the listed options, or specify
   a filter (e.g. 'only GTIN 87123...').`

3. **Load the voice.** Read the voice template named under *Inputs* (data folder first) — its few-shot examples
   and rules *are* the voice for this `prompt_version`. If the file for the requested version is
   absent, stop and say so (a version bump needs its voice file); do not fall back to another version.

4. **Generate, per unit.** For each request, produce a ranked `usps` list in the voice:
   - `usps[0]` = the tagline (~30–60 chars); `usps[1:]` = Eigenschappen bullets (each ≤ ~80 chars).
   - **`mode = tighten`:** shorten and rank the request's `candidates`; keep their meaning, invent no
     new claims. **`mode = generate`:** write from `inputs.marketing_message` (1083) using
     `functional_name`/`net_content`/dims/`material` as context; if 1083 is blank, write minimally
     from `functional_name`.
   - **`translations` non-empty:** also return one key per listed field, translated into this
     language. Each entry gives you `field`, `source_language` and `source_value` — **translate
     that text, do not write a new value from scratch and do not elaborate on it.** These go
     back into the client's GS1 datapool, so a rewrite there is a wrong value in their master
     data, not just on the page. Return no key for a field the request did not list; it would
     be dropped on read anyway.
     A `source_value` holding a **comma-separated list** is one the feed repeats across slots —
     `material` is the case, e.g. `"kunststof, metaal"`. Translate each item and return them
     comma-separated in the same order (`"plastique, métal"`): keep the shape, add nothing,
     reorder nothing, and never fold the list into a phrase.
   - Never emit net content, dimensions, or material as USPs (those are added deterministically).
   - **`inferences`:** list any claim you wrote that goes *beyond* the literal feed text — e.g.
     "snoerloos" derived from "batterie rechargeable". Plausible-but-derived is allowed; it is not
     allowed to be silent. Each entry becomes a `generation_inference` finding in §2 of the
     data-quality report, which is where a human confirms it holds for the real product. Omit the
     key when everything you wrote is literally in 1083/1067.
   Work in batch; do not narrate each unit.

5. **Write the results.** Write `output/{client}/data/generation_results.json`:
   ```json
   {
     "client_id": "democlient",
     "results": [
       { "gtin": "02987653000473", "language": "fr",
         "usps": ["Retirez facilement les vis abîmées", "Fonctionne sur bois, plastique et verre"],
         "translations": { "product_name": "Extracteur de vis", "material": "plastique" },
         "inferences": ["fonctionne sur le verre"],
         "input_fingerprint": "<echo from the matching request>" }
     ]
   }
   ```
   Echo each unit's `input_fingerprint` from its request (so a feed edit since emit is caught), and
   include `translations` only for units whose request listed gaps — one key per listed field, no
   others. `inferences` is optional and omitted when there are none. `client_id` must equal the
   run's client.

6. **Validate.** Run `python -m scripts.run_generate {client} --validate`. Surface its stderr line
   verbatim, e.g. `validated 8 result(s), rejected 2; 28/30 unit(s) to publish have copy;
   2 without`. Those totals are the units **this run publishes** — the in-scope NEW and CHANGED
   rows — not the catalogue and not every unit in scope. A `surplus` count is not a rejection: it
   is copy the run does not need, which is ordinary for a file written against an earlier batch. It writes nothing — it checks that what was
   just written answers this run. A non-zero exit is a config error — stop and show it (step:
   Failure modes).

7. **Review (gate #1 of 2).** Present a representative sample — a few NL and FR blocks, including any
   `tighten` units and any that carried `translations` — and the coverage counts. Point to
   `output/{client}/data/generation_results.json` for the full copy and
   `output/{client}/data/generated_issues.json` for the reported values. Then:
   ```
   Generated content is written for this run (reviewed once here). run_plan is the second review before publish.
   [looks good — continue to run_plan | regenerate GTIN … | cancel]
   ```
   - `looks good` — done; the operator proceeds to the `flow-orchestrator` skill / `run_plan`.
   - `regenerate GTIN …` — redo those units (edit their results, re-run `--validate`).
   - `cancel` — stop; the results file keeps whatever was written so far (nothing is published).
   Off-menu reply → the same canned reply as step 2. Never offer to publish from here.

## How the work is done

Python. This skill drives `scripts/run_generate.py` (`--emit` / `--validate`) and reads/writes
the `output/{client}/data/` JSON artifacts. The copy itself is written by Claude in-session, so no
API key is involved; `lib/llm.py` is the alternative headless producer. No MCP server is involved
and there is no `.mcp.json`.

## Failure modes

- **Requests file missing.** `generation_requests.json` is absent — run
  `python -m scripts.run_generate {client} --emit` first; do not hand-write requests.
- **`--validate` exits 2.** A config error (unknown client, unreadable products, or a results file
  whose `client_id` differs from the run): surface the stderr `config error: …` and stop. A
  *missing* results file is not an error — it reports `0/N units have copy` and you have not
  written it yet.
- **Fingerprint mismatch → stale rejection.** `--validate` warns and rejects a result whose
  `input_fingerprint` no longer matches the pending request (the feed changed since `--emit`).
  `run_plan` drops the same result for the same reason, so re-emit and rewrite those units rather
  than forcing the old copy.
- **No pending unit → ignored.** A result for a `(gtin, language)` that is not pending is ignored
  with a warning — expected, not an error. Two causes and the warning names which: **not in scope
  for this run** (the process list was pruned between `--emit` and `--validate`), or **the feed
  supplies this unit's copy verbatim** (a short attr 1067, published as-is and never requested).
- **Blank marketing message.** A `generate` unit whose 1083 is empty still gets copy written from
  `functional_name` + context, and the gap is reported as `missing_generation_input` in
  `generated_issues.json` — surface it so the operator fixes 1083 in MyGS1.
- **This pipeline fails silently.** A green `--validate` only means the JSON validated and matched
  this run's units. Eyeball the actual NL and FR blocks in `generation_results.json` against the
  real product before continuing — never trust the "validated N" count alone. Never put specs into
  `usps`; never publish from this skill.
