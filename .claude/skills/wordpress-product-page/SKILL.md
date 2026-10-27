---
name: wordpress-product-page
description: "Render and idempotently upsert a client's WordPress product pages, one per (GTIN, language). Use when the operator says 'create product pages for {client}' or 'update {client} WordPress pages'."
---

# WordPress Product Page

## When to load

Trigger phrases: **"create product pages for {client}"**, **"update {client}'s WordPress pages"**
(§10.2) — e.g. "create product pages for democlient". Load this skill to render and upsert a client's
product pages on their WordPress site, idempotently, one per `(GTIN, language)`. In the pilot this is
one leg of `flow-orchestrator` / `run_execute`; load it directly to create or update pages on their
own.

## What this skill does

Drives `lib/wp_client.py` and `lib/acf.py`. For each `(GTIN, language)`
it renders the page from the client's template, upserts it idempotently (matched by `existing_id`,
then `slug`, then `meta.gtin`), and writes the ACF fields mapped in `clients.yml` `acf_map`. Auth is
HTTP Basic with a WordPress **application password** read from an environment variable. Because the
client's theme renders from ACF, not `post_content`, the ACF fields **are** the page — so ACF is
always written in a **separate** follow-up POST (values silently drop if they ride a `?lang=`
create). Pages are only written after the plan's two review gates; this is the write step, not a
review step. Tone is **concise and business-like, not conversational**.

## Inputs

- `client_id` (from the trigger phrase; ask if unclear). `get_client(client_id)` from
  `lib/config.py` resolves `site_url`, credentials, and post status from `clients.yml`; it is
  optional when exactly one client is defined.
- `clients.yml` `wordpress` config: `post_type`, `multilingual_plugin`, `wpml_helper_path`,
  `languages`, `acf_map`, `slug_pattern`, `target_url_pattern`; and the `app_password_env` var.
- The client's templates at `templates/{client}/product.{lang}.html` (per `template.files`).
- Parsed products at `output/{client}/data/products.json`, with the merged generated content for the
  `acf_map` source fields (tagline / description).

## Steps

1. **Resolve the client.** Determine `client_id` from the request; ask if ambiguous. Confirm which
   `(GTIN, language)` rows are in scope (usually the confirmed plan subset).

2. **Verify the template exists.** Confirm `templates/{client}/product.{lang}.html` is present for
   each language in scope; stop if a language's template is missing.

3. **Detect the multilingual plugin.** `WordPressClient` probes at construction and exposes the
   result as `client.multilingual_plugin` — `polylang`, `wpml`, or `none`. The client's configured
   `multilingual_plugin` overrides the probe (a wrong probe would silently disable translation
   linking); on a mismatch, honour the config and note the discrepancy.

4. **Render and build ACF.** Render the page content, then build the ACF payload with
   `build_acf_payload(product, language, acf_map)` — it reads each mapped source field for **this
   language only** (no cross-language fallback), omitting and warning on any absent value.

5. **Upsert.** Call `upsert_page(post_type, slug, title, content, language, meta={"gtin": gtin},
   featured_media=…, parent=…, existing_id=…, acf=…)`. It is idempotent — matched by `existing_id`
   → `slug` → `meta.gtin`. Callers **must** set `meta["gtin"]` (the idempotency key). Upload
   featured media with `upload_media(path, title=…)` (idempotent by content hash + slug) first if
   needed; `find_by_slug(post_type, slug, language)` looks a page up without writing.

6. **Verify the URL (§10.2).** Call `verify_url(url)` — a HEAD returning 2xx/3xx. It returns `True`
   or **raises** `WordPressAPIError`; it never returns `False`. Treat a pass as "the URL resolves",
   **not** "the page has content" (see Failure modes).

## The Python API

`lib/wp_client.py` `WordPressClient`, constructed from the client's `WordPressConfig` and used as a
context manager:

```python
from lib.config import get_client
from lib.wp_client import WordPressClient

cfg = get_client(client_id)
with WordPressClient(cfg.wordpress) as wp:
    page = wp.upsert_page(post_type=..., slug=..., meta={"gtin": gtin}, acf=acf, ...)
    wp.verify_url(page["link"])
    wp.link_translations({"nl": nl_id, "fr": fr_id})
```

| Method | Does |
|---|---|
| `client.multilingual_plugin` | The detected plugin (`polylang` / `wpml` / `none`), resolved at construction |
| `upsert_page(...)` | Create or update one page idempotently (lookup by `existing_id` / `slug` / `meta.gtin`) |
| `upload_media(path, title=None)` | Upload a media file idempotently by content hash + slug; returns its id |
| `find_by_slug(post_type, slug, language=None)` | Find a page by slug; returns `None` when absent |
| `verify_url(url)` | `True` for 2xx/3xx via HEAD; raises `WordPressAPIError` otherwise |
| `link_translations({lang: page_id})` | Link per-language pages as translations of one another |
| `set_page_status(...)` / `delete_page(...)` | Draft or remove a page, guarded by its GTIN |

`build_acf_payload(product, language, acf_map)` in `lib/acf.py` builds the ACF dict.

There is a `wordpress` MCP server in `mcps/wordpress/`, but it is **not** how this works and there is
no `.mcp.json`. It exposes five of the methods above and stops there: `link_translations` and the
whole take-down path (`set_page_status`, `delete_page`, `delete_media`) have no MCP equivalent, so
neither a multilingual publish nor `scripts/run_unpublish.py` could go through it. OD-2 keeps those
servers private. `scripts/run_execute.py` drives the Python client on the orchestrated path; so
should you.

## Failure modes

- **ACF silently drops on a `?lang=` create.** Inline ACF on a create carrying a language returns
  `201` with the fields empty and no error. ACF is therefore always a **separate** POST to the page
  id (for both creates and updates) — never fold it into the create and assume it stuck.
- **`verify_url` 200 ≠ page has content.** The theme renders from ACF, so `post_content` is empty
  and a `200` says only that the URL resolves. Confirm the ACF-rendered page actually shows the
  copy before trusting the run — the pipeline fails silently here.
- **Wrong plugin silently unlinks translations.** If the probe disagrees with the configured
  `multilingual_plugin`, the config wins on purpose; a wrong probe swapping in the no-op adapter
  would leave translations unlinked with no error. On WPML the site's helper endpoint
  (`wpml_helper_path`) does the linking — verify it exists before a real run.
- **GTIN / slug collisions.** A matched page whose `meta.gtin` differs raises `GtinMismatchError`
  (E8); a non-GTIN slug collision raises `WordPressAPIError` (E11). Both stop the row rather than
  overwrite a foreign page.
- **Missing image → page still created.** A source image that 404s or times out (E7) skips featured
  media but still creates the page; a missing ACF value is omitted and warned, never raised — a
  blank tagline must not block publishing.
- **Auth is terminal.** WordPress `401`/`403` is not retried (429/5xx are). An unset
  `app_password_env` raises `MissingCredentialError`. Never publish to production during validation.
