# WordPress onboarding

What a WordPress site needs before the tool can publish product pages to it, and the four silent
failure modes to guard against.

Derived from `lib/wp_client.py`, `lib/multilingual.py`, `lib/acf.py`, `lib/media.py`,
`lib/media_video.py`, and `lib/config.py`. Where this contradicts older planning documents — in
particular anything recommending Polylang for the pilot — this file and the code are correct.

## Checklist

| # | Requirement | Check |
|---|---|---|
| 1 | WordPress 5.6+ | Application passwords landed in 5.6. |
| 2 | REST API reachable over **HTTPS** | `curl -sS https://{site}/wp-json/wp/v2/types` returns JSON. |
| 3 | No security plugin blocking REST or Basic auth | The most common cause of an inexplicable `401`. |
| 4 | An automation user with an editor-or-better role | Do not reuse a human's account. |
| 5 | An **application password** for that user | Six space-separated groups. See the quoting trap below. |
| 6 | Target post type registered with `show_in_rest: true` | Without it the post type is invisible to `/wp-json/wp/v2/{post_type}` and every write 404s. |
| 7 | Taxonomies registered with `show_in_rest: true`, and the terms existing | The tool warns on unmapped values; it never invents terms. |
| 8 | ACF fields exposed to REST, if using ACF | |
| 9 | Media upload limits raised | Print masters are large; see [Media](#media). |
| 10 | Multilingual plugin configured, if multilingual | See [Multilingual](#multilingual). |
| 11 | Permalinks configured and a slug strategy decided | Slugs are the primary idempotency lookup. |

Verify against the **real site**. This pipeline fails silently in more than one place, and green unit
tests prove nothing about a live WordPress install.

## Authentication

HTTP Basic with an application password. `clients.yml` names the env var; `.env` holds the value.

```yaml
wordpress:
  site_url: "https://example.com"
  username: "gs1-publisher"
  app_password_env: CLIENT_WP_APP_PASS
```

> **Quote the application password.** WordPress issues it as six space-separated 4-character groups.
> ```bash
> CLIENT_WP_APP_PASS='abcd EFGH ijkl MNOP qrst UVWX'   # correct
> CLIENT_WP_APP_PASS=abcd EFGH ijkl MNOP qrst UVWX     # BROKEN — loads as "abcd"
> ```
> Unquoted, `source .env` stops at the first space and the variable loads empty — the symptom is a
> `401` with a password you know is right.

A WordPress `401` is **terminal and never retried** (unlike GS1, there is no token to refresh). The
`Authorization` header is never logged, and the `meta.*` subtree is redacted from logged response
bodies.

## Post type, slugs, and idempotency

```yaml
wordpress:
  post_type: "product"          # default "page"
  post_status: "publish"        # use "draft" for a safe first wave
  slug_pattern: null
  target_url_pattern: null
```

Pages are looked up in this order: **`existing_id` → `slug` → `meta.gtin`**, with the last two scoped
to the language. A match is updated in place, keeping its id; no match creates a new page carrying
`meta.gtin`.

**`meta.gtin` is the idempotency key.** It is what makes re-runs converge instead of duplicating, and
it is what powers two guards:

- **`GtinMismatchError` (E8)** — a page exists at the target slug but its `meta.gtin` belongs to a
  different product. The row is logged and **skipped** rather than overwriting someone else's page.
- **`WordPressAPIError` 409 (E11)** — the slug collides with an existing non-GTIN page. Needs a human;
  the tool will not guess.

Setting `post_status: draft` for a first wave is the cheapest way to inspect real output before
anything is publicly visible.

## Page content: ACF or template

Most clients write **ACF fields**, not HTML. `wordpress.acf_map` maps
`{acf_field_name: product_record_field}` and can reach into extras with `extras.`:

```yaml
wordpress:
  acf_map:
    product_title: product_name
    product_tagline: generated_tagline
    product_description: generated_description
    product_material: extras.material
```

Field *names* are the client's, so they live in config rather than in code. Details and the
HTML-template alternative: [`template-variables.md`](template-variables.md).

> **Silent failure #1 — ACF is a second call.** On a multilingual site the page is created with
> `?lang=`, and **ACF is written in a separate follow-up call**. Both are required and *both fail
> silently* if misconfigured: you get a `200`, a page that exists, and no field values. Never trust a
> status code — fetch the rendered HTML and confirm your copy is actually in it.

> **Silent failure #2 — the ACF read-back shape is not the write shape.** What you get reading a
> field is not necessarily what you must send writing it. For images the write shape is an
> **attachment id**, which is why `media.image_write_shape` is config (default `id`) rather than
> hardcoded — a wrong guess is a config edit, not a code change. Confirm it live.

## Multilingual

One page per `(GTIN, language)`, then linked as translations of one another. Configure explicitly:

```yaml
wordpress:
  multilingual_plugin: wpml       # none | polylang | wpml
  default_language: nl
  languages: ["nl", "fr"]
  wpml_helper_path: "/wp-json/gs1dl/v1/translations"
```

The tool also **probes** the site — Polylang's `/wp-json/pll/v1/languages`, then WPML's
`/wp-json/wpml/v1` — but:

> **Silent failure #3 — an explicit config value always wins over the probe, on purpose.** A probe
> can be wrong for reasons unrelated to the real setup: a renamed route, a plugin version change, an
> admin-gated endpoint. If a failed probe were allowed to override a configured `wpml`, the tool
> would swap in `NoOpAdapter`, whose `link_translations` does nothing and raises nothing — so every
> page publishes, reports `ok`, and is simply **never linked to its translation**. Configure the
> plugin explicitly. A mismatch between configured and detected only warns.

### WPML needs a site-side helper

**WPML publishes no core REST route** for assigning a post's language or linking posts as a
translation group — both require its PHP API. So the site must host a small helper (a Code Snippet or
mu-plugin) exposing one route, deliberately shaped like Polylang's so both adapters stay symmetric:

```
POST {wpml_helper_path}
  {"translations": {"nl": 123, "fr": 456}, "source_language": "nl"}
->  {"ok": true, "trid": 42, "translations": {"nl": 123, "fr": 456}}
```

The default path is `/wp-json/gs1dl/v1/translations`; a client may override it.

> **Silent failure #4 — the response is verified, not trusted.** The helper reads `translations` back
> from WPML's own tables rather than echoing the request, and the adapter **asserts it matches what
> was sent**, raising `WordPressAPIError` (409) if not. A silent no-op — the failure this integration
> is most prone to — therefore surfaces as an error instead of as a page that looks published but is
> unreachable in its own language.

`source_language` must be among the linked languages or `ConfigError` is raised: WPML needs a source
to hang the translation group (`trid`) off. Fewer than two languages is a no-op.

Polylang sites need no helper — `PolylangAdapter` posts to `/wp-json/pll/v1/translations` directly.
Single-language sites use `NoOpAdapter`.

> **Silent failure #5 — every count you read is per-language, and nothing says which one.** WPML
> filters by the current language, so listing the product post type returns *one language's* posts.
> Neither language's count is the catalogue, and
> neither is wrong. Anyone eyeballing wp-admin or the REST API to confirm what is live is seeing
> half the truth, and which half depends on a language setting they did not choose. Pass the
> language explicitly (`?lang=fr`, or the admin's switcher) and read both — or read
> `output/{client}/state.json`, which is keyed by `(GTIN, language)` and does not have this problem.

## Taxonomies

```yaml
wordpress:
  taxonomies:
    product_cat:
      map_from_column: "category"
```

Terms must already exist and the taxonomy must be `show_in_rest`. An unmapped value **warns**; the
tool never creates terms or guesses a category. For GPC-brick-driven categorisation, use
`build_brick_map` and its `--check` coverage gate.

## Media

Configured under `media:` — see `MediaConfig` in `lib/config.py` for the full field list.

```yaml
media:
  image_max_dim: 1600
  image_quality: 85
  header_image_field: "product_header_image"
  regular_image_field: "product_regular_image"
  video_file_field: "product_header_video_file"
  image_write_shape: id           # id | url
  video_transcode: false
  require_hero_image: false
```

Media is written **imperatively at execute time**, not through `acf_map` — an attachment id only
exists after upload. That is why the field *names* live in `media:`.

### Images

Source images from a GDSN feed are typically **print masters** — in the pilot, 92% were `image/tiff`
and many ran 10–45 MB at 3200×3200, which WordPress simply refuses. So `convert_image_for_web`
converts **everything** (TIFF, PNG, and already-JPEG alike) to a baseline web JPEG: flattened onto
white, downscaled to `image_max_dim` (never upscaled), aspect preserved, metadata stripped, fixed
encoder parameters.

Converting uniformly rather than only when needed buys two things: the dimension cap applies to every
image, and the output is **byte-deterministic** — identical input bytes give an identical SHA-256, so
re-runs reuse the existing attachment instead of churning duplicates.

Raise the site's upload limits anyway (`upload_max_filesize`, `post_max_size`).

- Undecodable bytes → `None`, logged, **featured media skipped, page still created** (E7).
- `require_hero_image: true` instead holds a GTIN whose source `image_url` is blank out of the plan
  entirely, so a hero-less page can never publish (E22). A *runtime* fetch failure still degrades per
  E7.

### Videos

Videos are **not** in the GDSN feed. The operator supplies per-language folders whose files are named
by marketing name, so `video_map_path` points at a **client-confirmed** name→GTIN mapping. Draft and
gate it:

```bash
python -m scripts.build_video_map            # emit a hint skeleton
python -m scripts.build_video_map --check     # exit 1 if any file is unmapped
```

**Working through the backlog is a spreadsheet job**, and confirming a row is the client's call,
so there is a report to send them:

```bash
python -m scripts.report_video_candidates              # output/{client}/video-map-candidates.xlsx
python -m scripts.report_video_candidates --top-n 5 --format csv
```

One row per (language, file) over the union of the folders and the mapping — so a file nobody has
mapped and a mapping row whose file never arrived are both visible — carrying each row's state, the
GTIN it holds today, that product's names, and the ranked candidates. Read the **value that scored**
and the **field it came from** next to the score: the filenames are English, and this feed's English
sits in the *French* slots, so a 0.83 will often land beside a name that is not the Dutch one. The
`marketing_name` / `logistics_name` columns are what identify a product — `product_name` is the
short generic one (`siliconenbak`, `bezem`).

**Filling it in happens on the Data screen** — step 3 takes the client's sign-off sheet, and *The
mapping, file by file* folded beneath it is the row-by-row editor. It lists
every file per language with its state, offers the same fuzzy hints as suggestions, and writes the
file a row at a time so the comments and the confirmed rows survive. Drafting stays here, in the
terminal: re-drafting discards client sign-off, and redirecting output over the file should be a
deliberate act rather than a button.

`video_transcode: true` runs the ffmpeg H.264/MP4 prepare step — necessary because a source `.mpg`
will not play in an HTML5 `<video>` element. Requires `ffmpeg` on PATH.

### Attachment dedup

Media slugs are **content-addressed**: `{base}-{sha12}`.

This is not cosmetic. Dedup originally used a `content_sha256` meta key, but WordPress **silently
drops unregistered meta on attachments**, so the marker vanished and every run re-uploaded — and
stale attachments squatted the base slug. A content-addressed slug makes dedup a pure slug lookup
that needs no meta and cannot be squatted. If you see media duplicating, verify this scheme is intact
rather than reintroducing a meta key.

## Verifying a page

For every page in a first wave:

1. **Fetch the HTML** and confirm the copy, image, and video are rendered. A `200` is not evidence.
   ```bash
   curl -sS https://{site}/{slug}/ | grep -o 'expected-copy'
   ```
2. **Check the translation link** — the other language's page is reachable via the site's switcher.
3. **Check resolution with GET** — `curl -sS -o /dev/null -w '%{http_code}' -L
   https://id.gs1.org/01/{gtin14}` → 307 → 200. The resolver **404s to HEAD**.

**Counting, rather than checking one page, needs the language named.** A multilingual site answers
"how many products are live?" one language at a time (silent failure #5 above), so a list that looks
short is more often a language filter than a failed run. Ask each language separately, or read
`output/{client}/state.json`.

## Taking a page down

```bash
python -m scripts.run_unpublish --gtin {gtin} --dry-run
python -m scripts.run_unpublish --gtin {gtin}
```

Retracts the Digital Link, drafts the pages, and classifies the GTIN as **HELD** so a later run will
not republish it. Manually drafting a page in wp-admin does **not** hold it — the next run will
publish it again. `run_execute --revive` is the deliberate opt-in to republish held GTINs.

`set_page_status` and `delete_page` exist on the client for finer-grained teardown.

## See also

- [`setup.md`](setup.md) · [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) · [`troubleshooting.md`](troubleshooting.md)
- [`template-variables.md`](template-variables.md) — `acf_map` and templates in detail.
- `IMPLEMENTATION_SPEC.md` §4.4 (client shape), §4.5 (multilingual), §6.1–§6.2 (idempotency).
