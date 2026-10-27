# Template variables and field mapping

Getting product data onto a page. There are two routes — **ACF fields** and **HTML templates** — and
most clients need only the first.

Derived from `lib/acf.py`, `lib/templates.py`, `lib/records.py`, and `lib/config.py`.

## Which route

| | ACF fields (`acf_map`) | HTML template (`template`) |
|---|---|---|
| Output | Individual ACF field values, rendered by the theme | One block of HTML into `post_content` |
| Config | `wordpress.acf_map` | `template.override_dir` + `template.files` |
| Best when | The theme already has a product layout | There is no product layout to fill |
| Used by | The pilot, and most real sites | Simple sites |

They are not exclusive, but if the theme owns the layout, use `acf_map` and leave `content` minimal.
The pilot writes ACF fields exclusively.

## The available fields

Everything comes from a `ProductRecord` (`lib/records.py`):

| Field | Type | Notes |
|---|---|---|
| `gtin` | `str` | As in the export; leading zeros preserved |
| `gtin14` | `str` | Zero-padded to 14 — what the Digital Link URI uses |
| `brand` | `str` | Required |
| `product_name` | localised | Required in the default language |
| `description_short` | localised | |
| `description_long` | localised | |
| `generated_tagline` | localised | Written by the generator, not the export |
| `generated_description` | localised | Written by the generator, not the export |
| `gpc_brick_code` | `str \| None` | |
| `net_content` | `str \| None` | Stored with a GDSN unit code; decoded to words at render |
| `image_url` | `str \| None` | Source URL, not the WordPress attachment |
| `category` | `str \| None` | Resolved from the brick map |
| `extras` | `dict[str, str]` | Everything declared in `gdsn_extras` / `extras_columns` |

**Localised** fields hold one value per language and resolve to the page's language.

## ACF mapping

```yaml
wordpress:
  acf_map:
    product_title:            product_name
    product_tagline:          generated_tagline
    product_description:      generated_description
    product_brand:            brand
    product_net_content:      net_content
    product_material:         extras.material
    product_header_video_text: generated_tagline     # reusing one source is fine
```

`{acf_field_name: product_record_field}`. Reach into extras with `extras.{name}`. Several ACF fields
may share one source.

### Rules that matter

**A missing value omits the field — it does not fail the page.** An unresolvable source logs a warning
and the field is left out. A missing tagline should not stop a page publishing with its title and
image. An entirely empty payload means the ACF write is skipped altogether rather than sending
nothing.

**No cross-language fallback, ever.** A localised field absent in this page's language resolves to
`None` and the field is omitted. Falling back to another language would put Dutch text on a French
page — worse than an empty field.

**Media is not in `acf_map`.** Images and video are written imperatively at execute time, because an
attachment id only exists after upload. Their field *names* live under `media:` —
`header_image_field`, `regular_image_field`, `video_file_field`. See
[`wordpress-onboarding.md`](wordpress-onboarding.md#media).

**Deterministic fields should stay deterministic.** Only map a `generated_*` field where copy genuinely
has to be written. Anything derivable from the export — dimensions, material, net content — should map
straight through, so it cannot drift or be fabricated.

## HTML templates

Mustache, rendered by `pystache`.

```yaml
template:
  override_dir: "templates/myclient"      # optional
  files:
    nl: "product.nl.html"
    fr: "product.fr.html"
```

**Resolution order**, first existing file wins (`{file}` is `files[language]`, or
`product.{language}.html` when there is no entry for the language):

1. `input/{client_id}/templates/{file}` in the **data folder**, which is where a client's templates
   belong: they travel there in a handover pack, and a release that ships none still finds them
2. `{override_dir}/{file}` in the program folder, or `templates/{client_id}/{file}` when
   `override_dir` is unset: the fallback for an installation that has not moved them
3. `templates/_default/product.{language}.html`: the built-in fallback

If none exists, `TemplateError` names every path it tried.

### Context

```
{{gtin}}  {{gtin14}}  {{brand}}  {{product_name}}
{{description_short}}  {{description_long}}
{{gpc_brick_code}}  {{net_content}}  {{image_url}}  {{category}}
{{language}}
{{extras.<name>}}
{{client.display_name}}  {{client.id}}
```

Localised fields are already resolved to `{{language}}`. `net_content` is decoded from its GDSN unit
code into words in the page's language.

**`None` renders as empty, not `"None"`.** Absent scalars become empty strings, so a bare `{{field}}`
renders nothing and a `{{#field}}` section stays correctly falsy.

### Two rules

**E12 — a missing `extras` key renders empty and warns once.** `{{extras.material}}` for a product
with no `material` extra produces nothing and logs one warning per template/key per run, not one per
product. Deliberate: a page missing an optional attribute should still publish, but you should be
told.

**E13 — never use triple-brace.** `{{{field}}}` disables HTML escaping. Product data can legitimately
contain `<`, `&`, or even `{{`, and `{{ }}` escapes it at insertion. Triple-brace turns a product name
into an injection vector. There is no valid reason to use it here.

The renderer runs with `missing_tags="ignore"`, so an unknown tag renders empty rather than raising —
which makes E12's explicit warning the only signal you get. Read the warnings.

## Verifying

`acf_map` is validated against your **site's actual field names**, and nothing checks them for you —
a typo produces a page that publishes cleanly with a field silently missing.

```bash
# 1. Dry-run and read the warnings — every omitted field is logged.
python -m scripts.run_execute --plan output/{client_id}/plan.json --dry-run

# 2. Publish ONE page as a draft, then read the rendered HTML.
curl -sS https://{site}/{slug}/ | grep -o 'expected-copy'
```

A `200` is not evidence. The ACF write is a **separate call** from the page create, and both fail
silently — see [`wordpress-onboarding.md`](wordpress-onboarding.md#page-content-acf-or-template).

## Generated content

Fields prefixed `generated_` are filled by the copy generator, not the export. Two backends share one
cache and contract:

```bash
python -m scripts.run_generate --emit      # queue requests for the in-session producer
python -m scripts.run_generate --validate  # check the results against this run
python -m scripts.run_generate --backend api
```

`--emit` / `--validate` needs **no API key** — Claude writes the copy in session. Re-run `run_plan`
afterwards so the copy merges into the plan. See [`costs.md`](costs.md).

**E21:** if the generator is enabled but a `(GTIN, language)` has no generated tagline — for instance
because the source marketing message is blank — the row is **SKIPPED** from the plan so a blank page
can never publish. The gap is reported as `missing_generation_input`. Fix the source data in MyGS1;
do not fabricate a value to clear the warning.

## See also

- [`data-source-export-schema.md`](data-source-export-schema.md) — where these fields come from.
- [`wordpress-onboarding.md`](wordpress-onboarding.md) — where they go, and the silent failures.
- [`troubleshooting.md`](troubleshooting.md) — `TemplateError`, E12, E13, E21.
- `IMPLEMENTATION_SPEC.md` §3 (mapping and template variables), §4.6 (templates).
