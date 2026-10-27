---
name: gs1-digital-link
description: "Point a client's GTINs at their product pages in the GS1 NL resolver by building and upserting each Digital Link entry. Use when the operator says 'set resolver targets for {client}' or 'update the Digital Link for {client}'."
---

# GS1 Digital Link

## When to load

Trigger phrases: **"set resolver targets for {client}"**, **"update the Digital Link for {client}"**
(§10.3) — e.g. "set resolver targets for democlient". Load this skill to point a client's GTINs at
their product pages in the GS1 NL resolver: build each Digital Link entry from the `ProductRecord`
plus `clients.yml`, and upsert it with `lib/gs1_dl_client.py`. In the create-only pilot this is
normally driven by the `flow-orchestrator` skill through `run_execute` (`/gs1-links` is the
links-only mode of that flow); load this skill directly when setting or inspecting resolver targets
on their own.

## What this skill does

Drives `lib/gs1_dl_client.py`. For each `(GTIN, language)` it builds the
links array from the client's `gs1_links` config (one `gs1:pip` link per configured language; the NL
link is the `default` standaardlink) and the `target_url` pattern, sets `item_description` to the
product name, and upserts the entry. Auth is OAuth2 client-credentials: the client mints a ~1h JWT
(cached, Bearer) from credentials in environment variables, against the `test`
(`gs1nl-api-acc.gs1.nl`) or `production` (`gs1nl-api.gs1.nl`) host per the client's resolved
environment. Upserts are idempotent — `CreateOrUpdate` replaces the whole links array, so a language
left out of the payload is **deleted** from the resolver. Tone is **concise and business-like, not
conversational**.

## Inputs

- `client_id` (from the trigger phrase; ask if unclear). `get_client(client_id).gs1.resolve()`
  produces the account number, resolver host, and credentials from `clients.yml` plus `.env` — you
  never pass them by hand. It is optional when exactly one client is defined.
- The resolved GS1 environment (`test` | `production`) for the client. A `production` target is
  gated by the environment-confirmation prompt (§10.6.7) when reached via `flow-orchestrator`.
- `clients.yml` `gs1_links` (link types, `default`, `public`, `title_pattern`) and
  `wordpress.target_url_pattern` / `languages`.
- Parsed products at `output/{client}/data/products.json` for the GTINs, names, and target URLs.

## Steps

1. **Resolve the client.** Determine `client_id` from the request; ask if ambiguous. Confirm which
   GTINs are in scope (usually the confirmed plan subset).

2. **Build the payload.** For each GTIN assemble `links[]` from `gs1_links` and the per-language
   `target_url`, one link per configured language (NL carries `default: true`). Set
   `item_description` to the product name and `is_enabled` to `true`. Include every configured
   language — a missing language is removed from the resolver, not left untouched.

3. **Upsert.** Call `safe_upsert(gtin, item_description, links, is_enabled=True, overwrite=...)` —
   it GETs before writing and raises `OverwriteError` rather than clobbering an existing entry
   unless `overwrite=True`. Prefer it to the bare `upsert`. For many GTINs, `upsert_bulk(entries)`
   batches into groups of `batch_size` internally. To inspect the current entry first, `get(gtin)`
   returns `None` when the GTIN has no entry.

4. **Report.** Summarise how many entries were written and any that errored. In the orchestrated
   pilot the resolver write is one leg of `python -m scripts.run_execute {client} --confirmed
   output/{client}/plan.confirmed.json` (same client, `overwrite=True` because the plan is
   operator-confirmed); the post-execute summary (§10.6.4) reports GS1 failures alongside WordPress.

## The Python API

`lib/gs1_dl_client.py` `GS1DigitalLinkClient`, constructed from the resolved GS1 config and used as
a context manager:

```python
from lib.config import get_client
from lib.gs1_dl_client import GS1DigitalLinkClient

cfg = get_client(client_id)
with GS1DigitalLinkClient(cfg.gs1.resolve()) as gs1:
    gs1.safe_upsert(gtin=gtin, item_description=name, links=links, is_enabled=True, overwrite=True)
```

| Method | Does |
|---|---|
| `safe_upsert(...)` | GET-before-write upsert; raises `OverwriteError` instead of clobbering unless `overwrite=True` |
| `upsert(...)` | Create or update one GTIN's record (no guard) |
| `upsert_bulk(entries)` | Many GTINs, batched by `batch_size` |
| `get(gtin)` | The current record, or `None` |
| `set_enabled(gtin, enabled)` | Enable or disable a record |
| `retract(gtin)` | Take a product down: clears the links and disables the record |
| `validate_draft(...)` | Validate a payload without writing |

There is a `gs1-nl` MCP server in `mcps/gs1-nl/`, but it is **not** how this works and there is no
`.mcp.json`. It exposes **three** of the seven methods above — `set_enabled`, `retract` and
`validate_draft` have no MCP equivalent, so `scripts/run_unpublish.py` could not go through it at
all — and OD-2 keeps those servers private. `scripts/run_execute.py` drives the Python client on
the orchestrated path; so should you.

## Failure modes

- **No DELETE in v2.** There is no delete endpoint. `retract` only disables an entry
  (`isEnabled=false`), leaving a permanent-but-inert record; the links stay. Removing a language
  from the payload deletes that link on the next upsert.
- **Credentials rejected.** A 4xx from the token endpoint means the credentials were rejected —
  raised as a `ConfigError`. Check the `client_id`/`client_secret` env vars named in `clients.yml`
  for the resolved environment; an unset var raises `MissingCredentialError`.
- **Wrong account number writes to an account that isn't ours.** The account number must come from
  the minted token's claim, not the GTIN prefix. A `200` proves nothing here — it never does. Use
  the confirmed `account_number_production` in `clients.yml`.
- **Transient API errors are retried.** 429 (honours `Retry-After`, up to 5 attempts) and 5xx /
  network errors (up to 3) back off and retry; a 401 re-mints the token once. A terminal non-2xx
  raises `GS1APIError` with the status, request id, and parsed error results.
- **Production is live.** For a `production` client the environment-confirmation gate (§10.6.7) is
  mandatory and non-overridable — never bypass it. Do not upsert to production during validation.
