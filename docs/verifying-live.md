# Verifying the live pipeline

How to prove the publish flows still work against the **real** WordPress site and the **GS1 NL
production** resolver, without degrading a live product and without fooling yourself.

Read [`setup.md`](setup.md) first. This document assumes a working install and a client with at
least one product already published.

> **Why this exists.** Gate testing proves the confirmation prompts render. It does not prove
> anything is written. The first time these flows were verified end-to-end this way, the exercise
> found a real bug that unit tests could not — see [Worked example](#worked-example).

## The problem this solves

You want to answer: *does the pipeline actually still publish?* Three approaches suggest
themselves, and two of them are wrong.

| Approach | Verdict |
|---|---|
| Take a product down (`run_unpublish`), then republish | **Does not work.** The GTIN classifies HELD (`lib/state.py`), and `run_execute` drops HELD rows unless `--revive` — which no skill passes. The run reports "nothing to execute". It also leaves the product with no page and no resolver for the duration. |
| Delete the page by hand | **Does not work.** State still says `publish` with a matching content hash, so the row plans UNCHANGED and nothing happens. Hand-editing state to force it is the E19 trap in miniature. |
| Trust the HTTP 200s | **Proves nothing.** A 200 says the request was accepted. It does not say the remote changed. |

The working approach is to change **only the bookkeeping**, and then prove each write independently.

## Why a state-only reset is safe

The pipeline is idempotent by design, so nothing has to be taken down:

| Layer | Behaviour on re-run | Source |
|---|---|---|
| WordPress page | `upsert_page` looks up by `existing_id` → `slug` → `meta.gtin`. With state removed it falls to slug, finds the live page, and **updates it in place** — same page id | `lib/wp_client.py` |
| Featured media | Deterministic JPEG encode → identical SHA-256 → `upload_media` **reuses the existing attachment** | `lib/media.py` |
| GS1 record | `upsert` rewrites the link set; the record stays **enabled throughout** | `lib/gs1_dl_client.py` |
| Generated content | `pending_requests` queues only *gaps*, so a cached GTIN is **not regenerated** | `scripts/run_generate.py` |

Removing one GTIN's entry from `state.json` makes it plan NEW (`_classify` returns NEW when there is
no prior) and drops it out of `already_present`, which `_pilot_gate` computes from `state.entries`.
Exactly two rows (nl + fr) become actionable; nothing else moves.

This is **not** the E19 blanket reset. E19 is dangerous because *every* row becomes NEW against a
live site. Here one GTIN does.

## The observability problem

A successful run leaves the system looking exactly as it started. So **"it worked" and "it silently
did nothing" are indistinguishable** unless you arrange otherwise. Each leg needs independent,
server-side evidence:

| Leg | Evidence | Notes |
|---|---|---|
| WordPress | **`modified_gmt` advances.** `_write_page` always POSTs — there is no unchanged-skip path | The custom post type is registered **without** `revisions` support, so `/revisions` 404s. Do not rely on it. |
| ACF fields | Compare all field values before/after, and confirm they **render** in fetched HTML | The ACF write path fails silently; a 200 is not enough |
| State reconstruction | After deleting the entry, `wp_page_id` returning correctly proves a live lookup | Proves lookup, not write — pair it with `modified_gmt` |
| **GS1** | **Nothing, by default** | `DigitalLinkRecord` carries no timestamp, version or revision field, and `_finish_links` never reads back — it hashes the links it *sent*. An identical-content upsert is indistinguishable from a no-op. |
| Resolution | Regression check only | 307 → 200 passes before the run too |

