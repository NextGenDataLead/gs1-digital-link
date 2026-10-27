# Troubleshooting

Every error this tool raises, plus the traps that have already cost real debugging time on a live
pilot. If you are mid-incident, start with [Traps that have actually bitten](#traps-that-have-actually-bitten)
— the failure is more likely there than in the reference tables.

## Before you debug: run the doctor

```bash
python -m scripts.doctor             # or --offline, for the checks that need no network
```

One line per check, a remedy under each failure, exit `1` if anything failed. It catches the
config errors, missing secrets, stale generated copy and empty-scope conditions described below
*before* a run, which is the only time they are cheap. `--json` emits the same results for a
caller to parse.

It never writes anything and never reads `state.json` — an idle read of a corrupt one
quarantines it (E19), and a diagnostic must not change what the next run does.

## How to read a failure

**Exit codes** are uniform across the scripts:

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | The work itself had errors. Some rows may have succeeded — the run does **not** abort on a single row failure. |
| `2` | Configuration, credential, or usage error at startup. Nothing was attempted. Also the **refused production run**. |

**Where to look:**

| What | Path |
|---|---|
| Per-row outcome of every mutating run | `output/{client_id}/runs/{ts}.jsonl` — one `RunOutcome` per row, written whether the row succeeded or failed, and appended **as each row completes**, so a run that died part-way still accounts for what it did. `run_execute` prints the path at the start of the run as well as at the end; `tail -f` it to watch a run in progress. A failed row carries `failed_call` (which request broke — method, path and label) and an `error` that quotes what the server actually said, so a failure is diagnosable from the file alone rather than from a console nobody kept |
| Why a plan came out the way it did | `output/{client_id}/plan.summary.json` — the gate exclusions, the tally of units dropped before classification, the E19 reset flag and where the corrupt file went, plus `run_plan`'s summary line verbatim. Written on every run, so a *missing* file means `run_plan` never ran |
| What the tool believes is already published | `output/{client_id}/state.json` |
| Whether that belief is **true** | `python -m scripts.reconcile` — lists every page on the site carrying a `meta.gtin`, per language, and diffs it against `state.json` both ways. Read-only. A run that fails part-way creates this divergence itself: the page is written, the row is logged as an error, and nothing is recorded |
| Source-data problems | `output/{client_id}/data-quality-report.md` (`python -m scripts.report_quality`) |
| Which video belongs to which product | `output/{client_id}/video-map-candidates.xlsx` (`python -m scripts.report_video_candidates`) — every file on disk and every mapping row, with ranked candidates and the field each score came from. The gap *count* is `build_video_map --check`; this is the sheet the client fills in |
| What was actually published, per wave | `docs/clients/{client_id}-live-log.md`, where a client keeps one |

**Every exception** derives from `OrchestratorError` (`lib/errors.py`), so `except OrchestratorError`
catches anything this tool raises. Secrets never reach the logs:
`lib.logging_setup.scrub_response_body` redacts `password`, `secret`, `token`, `key`,
`authorization`, and the whole `meta.*` subtree of WordPress responses.

---

## Traps that have actually bitten

These are not hypothetical. Each one was diagnosed the hard way during the pilot.

### The resolver 404s to HEAD but 307s to GET

Testing Digital Link resolution with `curl -I` or any HEAD request returns **404** even when the
entry is live and correct. Always test with **GET**:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://id.gs1.org/01/{gtin14}       # -> 307
curl -sS -o /dev/null -w '%{http_code}\n' -L https://id.gs1.org/01/{gtin14}    # -> 200 at the page
```

On Windows, PowerShell's `curl` is an alias for something else; use `curl.exe` with `-o NUL`
instead of `/dev/null`, or simply open the address in a browser, which always sends GET.

A HEAD 404 is **not** evidence of a broken record. Re-test with GET before touching anything.

### A scan opens the wrong language, or the run warns "scan check: … do not land on their own page"

After every real `links` or `both` run, `run_execute` scans each record it wrote once per language
and compares the landing with that language's page. The warning lists each miss:

```
WARNING scan check: 1 of 2 scan(s) do not land on their own page (GS1 may still be updating —
re-scan in a few minutes; details in output/{client}/runs/{stamp}/scan-check.json):
  02987653000961 (fr): expected https://…/fr/…/p-02987653000961/, scan went to https://…/p-02987653000961/
```

**It is a warning, not an error.** The record was written and its pages serve; the exit code is
unchanged and there is nothing to retry. In order of likelihood:

1. **GS1 had not caught up.** The public resolver serves the previous record for about half a
   minute after a write, and the check gives up after two. Scan again a few minutes later — see
   [`verifying-live.md` § Checking each language](verifying-live.md#checking-each-language).
2. **The record has no per-language default.** Read it with
   `?linkType=all` and `Accept: application/linkset+json`. If it carries `defaultLink` but no
   `defaultLinkMulti`, every phone goes to the default (Dutch) page. A plain `links` re-run
   of the product makes GS1 add `defaultLinkMulti`. A row the plan calls UNCHANGED can still be
   confirmed for that — `run_execute` writes any confirmed row.
3. **The language has no page.** A product linked to an existing page whose site advertises no
   French version gets the Dutch link only, so a French phone opening Dutch is correct. The check
   only scans languages the record has a link for, so this one never warns.

If a re-run still lands wrong, the resolver is choosing differently from what the record says —
that is GS1's behaviour to raise with GS1 NL, not something to change in this tool.

### A page went live without its video, and the run warned about the file

A video that cannot be prepared never stops the page: it publishes without that video, the run
warns, and `runs/{stamp}/data-quality.md` lists it. The warning says which of three things it was:

| Warning | Means | What to do | Retried? |
|---|---|---|---|
| `video file not found at input/…/NL/x.mpg (looked from the folder …)` | The mapping names the file, but it is not in that language's folder **in the data folder** | Move it there — next to `mapping.yml`, under `input/{client}/videos/NL` or `FR`, not in the program folder | **Yes**, on the next plan once the file is there |
| `ffmpeg not found (ffmpeg)` | ffmpeg is not installed, or this session cannot see it | Install it (`install.bat` offers it), sign out of Windows and back in, restart the app | **Yes**, while the file is in its folder |
| `ffmpeg could not convert …: <ffmpeg's last line>` | ffmpeg read the file and refused it | Replace the file, under a different name in the mapping | **No** — it would fail the same way on every run |

"Retried" means the next plan classifies that page **CHANGED** with `video: none → x.mpg`, so a
`pages` run puts the video on. Before 0.4.2 every failure counted as the last kind: the plan called
the page UNCHANGED and there was nothing to confirm. Failures recorded before then are retried once their file is in
place.

An ffmpeg exit code of `4294967294` (Windows) or `254` is −2, *No such file or directory*: the
input was not there. The run now checks the file itself before asking ffmpeg.

### A French page reads as having no text

Reading a page's custom fields with `?lang=fr` (or any `lang=`) on this WPML site returns
`acf: []` — every field empty — for pages that are in fact complete. Read them by post id with **no**
language parameter: the id already names the language version.

### Rehearsing a screen changed the real client's files

Uploading through the Data screen **replaces** the files in the client's `input/` folder in place.
Driving it — by hand or with Playwright — against the real client to try something out overwrites
that client's real product list. Rehearse against the demo client instead:
`python -m scripts.make_demo_export` writes a synthetic export, list and videos for `democlient`,
in a scratch data folder (`GS1_DATA_DIR`), and refuses to overwrite anything without `--force`.

### `400 21011 "No valid contract found."`

The GS1 account has no **Digital Link contract**. This is a GS1-side provisioning matter, not a bug
and not something a code change can fix — the account needs the contract added. The Data Source
contract that issued your GTINs is a *different* contract. Note the asymmetry: on a **GET** this same
400 is interpreted as "no entry exists" and `get()` returns `None`; on a **write** it is fatal.

### An unquoted WordPress application password loads empty

WordPress issues application passwords as six space-separated 4-character groups. In `.env`:

```bash
DEMOCLIENT_WP_APP_PASS='abcd EFGH ijkl MNOP qrst UVWX'   # correct — single-quoted
DEMOCLIENT_WP_APP_PASS=abcd EFGH ijkl MNOP qrst UVWX     # BROKEN — silently empty
```

Unquoted, `source .env` stops at the first space and the variable loads as `abcd`. The symptom is a
confusing `401` even though the password is right. `python-dotenv` — which the scripts use — parses the
unquoted form correctly, so this bites only when you source `.env` by hand, as the staging tests
require. Keep the quotes and both paths work.

### `MissingCredentialError` when you expected the credentials to be there

Credentials resolve **lazily, at the first API call**, so this can fire after parse, plan and a
clean dry-run have all passed. `python -m scripts.doctor` resolves them eagerly instead, which is
the whole reason it exists — run it first.

`python -m scripts.<name>` loads `.env` for you — `load_env()` in `lib/env.py`, called from each
script's `if __name__ == "__main__":` block. So if a credential is missing, work through this order:

1. **Is the variable in `.env`, at the repository root?** That file is the single source of truth. `lib/env.py` resolves it relative to the repo, not your working directory, so *where* you
   run from does not matter.
2. **Does the name match `clients.yml` exactly?** `clients.yml` stores env var **names**, not values;
   a typo in either place produces this error with everything apparently present.
3. **Is the value non-empty?** A bare `NAME=` in `.env` sets the variable to an empty string, which
   reads as present but fails at the API. Watch for the unquoted-password trap above.
4. **Is something already exporting that name?** `load_env()` uses `override=False`, so a variable
   already in your environment wins over `.env` — deliberately, so CI and one-off overrides work. An
   empty or stale export therefore beats a correct `.env`. `unset NAME` and retry.

**Two places `.env` is *not* loaded**, by design:

- **The test suite.** `load_env()` sits in the `__main__` block rather than in `main()`, and the tests
  call `main()` directly. `.env` holds all four variables the staging guards gate on, so loading it in
  the test path would arm tests that write to live WordPress and GS1 production. To run those
  deliberately: `set -a; source .env; set +a && pytest -m staging`.
- **`lib/` at import time.** A library must not have import side effects.

If you are sourcing manually, note that **environment variables do not survive between separate Claude
Code tool calls** — sourcing in one call and running in the next loses them silently. Keep both in one
command with `&&`.

### Checking a WordPress credential without publishing anything

Answers "is this password actually good?" with a read-only request, so you never have to find out
mid-publish. Use it after editing `.env`, after a rotation, or whenever a `401` is confusing you:

```bash
python - <<'PY'
import os, httpx
os.environ.pop("DEMOCLIENT_WP_APP_PASS", None)   # ignore any stale exported value
from lib.env import load_env; load_env()        # read .env only
pw = os.environ["DEMOCLIENT_WP_APP_PASS"]
print("groups:", len(pw.split()))               # expect 6
r = httpx.get("https://www.democlient.nl/wp-json/wp/v2/users/me?context=edit",
              auth=("gs1-publisher", pw), timeout=20, follow_redirects=True)
print("HTTP", r.status_code)
d = r.json() if r.status_code == 200 else {}
print("user:", d.get("slug"), "| roles:", d.get("roles"))
PY
```

Expect `HTTP 200`, the bot's slug, and `roles: ['editor']`. Substitute the site URL, username and
variable name from your client's `wordpress` block in `clients.yml`. It never prints the password.

Three details carry the weight:

- **`?context=edit` rather than a bare `users/me`.** The bare route returns `200` for any credential
  that authenticates, and omits `roles` entirely. `context=edit` additionally requires edit
  capability — so it distinguishes "the password works" from "the password works *and* the account
  can still publish". A demoted or capability-stripped bot passes the naive check and then fails
  mid-run.
- **`os.environ.pop(...)` before `load_env()`.** `load_env()` uses `override=False`, so anything
  already exported beats `.env`. Without the pop you may be testing a stale value rather than the
  file you just edited — and getting a pass or a fail that says nothing about `.env`.
- **`len(pw.split())`.** A WordPress application password is six groups. Anything less usually means
  the value lost its quotes in `.env` and was truncated at the first space (see the trap above).

For GS1 there is no equivalent read-only probe — the cheapest check is minting a token, which
`docs/gs1-nl-onboarding.md` covers.

### Rotating the WordPress application password

WP Admin → **Users** → the bot user → **Application Passwords**. Order matters:

1. **Add the new password first, leaving the old one live.** WordPress shows the value once, as six
   space-separated groups. No downtime, and no way to lock yourself out halfway.
2. **Update `.env`** — single-quoted, on one line. It is the only file to change; `clients.yml` holds
   the variable *name*, never the value.
3. **Verify with the snippet above**, which reads `.env` and ignores stale exports.
4. **Only then revoke the old password.** Re-running the snippet against the old value should give
   `401` — worth doing, since a revocation that did not take is invisible otherwise.
5. If any copy of the old value exists outside `.env` — a backup, a settings file, a password
   manager entry — delete or update it now.

**The trap:** a shell (or a Claude Code session) started before the rotation still holds the old
value in its environment, and `override=False` means it **wins over your corrected `.env`**. The
symptom is a `401` with a `.env` you have just checked by eye. Start a fresh session, or `unset` the
variable.

### A real production run is refused with exit 2

```
run_execute: refused — gs1.environment is 'production' and --i-understand-production was not passed
```

Working as designed. A live run against a `production` GS1 client requires
`--i-understand-production`, so that a bare `--plan` cannot publish live pages and mint permanent
GS1 records. Either pass the flag deliberately, add `--dry-run`, or switch the client to `test`.
`flow-orchestrator` appends the flag itself, but only after its environment-confirmation gate.

### A links-only run refuses a GTIN: "refusing to point a permanent GS1 record at it"

```
gtin 02987653007359 failed its per-product writes: RuntimeError('target URL for language nl
does not serve: https://www.democlient.nl/democlient/p-02987653007359/ (...) — refusing to
point a permanent GS1 record at it')
```

**Working as designed, and the one refusal you must not route around.** `/gs1-links` (i.e.
`run_execute --only links`) aims resolver records at pages it did not create, so it HEADs every
target before writing. A GS1 record **cannot be deleted** — a QR printed against a wrong URL is
permanent — so a target that does not serve stops that GTIN. The rest of the batch still publishes,
and the run exits 1.

The message means the page is not where the plan thinks it is. In order of likelihood:

1. **The slug does not match `wordpress.slug_pattern`.** Pre-existing pages rarely do. The target
   then falls back to `wordpress.target_url_pattern`, which builds a URL nothing lives at. Fix the
   pattern to match the real site, re-run `run_plan`, then re-run.
2. **The page is drafted or in the trash.** `verify_url` is unauthenticated, so a draft 404s.
3. **The URL is right but the site 405s on HEAD.** Rare; check with
   `curl -sS -o /dev/null -w '%{http_code}' -I {url}` against `-L` on a GET.

Check what the run actually tried: the resolution order is `state.json` → a slug lookup on the site
→ the plan row's `target_url`, and the run logs a warning naming which one it fell back to.

### A `pages` run leaves every row CHANGED

Expected. `run_execute --only pages` stores an **empty** `gs1_link_set_hash`, which means "page
published, resolver link never written". `lib/state.py` reports such a row CHANGED with a
`gs1_link` diff, so `/gs1-links` still has something to plan.

Without it the row's content hash would match, the next plan would say UNCHANGED, and a follow-up
`/gs1-links` would find nothing to publish while reporting success — the product would sit live on
the site with no QR resolving to it, and nothing would say so. Run `/gs1-links` to finish the
publish; the rows go UNCHANGED after that.

### A `pages` run left the plan empty instead of CHANGED

**Fixed — this is here for builds predating the fix, and for the diagnostic.**

Symptom: `/gs1-pages` succeeds, the pages go live, and the next `run_plan` reports
`0 new, 0 unchanged, 0 changed` with the GTIN counted under `pilot-excluded (… already have a page)`.
`/gs1-links` then has nothing to publish, so the product sits live with no QR resolving to it.

Cause: `_pilot_gate` in `scripts/run_plan.py` treated **any** state entry as finished pilot work. A
`--only pages` run writes an entry whose `gs1_link_set_hash` is empty — the marker that should
produce a CHANGED row (above) — but the gate runs *before* classification, so `_classify` never saw
the row. Only reachable with `media.restrict_to_mapped_gtins` on.

The gate now counts a GTIN as finished only when **every** language has a non-empty
`gs1_link_set_hash`. If you see this symptom, check that your `run_plan.py` has that condition.

The general lesson generalises past this one bug: **a filter that runs before classification can
hide rows that classification would have surfaced.** Both halves passed their own unit tests; only
the interaction failed. See [`verifying-live.md`](verifying-live.md) for the live check that caught it.

### The plan is empty and nothing says why

Check `skipped` in `output/{client_id}/plan.json`, and the `; N skipped (…)` clause on `run_plan`'s
summary line. Three checks drop a `(GTIN, language)` **before** it is ever classified, so it lands in
neither `rows` nor `counts` and `total` — which is `len(rows)` — does not see it either:

| Reason | Edge | What to do |
|---|---|---|
| `no_generated_copy` | E21 | Generate the copy (`generate content for {client}`), or fix the blank source field it came from |
| `missing_product_name` | E18 | Fill `product_name` for that language in MyGS1 |
| `blank_hero_image` | E22 | Fill `image_url` in MyGS1, or turn `media.require_hero_image` off |

Each drop also logs one `WARNING SKIPPED …` line naming the same reason, but a real run logs at
`WARNING` and the operator may never see it — the plan document is the durable record. A plan of
`0 new, 0 unchanged, 0 changed` with a non-empty `skipped` array means there **is** work; it means
the work is upstream. A plan with an *empty* `skipped` array genuinely has nothing to do.

### A page returns 200 but shows no content

**The ACF write path fails silently.** A `200` from WordPress means the post exists — it does not
mean your field values landed. Always fetch and inspect the rendered HTML:

```bash
curl -sS https://{site}/{slug}/ | grep -o 'your-expected-copy'
```

Common causes: an `acf_map` field name that does not exist on the site; ACF fields not exposed to
REST; or `media.image_write_shape` set to `url` where the theme expects an attachment **id**.

### Images write as an attachment id, not a URL

The ACF image field expects the **attachment id**. Writing a URL produces a page that renders
without its image and reports no error. This is what `media.image_write_shape: id` (the default)
exists for.

### Media re-runs create duplicates unless the slug is content-addressed

Media dedup cannot rely on a `content_sha256` meta key: WordPress **silently drops unregistered meta
on attachments**, so the marker vanished and every run re-uploaded. Worse, stale attachments squatted
the base slug. The fix in place is a **content-addressed slug**, `{base}-{sha12}` — dedup becomes a
pure slug lookup that needs no meta and cannot be squatted. Two consecutive runs now reuse the same
attachments. If you see media duplicating, check that this slug scheme is intact rather than adding a
meta key back.

### The state file reset itself and every row became NEW

See **E19** below. This is recoverable and safe by design, but it silently converts an incremental
re-run into a full rewrite of live pages. `run_plan` leads its summary with a warning and
`flow-orchestrator` surfaces it above the counts — **do not confirm past that warning** without
understanding why the reset happened.

### Two `run_execute` runs at once

Not supported. There is no lockfile in v0.1 (**E20**). Concurrent runs for the same client will
interleave state writes and lose updates. Run them one at a time.

The one thing that *is* handled is the log filename: it is a timestamp to the second, so two runs
started inside the same second would have shared one file and interleaved their rows. The second
one gets `{ts}-1.jsonl` instead (created with an exclusive open, so the two processes cannot both
win). That keeps each run's own account readable — it does not make the runs safe to overlap.

### A GS1 record can never be deleted

The v2 API has **no DELETE**. `run_unpublish` / `retract` deactivates the entry (`isEnabled` →
`false`) and deliberately leaves its links intact so a later reactivation does not have to re-enter
the whole configuration. The deactivated record stays on the account **permanently**. This is why a
smoke-test GTIN must never be a real product's GTIN.

### A page you drafted republished itself

Drafting a page does not remove it from the plan. Use `run_unpublish`, which classifies the GTIN as
HELD so it cannot be picked up again; a manually drafted page will be re-published by the next run.
`run_execute --revive` is the deliberate opt-in to publish GTINs that `run_unpublish` took down.

---

## Exception reference

All thirteen classes in `lib/errors.py`. (`IMPLEMENTATION_SPEC.md` §4.1 names the original eight;
the five added since are marked ✚.)

### `OrchestratorError`

Base class. Catch this to handle any tool-originated failure.

### `ConfigError`

Configuration missing, malformed, or internally inconsistent. Exit 2.

Common causes: `clients.yml` fails `schema/clients.schema.json` validation; an unknown `client_id`;
`multilingual_plugin: wpml` without `wpml_helper_path` and `default_language`; a WPML source language
absent from the linked set; **GS1 rejecting your credentials** (a 4xx from the token endpoint raises
`ConfigError`, not `GS1APIError` — it is a configuration fault, not an API outage).

**Fix:** validate the config first — `python -c "from lib.config import load_clients;
load_clients('clients.yml')"`.

### `MissingCredentialError`

An environment variable named in `clients.yml` is unset. Resolution is **lazy**, so this surfaces at
the first API call, not at startup (**E15**).

**Fix:** see [the section above](#missingcredentialerror-when-you-expected-the-credentials-to-be-there)
for the checklist. In short: the value belongs in `.env`, which scripts load automatically; remember
`clients.yml` holds env var *names*, never values.

### `ExportParseError`

A row could not be parsed into a `ProductRecord`. `parse_export` writes **nothing** and exits 1 —
partial output is never produced.

Common causes: a GTIN row with no `product_name` in the default language (**E5**); a required column
missing (**E17**); a `column_map` target that is not a `ProductRecord` field (**E6**, raised at config
load).

**Fix:** `python -m scripts.parse_export --dry-run` and iterate until clean. Required
fields are `brand` and `product_name`.

### `GS1APIError`

A Digital Link API call returned a non-success response after retries.

Attributes: `status_code`, `response_body` (raw, unscrubbed, for inspection), `error_results`,
`request_id`. `status_code == 0` means a transport failure below HTTP, not a server response.

`error_results` holds the parsed v2 `ErrorResult[]` payload
(`[{"identifier": ..., "errors": [{"code": ..., "message": ...}]}]`) when the 400 body follows that
shape, and is `None` otherwise — e.g. a 5xx with a plain-text body. **Check both fields:**
`error_results` for programmatic handling, `response_body` as the fallback. Include `request_id` in
any report to GS1.

### `WordPressAPIError`

A WordPress REST call returned non-success. Attributes: `status_code`, `response_body`.

- `401` — terminal, never retried. Check the app password quoting trap above, and that the user still
  has the role.
- `409` — slug collision with an existing non-GTIN page (**E11**). Needs human intervention; the tool
  will not guess.
- Also raised when the **WPML helper reports back a translation group different from the one
  requested** — that is the silent-no-op failure mode this integration is most prone to, surfaced as
  a `409` rather than left as a page that looks published but is unreachable in its language.

### `OverwriteError`

`gs1_dl_client.safe_upsert` found an existing Digital Link for the GTIN and `overwrite=True` was not
passed — the GET-before-write guard against clobbering a live resolver target. Attributes: `gtin`,
`existing` (the snapshot that would have been replaced, usable for rollback).

**Fix:** confirm you intend to replace the live target, then pass `overwrite=True`.

### `GtinMismatchError` ✚

A WordPress page exists at the target slug or id but its `meta.gtin` belongs to a **different**
product (**E8**). Attributes: `gtin`, `existing_gtin`, `wp_page_id`.

Deliberately distinct from `WordPressAPIError`: the row is logged and **skipped**, not treated as a
transport failure. Usually means a slug-pattern collision between two products.

### `TemplateError`

A template could not be resolved or rendered — missing file, or none of the override/client/default
candidates exist. See [`template-variables.md`](template-variables.md).

### `StateError`

The state file could not be loaded, parsed, or written. Exit 2.

Note the split from **E19**: *corrupt JSON* is recovered from (backed up, fresh state, loud warning).
`StateError` is the *environmental* fault — permissions, I/O — and is fatal.

### `ProcessListError` ✚

The process list (`input/{client_id}/process/selection/selections.xlsx`) is missing, unreadable, has no sheet
carrying the configured GTIN column, or carries the column with **no GTINs under it**. Treated like
`ConfigError` — exit 2 — because it names exactly which products a run may touch.

**Fix:** check `process_list.path`, and that `gtin_column` matches the header label exactly. The
header may sit anywhere (any sheet, any row); the reader scans for it.

**Empty is an error on purpose.** A file that parses to zero GTINs would otherwise produce an empty
plan and a run that reports success having published nothing — so it stops instead.

### Nothing is excluded that you expected to be excluded

The process list has no status columns and **every GTIN in it is processed**. If a product you
consider "already done" is being planned, its row is still in the file — delete it.

This replaced a reader that interpreted "already on website" / "already in GS1" columns by
presence: any non-blank cell meant *true*. That was right only for files marking rows with `X`. A
file saying `no` meant the opposite of the word, silently, and in both directions — a wrong
"on website" emptied the plan and the run reported success having published nothing, while a wrong
"in GS1" made a product eligible and pointed the pipeline at a GTIN with no resolver record.

Preparing the file is now the operator's job precisely because only the operator knows the rule.

### `GeneratorError` ✚

`generation_results.json` is corrupt, unwritable, or names a different client, or a producer result
failed validation (for example empty bullet lists). Like `StateError`, an on-disk artifact a run
publishes from, whose corruption the operator must see.

A *missing* file is not this error — it means no copy has been written for this run yet, which the
doctor reports as `generation_results` and `run_plan` turns into E21 holds.

**Fix:** inspect the file, or delete it and run the generate cycle again — `run_generate --emit`,
write the copy, then `--validate`. Never hand-edit it into a shape that fails the contract.

### `LLMAPIError` ✚

The Anthropic Messages API failed, or returned a 200 whose body lacks the forced `produce_copy` tool
call. Attributes: `status_code` (`0` for transport failure), `response_body`.

Only reachable via `run_generate --backend api`. The in-session producer (`--emit` / `--validate`)
needs no API key and cannot raise this.

---

## HTTP outcomes

Condensed from `IMPLEMENTATION_SPEC.md` §5.1. 429 and 5xx have independent retry budgets with
exponential backoff.

| Layer | Status | Action | Retries |
|---|---|---|---|
| GS1 | 2xx | success | — |
| GS1 | 400 "No valid contract found" on **GET** | treated as not-found, `get()` → `None` | none |
| GS1 | 400 / 401 / 403 otherwise | `GS1APIError` | none |
| GS1 | 404 on GET | not-found → `None` | none |
| GS1 | 404 on POST, 409 | `GS1APIError` | none |
| GS1 | 429 | back off | up to 5 |
| GS1 | 5xx, timeout | exponential retry | up to 3 |
| WordPress | 2xx | success | — |
| WordPress | 400 / 401 / 403 | `WordPressAPIError`, **terminal** | none |
| WordPress | 404 on GET lookups | `None` | none |
| WordPress | 409 slug conflict | `WordPressAPIError` — needs human | none |
| WordPress | 429 | back off | up to 5 |
| WordPress | 5xx | exponential retry | up to 3 |
| WP verify URL | anything but 2xx/3xx | `WordPressAPIError` | none |

**Run-level policy (§5.3):** per-row failures are recorded as `RunOutcome(status="error")` and the
loop continues. Exit 0 if every row succeeded, 1 if any errored; state is saved with partial results.
Startup config/credential errors abort immediately with exit 2.

---

## Edge case inventory

`IMPLEMENTATION_SPEC.md` §7, with the handling location. Everything here is intended behaviour.

| # | Condition | Behaviour |
|---|---|---|
| E1 | GTIN with leading zeros | Preserved, never silently stripped |
| E2 | GTIN as an integer (openpyxl cast) | Coerced to string, zero-padded |
| E3 | Duplicate GTINs in the export | First wins; the rest WARN and are skipped |
| E4 | Empty Excel row | Skipped silently |
| E5 | GTIN with no `product_name` in the default language | `ExportParseError` naming the GTIN |
| E6 | `column_map` target that is not a `ProductRecord` field | `ExportParseError` at config load |
| E7 | `image_url` 404s or times out | Featured media skipped; **page still created**; noted on the `RunOutcome` |
| E7b | Upload succeeds but WordPress stored fewer bytes than were sent | `MediaIntegrityError` — the attachment is deleted and the **row fails**. Not E7: that is media which was never available, this is media which is *wrong*, and a half-uploaded video the site serves happily is worse than a failed one. Deleted before the call that would claim the content-addressed slug, or the fragment would be adopted by every later run |
| E8 | Page's `meta.gtin` ≠ row's GTIN | `GtinMismatchError` — log ERROR, skip the row |
| E8b | A media attachment to be deleted carries no `meta.content_sha256` | `MediaOwnershipError` — nothing is deleted. The media sibling of E8: an attachment id is just a number, and the content hash this tool stamps on its own uploads is what distinguishes them from the client's library. Unreadable meta counts as "not ours" — declining to delete an orphan of ours is recoverable, deleting a client's product photo is not |
| E9 | GS1 upsert succeeds, WP later 500s | GS1 state kept; WP failure logged; run continues |
| E10 | NL succeeds, FR fails | State reflects NL; FR retried next run |
| E11 | Slug collision with a non-GTIN page | `WordPressAPIError` — human intervention |
| E12 | Template uses `{{extras.foo}}`, `foo` absent | Renders empty; WARN once per run |
| E13 | Product data contains `{{` or `}}` | Escaped at insertion. **Never** use triple-brace |
| E14 | GS1 returns 401 mid-run | Row errored; later rows try again |
| E15 | `clients.yml` names an unset env var | `MissingCredentialError` at first API call (lazy) |
| E16 | More columns than `column_map` | WARN per unmapped column |
| E17 | Fewer columns than expected | `ExportParseError` if required, WARN if optional |
| E18 | Language has no `product_name.{lang}` for a GTIN | That unit is dropped before classification, recorded in `plan.json` under `skipped`, and surfaced in chat |
| E19 | State file is corrupt JSON | Backed up to `state.json.corrupt.{ts}`, fresh state, ERROR logged, **and the reset surfaced above the plan counts** |
| E20 | Two `run_execute` runs interleave | **Not supported.** No lockfile in v0.1. Same-second runs do at least get separate log files (`{ts}-1.jsonl`) |
| E21 | Generator on, but a `(GTIN, language)` has no generated tagline | Unit dropped so a blank page can never publish; recorded under `skipped`; gap also reported via `missing_generation_input` (only when 1083 is blank in every configured language — otherwise it is a pending translation, reported as `value_translated` once filled) |
| E22 | `media.require_hero_image` set, source `image_url` blank | GTIN held out of the plan, one `skipped` entry per language; reported via `value_blank`. A runtime fetch failure still degrades per E7 |

### E19 in full — why the reset is safe, and why it must stay loud

State is a **cache** of what the tool believes it already did, derivable from the live systems, so
rebuilding it is safe: every write path is idempotent. Without a known page id, `upsert_page` still
matches the live page by slug then `meta.gtin` and updates in place (no duplicates); `safe_upsert`
reads before it writes; `render_qr` is byte-deterministic. A reset costs redundant work, not
corruption — which is why aborting would be the wrong trade.

But a reset also **reclassifies every row as NEW**, silently turning an incremental re-run into a
full rewrite of live pages and resolver targets. An ERROR in the log is too quiet for that — the
operator is reading the chat, not stderr. So `load_state` sets `State.reset_from_corrupt`, `run_plan`
leads its summary with a warning, and `flow-orchestrator` surfaces it **above** the counts. The
confirmation gate is what makes the reset safe in practice, and it only works if the operator is
told.

---

## Rollback

v0.1.0 implements Levels A and B (§5.4).

**Level A — structured logs + manual rollback.** Every mutating operation writes a `RunOutcome` to
`output/{client_id}/runs/{ts}.jsonl`. WordPress pages revert through the admin UI (revisions are
preserved). GS1 entries revert via MyGS1 or a re-run. QR files are overwritten by re-runs.

**Level B — prevent rather than recover.** `run_plan` produces `plan.json`; the skills show it in
chat before executing; `--dry-run` walks the whole plan replacing every mutating call with a log
line. Plus two GET-before-write guards: `OverwriteError` on the GS1 side, `GtinMismatchError` on the
WordPress side.

**Level C — snapshot and automated rollback — is not implemented.** `state.json` carries
`content_hash` and `gs1_link_set_hash`, enough for change detection but **not** previous-value
preservation. That is the gap.

Practical rollback for one product:

```bash
python -m scripts.run_unpublish --gtin {gtin} --dry-run   # preview
python -m scripts.run_unpublish --gtin {gtin}             # retract + draft pages
```

This retracts the Digital Link and drafts the pages, and classifies the GTIN as HELD so a later run
will not republish it. The GS1 record itself remains on the account, deactivated, forever.
