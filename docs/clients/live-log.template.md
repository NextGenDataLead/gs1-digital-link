# {Client} — live-publish log (template)

Copy this to `docs/clients/{client_id}-live-log.md` and fill it in. That path is **gitignored**
(`.gitignore` → `docs/clients/*-live-log.md`), so your copy stays local; this template is the part
that ships.

> **Why the real log is not committed.** It records the client's real published URLs, WordPress page
> ids and GTINs. Those identifiers must stay accurate for the log to be worth keeping — a log full
> of placeholder addresses records nothing — and this repository is public. So the file is kept
> local and backed up like any other operational record.
>
> **Ignoring is not retraction.** If a live log was committed before being ignored, those versions
> remain reachable in git history. Removing them needs a history rewrite, which does not un-publish
> anything already cloned or cached.

> **Why it exists at all.** `output/{client_id}/state.json` is the machine source of truth, but it is
> local-only and not readable at a glance. This file is its human-readable companion. Keep them in
> sync — `state.json` wins on conflict, because it is what the pipeline actually wrote.
>
> **GS1 is append-only.** GS1 v2 has no delete: `retract` only sets `isEnabled:false` and the record
> persists forever. So a GTIN that ever had a GS1 record stays listed even once disabled.

## Currently live

Pages serving on the live site with an **enabled** GS1 Digital Link record.

| GTIN | Product (nl / fr) | Lang | WP page | URL | GS1 | First published | Wave |
|---|---|---|---|---|---|---|---|
| `0871234567890` | Example / Exemple | nl | 1234 | https://www.example.test/{post_type}/p-0871234567890/ | enabled | 2026-01-01 | 1 (2026-01-01) |
| `0871234567890` | Example / Exemple | fr | 1235 | https://www.example.test/fr/{post_type}/p-0871234567890/ | enabled | 2026-01-01 | 1 (2026-01-01) |

**Live GTIN count (via this pipeline): N.**

Verify any row with a GET (never HEAD — the resolver 404s to HEAD):
`curl -sSL -o /dev/null -w '%{http_code}\n' https://id.gs1.org/01/<gtin>` → expect `200` after the 307.

## Drafts / partially provisioned (not live)

Pages that exist but are **not** serving (draft), and/or GS1 records created then disabled.

_None._

## Not published by this pipeline

Products already live on the site before or outside this pipeline (client-maintained, per the
`process_list` file). They are **not** counted above and have no state entry.

## Verification runs

Deliberate live runs whose purpose was to **prove the pipeline works**, not to publish anything new.
Record them here so the audit trail shows no unexplained writes — see
[`../verifying-live.md`](../verifying-live.md) for the procedure and the evidence each leg needs.

## How to update

After each live wave (`run_execute … --i-understand-production`, or the `flow-orchestrator` execute):

1. Read the new/changed entries in `output/{client_id}/state.json`.
2. Add a row per `(GTIN, language)` to **Currently live** (or **Drafts** if `wp_status != publish`),
   with the WP page id + URL + GS1 status + date + wave label.
3. Bump the live GTIN count.
4. Keep the file with your working copy — it is gitignored, so it is not committed with the wave.