**Read WordPress one language at a time, and say which.** On a WPML site a listing returns only the
current language's posts, so a page can be missing from
what you are looking at and live all the same. Verify each `(GTIN, language)` explicitly rather than
counting; see [`wordpress-onboarding.md`](wordpress-onboarding.md#multilingual), silent failure #5.

### Making the GS1 write observable

Perturb the resolution target before the run, and require the run to restore it.

- **Before:** upsert both links' `targetUrl` to a deliberately safe URL — **the client's own
  homepage**.
- **After:** GET the record. Both targets back on the product pages = the write landed. Still on the
  homepage = silent no-op, caught.

**The choice of URL is what makes this safe.** Pointing at the homepage keeps the QR resolving
(307 → 200) to a legitimate page for the whole window: no 404, no dead scan, no wrong-product
confusion. It is *better* than disabling the record, where a scan resolves to nothing. Pointing at
another product's page would be worse than either — don't.

Note this is a hand-written `upsert` **outside `flow-orchestrator`**. A test perturbation is not
publishing, but it is a deliberate manual write to a production record. Snapshot first, and record
it in the client's live log.

## Two gates that prevent false passes

Neither is bookkeeping. Skipping either can produce a **PASS that means nothing**.

**1. Pre-flight content-hash comparison.** `plan.json` carries `content_hash` per row. Compare it to
the snapshot's before authorising anything:

- identical → the page write provably cannot change content. Proceed.
- different → something drifted since publish. **Stop.**

**2. Post-perturbation calibration.** After perturbing, confirm the resolver actually serves the
homepage. If the perturbation silently failed, the record still points at the product page, the run
"restores" a value that never changed, and you record a pass having proven nothing — the exact
failure mode the exercise exists to catch.

**Propagation lag is not failure.** The resolver can trail the API by seconds. Both resolution
checks need a retry window; poll until it flips rather than reading the first response as broken.

## Procedure

Pick a GTIN that is already published and known-good — a known-good subject makes any deviation
signal rather than ambiguity.

1. **Snapshot**, outside the repo tree so `git clean` cannot reach it: `state.json`, the full GS1
   record, both pages' `modified_gmt`, and the current resolver target. Build the restore payload
   now, while the values are known good. *Everything downstream depends on this existing.*
2. **Perturb** both links' `targetUrl` to the client homepage.
3. **Calibrate** — GET the record *and* the resolver; both must show the homepage. Retry for lag.
4. **Remove** that one GTIN's entry from `state.json`.
5. **Plan** → expect `2 new`. **Run the pre-flight hash gate.**
6. **Dry run.**
7. **`/gs1-pages`** through the gates.
8. **Verify WordPress:** `modified_gmt` advanced; ACF values unchanged; fields render in live HTML.
9. **Verify the handoff:** re-plan. Rows must now be **CHANGED** with
   `diff={'gs1_link': ['not written', 'will be written']}` — see
   [`troubleshooting.md`](troubleshooting.md#a-pages-run-leaves-every-row-changed).
   Build the confirmed subset from **this** plan, not an earlier one.
10. **`/gs1-links`** through the gates.
11. **Verify GS1:** both `targetUrl`s restored, off the homepage. Do not accept a 200 as proof.
12. **Verify resolution, once per language:** `GET id.gs1.org/01/{gtin}` → 307 → that
    language's product page → 200, with a retry window. **GET, not HEAD** — the resolver 404s to
    HEAD. The links run already did this and wrote `runs/{stamp}/scan-check.json`; read it, then
    repeat it by hand as below, because a scan check that passes is only as good as its header.
13. **Reconcile:** every `state.json` field byte-identical to the snapshot; other GTINs untouched;
    `run_plan` back to its prior baseline.
14. **Log it** in the client's live log, including the manual perturbation.

### Checking each language

The resolver picks the page from the phone's `Accept-Language`, so "the QR resolves" is a claim per
language, not per GTIN. Ask the way a phone does — the regional form first, then the plain one:

```bash
curl -sS -o /dev/null -w '%{redirect_url}\n' -H 'Accept-Language: nl-BE,nl;q=0.9' https://id.gs1.org/01/{gtin14}
curl -sS -o /dev/null -w '%{redirect_url}\n' -H 'Accept-Language: fr-BE,fr;q=0.9' https://id.gs1.org/01/{gtin14}
```

Each must print that language's own page. To see *why* a scan lands where it does, read the record
the resolver is serving:

```bash
curl -sS -H 'Accept: application/linkset+json' 'https://id.gs1.org/01/{gtin14}?linkType=all'
```

A record that sends each language to its own page carries `https://ref.gs1.org/voc/defaultLinkMulti`
with one entry per language. One that has only `defaultLink` sends **every** phone to that single
page, whatever its language — the French link is stored but a plain scan never reaches it.

Three things that read like failures and are not:

- **A bare regional header goes to the default.** `Accept-Language: fr-BE` alone, with no plain
  `fr`, resolves to the Dutch page even on a correct record. Phones list both; test with both.
- **The record you read may be the previous one.** After a write the public resolver serves the old
  record for about half a minute. Poll until `description` shows the new name before judging.
- **`?linkType=gs1:pip` is not a scan.** It picks by language even on a record without
  `defaultLinkMulti`, so it can pass a record that a phone camera does not. Test the bare URI.

### Rollback

Copy the snapshot `state.json` back and re-upsert the links from the restore payload. Nothing is
mutated destructively at any step.

**Restore both, or neither.** Restoring the GS1 record while leaving a pages-only `state.json`
leaves state claiming a resolver link is still owed when the record is already correct — the
bookkeeping and reality disagree, and the next plan will act on the wrong one.

## What this does not cover

The exercise proves **update-in-place** and **overwrite**. It does not reach:

- the *create* branches — a brand-new page, a first-time GS1 record;
- `--only links` refusing a target that does not serve, since the page serves correctly.

Those need a GTIN dedicated to smoke testing, not a live product.
[`tests/integration/test_run_execute_staging.py`](../tests/integration/test_run_execute_staging.py)
is the harness, and its two guards — company-prefix check plus a pre-flight that refuses if a page
already exists — are there because a real product's GTIN would otherwise let the run adopt its live
page and the teardown delete it, with every ownership check passing.

## Worked example

The first full run of this procedure (2026-08-04, `02987653007717`) found a real bug: `_pilot_gate`
dropped any GTIN with a state entry *before* classification, so a `--only pages` run removed its own
GTIN from every later plan and `/gs1-pages` → `/gs1-links` dead-ended with an empty plan. Both
mechanisms were correct in isolation, which is why the unit suite passed.

Step 9 above is exactly where it surfaced. The full record, including the evidence table, is in
`docs/clients/{client_id}-live-log.md` (local-only, gitignored) under *Verification runs*.
