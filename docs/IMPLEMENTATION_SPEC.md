# Implementation Specification — GS1 Digital Link Orchestrator

**Purpose:** This document is the source of truth for **how** the tool is built. `PROJECT_HANDOVER.md` explains **why**. Read that first, keep this open while coding.
**Audience:** The person coding (with Claude Code as co-pilot), and Claude Code itself.
**Version:** 0.3
**Last updated:** 2026-07-04

---

## 0. How to use this document with Claude Code

When starting a new session, paste this document into Claude Code's context (or reference it by path if using Claude Code's file access). Point to the section relevant to what you're about to build, e.g. "Implement `lib/gs1_dl_client.py` per §4.3 and §5, with fixtures from §13.2."

The design goal: any single module should be implementable by asking Claude Code "build this per §X" without needing to explain naming, error handling, or interface conventions again.

**For an agent building this project from scratch** — read the docs in this order, ignore the rest:

| Document | Purpose | When |
|---|---|---|
| `PROJECT_HANDOVER.md` | The "why" — scope, decisions, phases, risks | **First**, fully |
| `IMPLEMENTATION_SPEC.md` (this) | The "how" — types, contracts, DoD | **Second**, fully — operational bible |
| `architecture.svg` | System diagram | Skim for spatial context |


**Where to find what while building:**

| Need | Location |
|---|---|
| Phase overview (11 phases, effort, exit gates) | `PROJECT_HANDOVER.md` §8.1 |
| What each phase actually does | `PROJECT_HANDOVER.md` §8.2 |
| Definition of Done per phase | This document, §12 |
| Copy-paste starter prompt for a phase | Obsolete — all phases complete |
| Coding conventions (style, error handling, HTTP, JSON) | This document, §1 |
| GS1 NL API v2 spec (endpoints, bodies, responses) | `PROJECT_HANDOVER.md` §4.1 + §4.2 |
| Client-code shape for `lib/gs1_dl_client.py` | This document, §4.3 |

**Sections marked `TODO — needs real data`** cannot be finalised until the data-gathering steps in §13 are executed. Skip those sections and fall back to §12 acceptance criteria (which are testable without real data) when building modules that depend on them.

---

## 1. Language, style, and conventions

- **Python 3.11+**. Use PEP 604 union syntax (`str | None`, not `Optional[str]`).
- **Type hints mandatory** on every function signature. `mypy --strict` should pass.
- **Docstrings**: Google style. Every public function/class gets one.
- **Naming**: `snake_case` for functions, variables, module names; `PascalCase` for classes and types; `SCREAMING_SNAKE` for module-level constants.
- **Line length**: 100.
- **Formatter**: `ruff format` (config in `pyproject.toml`, see §1.1). No manual formatting debates.
- **Linter**: `ruff check` with rules `E,F,I,N,UP,B,SIM,PL`. No unused imports, no unused vars, no wildcard imports.
- **Imports**: absolute (`from lib.gs1_dl_client import ...`), never relative.
- **String formatting**: f-strings for interpolation, `.format()` only when the template is separately configurable (e.g. `target_url_pattern` from `clients.yml`).
- **Errors**: raise typed exceptions from `lib.errors` (§4.10). Never bare `raise Exception(...)`.
- **Logging**: `logging` module, never `print()`. Loggers named after the module (`logging.getLogger(__name__)`).
- **HTTP**: `httpx` (sync client). Not `requests`. Consistent across all modules.
- **JSON**: stdlib `json`. `pydantic` for structured schemas (`ProductRecord`, `Plan`, etc.), not for HTTP-response shapes (use `TypedDict` for those).
- **No sync-in-async or vice versa mixing**. The whole codebase is sync. If a future async need arises, it's a separate design decision.

### 1.1 pyproject.toml (relevant excerpts)

```toml
[project]
name = "gs1-digital-link-orchestrator"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.6",
    "openpyxl>=3.1",
    "pyyaml>=6.0",
    "qrcode[pil]>=7.4",
    "pystache>=0.6",
    "jsonschema>=4.21",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-httpx>=0.30", "mypy>=1.9", "ruff>=0.4"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "PL"]

[tool.mypy]
strict = true
python_version = "3.11"
```

---

## 2. Type definitions (Python)

All types live in `lib/records.py` unless noted. Use `pydantic.BaseModel` with `model_config = ConfigDict(frozen=True)` for immutability where noted.

### 2.1 `ProductRecord`

The internal normalised shape produced by `parse_export.py` and consumed by everything downstream. **Language-agnostic at the top level; language-specific fields nested.**

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class LocalisedText(BaseModel):
    """A text value that varies per language.

    Keys are ISO 639-1 codes (nl, en, fr, de, ...).
    """

    model_config = ConfigDict(frozen=True)
    values: dict[str, str]

    def get(self, lang: str, fallback: str | None = None) -> str | None:
        return self.values.get(lang, self.values.get(fallback) if fallback else None)


class ProductRecord(BaseModel):
    """The canonical internal shape for one product.

    Every downstream module (templates, WP client, GS1 client, QR, state) consumes
    this. `parse_export.py` produces it from the client's Excel; the column-mapping
    layer in §3 handles the client-specific variation.
    """

    model_config = ConfigDict(frozen=True)

    gtin: str = Field(..., pattern=r"^\d{8,14}$")
    brand: str
    product_name: LocalisedText

    gpc_brick_code: str | None = None
    net_content: str | None = None
    image_url: str | None = None
    category: str | None = None

    description_short: LocalisedText | None = None
    description_long: LocalisedText | None = None

    extras: dict[str, str] = Field(default_factory=dict)

    @property
    def gtin14(self) -> str:
        """Zero-padded to 14 digits for Digital Link URIs."""
        return self.gtin.zfill(14)
```

### 2.2 `Plan`, `PlanRow`, `PlanClassification`

```python
from enum import Enum


class PlanClassification(str, Enum):
    NEW = "new"
    UNCHANGED = "unchanged"
    CHANGED = "changed"


class PlanRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    classification: PlanClassification
    title: str
    slug: str
    content_hash: str
    target_url: str
    diff: dict[str, tuple[str, str]] | None = None
    product: ProductRecord


class SkipReason(StrEnum):
    MISSING_PRODUCT_NAME = "missing_product_name"  # E18
    NO_GENERATED_COPY = "no_generated_copy"  # E21
    BLANK_HERO_IMAGE = "blank_hero_image"  # E22


class SkippedUnit(BaseModel):
    model_config = ConfigDict(frozen=True)

    gtin: str
    language: str
    reason: SkipReason
    detail: str


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_id: str
    generated_at: datetime
    total: int
    counts: dict[PlanClassification, int]
    rows: list[PlanRow]
    skipped: list[SkippedUnit] = Field(default_factory=list)


class ConfirmedPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: Plan
    confirmed_gtins_by_lang: set[tuple[str, str]]
```

### 2.3 `RunOutcome`, `StateEntry`

```python
class RunOutcome(BaseModel):
    gtin: str
    language: str
    ts: datetime
    status: str
    wp_page_id: int | None = None
    wp_url: str | None = None
    wp_featured_media_id: int | None = None
    gs1_set: bool = False
    qr_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    failed_call: str | None = None


class StateEntry(BaseModel):
    wp_page_id: int
    wp_url: str
    wp_featured_media_id: int | None
    content_hash: str
    gs1_link_set_hash: str
    last_run: datetime
    title: str | None = None
    wp_status: str = "publish"
    retracted: bool = False
```

`failed_call` names the request a failed row blames — method, path, and the client's own label,
e.g. `POST /wp-json/wp/v2/media (upload media clip-a1b2c3d4e5f6)`. A row issues a page write, an
ACF write, a URL verification and up to two media uploads, and `error` alone does not tell them
apart: a live `403` recorded as `WordPressAPIError('WordPress API error 403')` took a re-run with
the output captured to a file before anyone knew it was a video upload rather than the page. The
API clients now carry the call (and a scrubbed, bounded excerpt of the response body) on the
exception itself — see `lib/errors.py` — and `run_execute` reads it back, following `__cause__`
so the deliberate `RuntimeError` wrap in `_verify_targets` does not lose it. Optional because run
logs predating the field have none and because not every failure is a call: `None` means "not
recorded", and readers omit it rather than guessing.

`wp_status` and `retracted` record a **deliberate take-down**, which the hashes cannot express:
they describe what was written, not whether it is still serving. `lib.state._is_held` reads both,
and either half alone is enough — `run_unpublish` retracts the resolver *before* it drafts the
pages, so an interrupted take-down must still read as held or the next run would reverse it
instead of finishing it. Both default to the published condition, so state files written before
they existed load unchanged.

`retracted` was called `gs1_enabled` and read as "is the resolver record enabled", which it never
was: `run_unpublish` is the only writer and `_is_held` the only reader, and both mean *somebody
took this down on purpose*. Under the old name a `--only pages` run recorded `gs1_enabled: true`
with no GS1 record in existence. Whether a resolver record exists is already recorded one field
up — an empty `gs1_link_set_hash` — which is why the rename adds no state. A file carrying the old
key is **translated on the way in, inverted**, rather than ignored: pydantic drops unknown keys by
default, and dropping this one would put every deliberately retracted product back on the site.

`title` is the page title as last written — the one product field state keeps verbatim, so a
re-run can show a real before/after in a CHANGED row's diff (§10.6.2). `content_hash` proves
*that* a product changed but, being a digest, can never say *what*; without a retained title the
§10.6.2 block renders an empty `Changes:` list whenever a rename leaves the (GTIN-derived) slug
in place. Optional because state files predating the field have no title: `None` means "not
recorded", and `_classify` omits the title row rather than guessing.

```python
class State(BaseModel):
    client_id: str
    entries: dict[str, dict[str, StateEntry]]
```

### 2.4 Config types (in `lib/config.py`)

Full type stubs for `GS1Config`, `ExportConfig`, `WordPressConfig`, `QRConfig`, `TemplateConfig`, `GS1LinkConfig`, `FlowConfig`, `ClientConfig`. See earlier version of this section or reconstruct from `clients.example.yml` in `PROJECT_HANDOVER.md` §10.1 — each key becomes a Pydantic field with matching type and `Literal` validation where enumerated.

---

## 3. Column mapping and template variable system

The core insight: **each client's MyGS1 export has different columns, and each client's WordPress template has different placeholders. The `ProductRecord` layer is what bridges them, and the client controls the bridge via `clients.yml`.**

### 3.1 The two-hop mapping

```
Excel column         →  ProductRecord field   →  Template placeholder
"Productnaam NL"     →  product_name.nl        →  {{product_name}}
"Merk"               →  brand                   →  {{brand}}
"Foto URL"           →  image_url               →  {{image_url}}
"HS-code"            →  extras.hs_code          →  {{extras.hs_code}}
```

The first hop is configured in `clients.yml` under `export.column_map` and `export.extras_columns`. The second hop is fixed: templates always read from `ProductRecord` fields.

### 3.2 How `column_map` works

Keys are Excel column names (exactly as they appear in the header row, case-sensitive, whitespace-preserved). Values are canonical `ProductRecord` field paths.

Supported target paths:
- **Language-agnostic**: `gtin`, `brand`, `gpc_brick_code`, `net_content`, `image_url`, `category`
- **Per-language**: `product_name.{lang}`, `description_short.{lang}`, `description_long.{lang}`
- **Free-form**: `extras.{name}`

Example for Democlient (NL + FR):
```yaml
export:
  path: "./input/democlient/products.xlsx"
  column_map:
    "GTIN":                        gtin
    "Merk":                        brand
    "Productnaam NL":              product_name.nl
    "Productnaam FR":              product_name.fr
    "Korte omschrijving NL":       description_short.nl
    "Korte omschrijving FR":       description_short.fr
    "GPC brick":                   gpc_brick_code
    "Foto URL":                    image_url
    "Categorie":                   category
  extras_columns:
    - "HS-code"
    - "Barcode type"
```

### 3.3 Required vs. optional mappings

Hard requirements (parse aborts if missing): `gtin`, `brand`, `product_name.{default_language}`.
Warnings only: any Excel column not in `column_map` and not in `extras_columns`. Ensures a client doesn't silently lose a column they meant to use.

### 3.4 Templates

Mustache syntax via `pystache`. Variables:
- `{{gtin}}`, `{{gtin14}}`, `{{brand}}`, `{{gpc_brick_code}}`, `{{net_content}}`, `{{image_url}}`, `{{category}}`
- `{{product_name}}`, `{{description_short}}`, `{{description_long}}` — resolved to current language
- `{{extras.HS-code}}` etc — as spelled in `extras_columns`
- `{{language}}`, `{{client.display_name}}`, `{{client.id}}`

Templates at `templates/{client_id}/product.{lang}.html`, falling back to `templates/_default/product.{lang}.html`.

### 3.5 Adding a new client — mapping workflow

1. Client provides their MyGS1 export
2. `python scripts/inspect_export.py path/to/export.xlsx` — lists columns with samples
3. Draft `clients.yml` `export.column_map` block
4. `python scripts/parse_export.py {client_id} --dry-run` — validates
5. Iterate until warnings clear
6. Write WordPress template referencing the populated fields

### 3.6 GDSN datapool exports (`export.format: gdsn`) — spec extension

§3.1–§3.5 describe a **flat** single-sheet export (`export.format: flat`, the default).
The pilot client's real export from **GS1 Data Source / Netherlands** is a **GDSN datapool**
export, which is structurally different, so Phase 3 also supports `export.format: gdsn`:

- **Multi-sheet**: one worksheet per GDSN module (`TradeItemDescription`,
  `MarketingInformation`, `TradeItemMeasurements`, `ReferencedFileDetailInformation`, …).
- **7 header rows** per sheet; data starts on the 8th. Each column's identity is a nested
  attribute *path* plus a label carrying the stable GDSN attribute number, e.g.
  `TradeItemDescriptionInformation > DescriptionShort[0] > Value` / `"Short product name (3297)"`.
- **Composite key**: every sheet is keyed on `Gtin` + `TargetMarketCountryCode` +
  `TradeItemUnitDescriptorCode`; the same GTIN recurs once per target market.
- **Localised text** is stored as adjacent `LanguageCode`/`Value` column pairs; measurements
  as `MeasurementUnitCode`/`Value` pairs.

Instead of `column_map`/`extras_columns`, a GDSN client declares:

- `market_language` — `{market_code: language}`, i.e. which market supplies each language
  (e.g. `{"528": "nl", "056": "fr"}`).
- `gdsn_map` — `{ProductRecord field: {sheet, attribute, localised?, with_unit?, primary_file?}}`.
  `attribute` is the GDSN attribute number (`"3297"`) or a path-segment name
  (`GpcCategoryCode`).
- `gdsn_extras` — the same shape, carried into `ProductRecord.extras`. Same shape means the same
  flags: an extra marked `required` holds the SKU exactly as a mapped field does (E23), and gets
  the same mandatory column in the coverage matrix. Consumers of that question read
  `ExportConfig.all_sources`, which merges both maps, rather than picking one.

`lib/gdsn.py` reads the workbook (`read_workbook`) and joins the sheets by GTIN into
`ProductRecord`s (`build_records`), selecting each language's value from its configured
market's `LanguageCode`/`Value` pair. `scripts/inspect_export.py` lists every sheet's
attributes and emits a suggested `gdsn_map`. The onboarding workflow (§3.5) is otherwise
unchanged: inspect → draft `gdsn_map` → `--dry-run` → iterate to zero warnings.

---

## 4. Module contracts

### 4.1 `lib/errors.py`

Typed exceptions: `OrchestratorError` (base), `ConfigError`, `MissingCredentialError`, `ExportParseError`, `GS1APIError(status_code, response_body, error_results: list[dict] | None = None, request_id: str | None = None)`, `WordPressAPIError(status_code, response_body)`, `TemplateError`, `StateError`.

**Five more were added as later phases needed them — `lib/errors.py` now carries 13 classes, not the 8 above.** Every one still derives from `OrchestratorError`, so `except OrchestratorError` remains the catch-all:

| Added | Raised by | Purpose |
|---|---|---|
| `OverwriteError(gtin, existing)` | `gs1_dl_client.safe_upsert` | GET-before-write guard: refuses to replace an existing Digital Link without `overwrite=True`. Carries the prior snapshot for rollback. |
| `GtinMismatchError(gtin, existing_gtin, wp_page_id)` | `wp_client.upsert_page` | E8 — a page at the target slug belongs to a different GTIN. Distinct from `WordPressAPIError` so callers **log and skip the row** rather than treating it as a transport failure. |
| `ProcessListError` | `process_list.load_process_list` | The process list is missing/unreadable/lacks the GTIN column/contains no GTINs. Treated like `ConfigError` (exit 2) — it names which products a run may touch. |
| `GeneratorError` | `lib.generator` | Corrupt, unwritable or wrong-client `generation_results.json`, or a producer result that fails validation. |
| `LLMAPIError(status_code, response_body, message=None)` | `lib.llm.AnthropicClient` | API failure, transport failure (`status_code == 0`), or a 200 lacking the forced `produce_copy` tool call. Only reachable via `run_generate --backend api`. |

Operator-facing reference for all 13, with symptoms and fixes: `docs/troubleshooting.md`.

`GS1APIError.error_results` carries the parsed 400 body when the response follows the standard v2 ErrorResult shape (`[{identifier, errors: [{code, message}]}]`); falls back to raw `response_body` when the body isn't in that shape (e.g. 5xx with plain text, or a non-standard error format). See §5.1 for parsing rules.

### 4.2 `lib/config.py`

`load_clients(path) -> dict[str, ClientConfig]` — validates against `schema/clients.schema.json`, applies defaults, does not resolve secrets (lazy). `get_client(client_id) -> ClientConfig` for scripts. Never log or return resolved secret values.

### 4.3 `lib/gs1_dl_client.py`

`GS1DigitalLinkClient(config)` — hosts derived from `config.environment` (test → `gs1nl-api-acc.gs1.nl`, production → `gs1nl-api.gs1.nl`). Path prefix `/digitallinkv2/v2/` is a module constant.

**Methods:**

```python
def upsert(
    self,
    gtin: str,
    item_description: str,
    links: list[LinkInput],
    is_enabled: bool = True,
    application_identifiers: list[AppIdentifier] | None = None,
) -> None:
    """POST /digitallinkv2/v2/digitallink.

    Builds CreateOrUpdateRequest body with:
    - accountNumber: self.config.account_number
    - identificationKeyType: "Gtin"
    - identificationKey: gtin (zero-padded to 14 via .zfill(14))
    - isEnabled: is_enabled
    - itemDescription: item_description
    - resolverSettings: {useGS1Resolver: config.resolver_settings.use_gs1_resolver,
                        resolverDomainName: config.resolver_settings.resolver_domain_name}
    - links: [{linkType, language, linkTitle, targetUrl, defaultLinkType,
              public, mediaType} for each]
    - applicationIdentifiers: application_identifiers or []

    Idempotent: same input twice → same server state.
    Raises: GS1APIError on non-2xx.
    """


def upsert_bulk(self, entries: list[BulkEntry]) -> BulkResult:
    """POST /digitallinkv2/v2/digitallinks.

    Body is a JSON array of CreateOrUpdateRequest bodies, same shape as single
    upsert. Batches automatically into groups of self.config.batch_size (default 50).
    """


def get(self, gtin: str) -> DigitalLinkRecord | None:
    """GET https://{host}/digitallinkv2/v2/digitalLink/01/{gtin14}

    Note (confirmed in Phase 2): the path segment is the GTIN **application
    identifier "01"**, NOT the string "Gtin"; "digitalLink" is capital-L (differs
    from the lowercase POST paths). Preserve exactly. (Using "Gtin" 404s for
    every GTIN.)

    Response shape: AdvancedDigitalLinkResponse — see PROJECT_HANDOVER §4.2:
        accountNumber, identificationKeyType, identificationKey, isEnabled,
        itemDescription, useGs1Elabel, isElabelSupported, digitalLinkUrl,
        resolverSettings (nested; resolverDomainName populated, e.g.
        "https://id.gs1.org"), links[] (LinkResponse, incl. linkTypeTitle and
        isElabelLink), applicationIdentifiers[]. Returns the record even when
        isEnabled is false.

    Not-found behaviour (confirmed in Phase 2):
        A missing GTIN returns 400 with body
        "No valid contract found for Gtin with id: {gtin}" → return None.
        (A 404, should the deployment change, is also treated as not-found.)
        Other 4xx/5xx → raise GS1APIError.
    """


def set_enabled(self, gtin: str, is_enabled: bool) -> None:
    """PATCH https://{host}/digitallinkv2/v2/digitalLink/01/{gtin14}/activationStatus

    Toggle the isEnabled flag without rewriting the full record. Path keys on the
    GTIN application identifier "01" (as get()). Useful for lifecycle actions like
    temporarily disabling a QR during a recall.

    Body: {"isEnabled": <bool>}
    Success: 204 No Content. Note: to *re-enable* a record, re-`upsert` with
    isEnabled=true (PATCH targets an existing findable record).

    Not exposed as an MCP tool in v0.1.0 (client method only). Add MCP wrapper
    in v0.2 if a workflow needs it.
    """


def validate_draft(
    self,
    gtin: str,
    application_identifiers: list[AppIdentifier] | None = None,
) -> ValidateDraftResult:
    """POST https://{host}/digitallinkv2/digitalLink/validateDraft

    Note: this endpoint does NOT have /v2/ in its path — the only v2 endpoint
    without that segment. Preserve exactly.

    Body: ValidateDigitalLinkDraftModel — see PROJECT_HANDOVER §4.2.
    Response: ValidateDigitalLinkDraftResponse (isValid + error message +
    available AIs + currentAnchorRelative).

    Use case: pre-flight validation before a bulk upsert. Not integrated into
    run_plan.py for v0.1.0 (deferred to v0.2). Provided so a future skill can
    validate a batch and only upsert the valid rows.
    """
```

**Auth (OAuth2 client-credentials — confirmed in Phase 2):** the client mints a short-lived JWT and sends it as a Bearer token. `GS1Config` carries `client_id_env` / `client_secret_env` (env var names) and `account_number` already resolved for the target `environment`.

```python
def _mint_token(self) -> str:
    # POST https://{host}/authorization/token with lowercase client_id /
    # client_secret headers -> {"access_token", "token_type", "expires_in"}.
    headers = {
        "client_id": os.environ[self.config.client_id_env],  # MissingCredentialError
        "client_secret": os.environ[self.config.client_secret_env],
    }
    resp = self._http.request("POST", self._base_url + "/authorization/token", headers=headers)
    if resp.status_code != 200:
        # 4xx -> ConfigError (bad/rotated credentials); else GS1APIError.
        ...
    data = resp.json()
    self._token = data["access_token"]
    self._token_expiry = time.monotonic() + float(data.get("expires_in", 3600))
    return self._token


def _auth_header(self) -> dict[str, str]:
    return {"Authorization": f"Bearer {self._get_token()}"}
```

`_get_token()` returns the cached token until ~60s before `expires_in` (default 3600s), then re-mints. A `401` from the Digital Link API invalidates the cache and triggers one re-mint + retry. Credentials and token are read from the env at mint time and **never logged**. (The earlier static-token / `auth_scheme` Bearer-vs-raw model is retired — see PROJECT_HANDOVER §4.1.)

**Retry policy:**
- 429: honour `Retry-After` if present, else exponential base 1s max 60s, up to 5 attempts
- 5xx: exponential base 0.5s max 30s, up to 3 attempts
- 4xx (not 429): raise immediately
- Network errors (`httpx.ConnectError`, `httpx.ReadTimeout`): as 5xx

**Timeouts:** connect 10s, read 30s, write 30s (constructor-configurable for tests).

**Logging:** INFO per success (GTIN, endpoint, elapsed ms); WARNING per retry; ERROR on final failure with abbreviated response body (first 500 chars, PII-scrubbed per §5.2). **Never log the token value.** Scrub `Authorization` header before logging any request record.

### 4.4 `lib/wp_client.py`

`WordPressClient(config)` — auth is HTTP Basic with `username` and resolved app password. Read timeout 60s.

`detect_multilingual_plugin() -> Literal["polylang", "wpml", "none"]` — runs at construction.

`upsert_page(post_type, slug, title, content, language, featured_media=None, parent=None, meta=None, existing_id=None) -> WordPressPage` — idempotent. Lookup order: (1) `existing_id`, (2) slug, (3) `meta.gtin`. Multilingual: link translations after creation via plugin-specific endpoint. Raises `WordPressAPIError` on non-2xx.

`upload_media(file_path, title=None) -> int` — idempotent via slug lookup.
`verify_url(url) -> bool` — HEAD, true iff 200 ≤ status < 400.
`find_by_slug(post_type, slug) -> WordPressPage | None`.

**Idempotency contract:** `upsert_page` idempotent w.r.t. `(site_url, post_type, meta.gtin)` when `meta.gtin` present. Callers must always set it.

### 4.5 `lib/multilingual.py`

`MultilingualAdapter` base with `link_translations(wp, translations: dict[str, int])`. Concrete: `PolylangAdapter` (uses `/wp-json/pll/v1/` endpoints), `WPMLAdapter`, `NoOpAdapter` for `multilingual_plugin: none`. Built by `make_adapter(plugin, *, wpml_helper_path, source_language)`.

> **`WPMLAdapter` is implemented and in production — this section previously described it as a "stub raises `NotImplementedError`, v0.2". That was the plan; the pilot needed it, so it was built.** WPML publishes no core REST route for assigning a post's language or linking a translation group (both need its PHP API), so the adapter POSTs to a small **site-side helper** — a Code Snippet / mu-plugin — at `wordpress.wpml_helper_path` (default `/wp-json/gs1dl/v1/translations`; a site can override it). The route is deliberately shaped like Polylang's so both adapters stay symmetric:
>
> ```
> POST {helper_path}  {"translations": {"nl": 123, "fr": 456}, "source_language": "nl"}
> ->                  {"ok": true, "trid": 42, "translations": {"nl": 123, "fr": 456}}
> ```
>
> The helper reads `translations` back from WPML's own tables rather than echoing the request, and `_assert_linked` **verifies it matches what was sent**, raising `WordPressAPIError` (409) otherwise — so a silent no-op, the failure this integration is most prone to, surfaces as an error rather than as a page that looks published but is unreachable in its own language. `ConfigError` if `source_language` is absent from the linked set (WPML needs a source to hang the `trid` off), or if `wpml` is configured without both `wpml_helper_path` and `default_language`. Fewer than two languages is a no-op.
>
> **Plugin selection: an explicit config value beats detection, deliberately.** `wp_client._resolve_plugin` probes Polylang's `/wp-json/pll/v1/languages` then WPML's `/wp-json/wpml/v1`, but only *uses* the probe when config says `none`; a configured value wins and a mismatch merely warns. Letting a failed probe override a configured `wpml` would substitute `NoOpAdapter`, whose `link_translations` does nothing and raises nothing — every page would publish, report `ok`, and never be linked. Helper source and live verification: `docs/clients/democlient-page-adapter.md` §7; operator-facing guide: `docs/wordpress-onboarding.md`.

### 4.6 `lib/templates.py`

`TemplateEngine(client_id, template_config)` with `render(product, language, client_meta) -> str`. Resolution order: `templates/{client_id}/product.{language}.html` → `templates/_default/product.{language}.html` → `TemplateError`.

### 4.7 `lib/qr.py`

`render_qr(uri, output_dir, gtin, formats, size_mm, ecc, dpi=300) -> list[Path]`. Uppercase-domain optimisation applied (scheme + hostname uppercased, path case preserved).

### 4.8 `lib/state.py`

`load_state(client_id) -> State` (empty if not present; a **corrupt** file is quarantined to `state.json.corrupt.{ts}` and an empty state returned with `reset_from_corrupt=True` — see E19 in §7; an **unreadable** file raises `StateError`), `save_state(state)` (atomic write-to-temp-then-rename), `compute_content_hash(product, language, target_url) -> str` (SHA-256 over canonicalised JSON), `diff_against_state(products, state, languages, target_url_pattern) -> list[PlanRow]`.

### 4.9 `lib/records.py`

Types from §2.1–§2.3 plus `parse_excel_row(row, column_map, extras_columns, default_language) -> ProductRecord`.

### 4.10 `lib/logging_setup.py`

`setup_logging(client_id, level="INFO")` — console INFO+ to stderr, file DEBUG+ to `output/{client_id}/runs/{ts}.log`. JSONL run log written by scripts, not by logging.

---

## 5. Error handling matrix

### 5.1 HTTP call outcomes

| Layer | Status | Action | Retries | Logs |
|---|---|---|---|---|
| GS1 API | 200/201/204 | success | — | INFO |
| GS1 API | 400 "No valid contract found" (GET) | not-found → `get()` returns `None` | none | INFO |
| GS1 API | 400/401/403 (other) | raise `GS1APIError` immediately | none | ERROR |
| GS1 API | 404 (GET) | not-found → return `None` (fallback; real not-found is the 400 above) | none | INFO |
| GS1 API | 404 (POST) | raise `GS1APIError` (unexpected) | none | ERROR |
| GS1 API | 409 | raise `GS1APIError` (conflict) | none | ERROR |
| GS1 API | 429 | back off | up to 5 | WARN retries, ERROR final |
| GS1 API | 5xx | exponential retry | up to 3 | WARN retries, ERROR final |
| GS1 API | timeout | as 5xx | up to 3 | WARN retries, ERROR final |
| WP API | 200/201 | success | — | INFO |
| WP API | 400/401/403 | raise `WordPressAPIError` immediately | none | ERROR |
| WP API | 404 (GET lookups) | return `None` | none | INFO |
| WP API | 409 (slug conflict) | raise `WordPressAPIError` — needs human | none | ERROR |
| WP API | 429 | back off | up to 5 | WARN retries |
| WP API | 5xx | exponential retry | up to 3 | WARN retries |
| WP verify URL | anything except 2xx/3xx | raise `WordPressAPIError` | none | ERROR |

**GS1 400 error body parsing:** When GS1 API returns 400, the response body is expected to be a JSON array following the standard `ErrorResult` shape: `[{"identifier": "<GTIN>", "errors": [{"code": "<CODE>", "message": "<MSG>"}]}]`. The client attempts to parse this and populate `GS1APIError.error_results`. If parsing fails (unexpected shape, non-JSON body, etc.), `error_results` stays `None` and the raw body is preserved on `response_body`. Callers should check both fields — structured errors for programmatic handling, raw body as fallback for logging.

### 5.2 PII scrubbing in logs

When logging response bodies, replace values of keys matching these patterns with `[REDACTED]`: `password`, `secret`, `token`, `key`, `authorization`; anything under `meta.*` in WP responses. Implement as `lib.logging_setup.scrub_response_body(body: str) -> str`.

### 5.3 Run-level failure policy

Per-row failures logged as `RunOutcome(status="error", ...)` and loop continues. Run does **not** abort on individual row failures. Exit 0 if all rows succeeded, 1 if any errored; state file saved with partial results. Aggregate summary to stderr at end.

Exception: configuration/credential errors at startup abort immediately with exit code 2.

### 5.4 Rollback and recovery

Tool implements **Level A + B** for v0.1.0. Level C documented for future.

**Level A — Structured logging + manual rollback.** Every mutating op produces a `RunOutcome` in `output/{client_id}/runs/{ts}.jsonl`. WP pages revert via WP admin (page revisions preserved). GS1 entries manually via MyGS1 UI or re-run with previous state. QR files overwritten by re-runs. Acceptable for v0.1.0 since pilot runs are 10–100 products.

**Level B — Dry-run and preview.** `run_plan.py` produces `plan.json` describing intended changes. Orchestrator skill shows plan in chat before `run_execute.py`. `--dry-run` on `run_execute.py` walks plan but replaces mutating HTTP calls with logging. Primary rollback mechanism: **prevent bad states rather than recover from them.**

**GET-before-write guard (Phase 2, implemented).** `gs1_dl_client.safe_upsert()` reads the current state first (`get()` is the snapshot primitive), **refuses to overwrite an existing Digital Link** unless `overwrite=True` (raises `OverwriteError`), and returns the prior snapshot for rollback. This is the client-level guard against silently clobbering a live resolver target — mandatory for any production run; `run_execute.py` snapshots the returned prior state before applying a change.

**Level C — Snapshot and automated rollback (deferred).** Design sketch: before `run_execute.py`, snapshot server state per GTIN (`snapshots/{ts}/wp/{gtin}.{lang}.json`, `snapshots/{ts}/gs1/{gtin}.json`). New `run_rollback.py {client_id} {snapshot_ts}` replays snapshot via same clients. Trade-offs: snapshot storage retention policy needed; deleting WP page vs. reverting to revision (revert safer, needs revisions enabled); snapshots contain product data (sensitive, same handling as `.env`).

**Not implemented as stopgap:** state.json has `content_hash` and `gs1_link_set_hash` per (GTIN, language) — enough for change detection but not previous-value preservation. That's the gap Level C fills.

---

## 6. Idempotency contracts

**§6.2 has a precondition the hash cannot supply.** The dedup slug folds the SHA-256 of the
*local* bytes into itself, so it says what the attachment was meant to be, never what arrived. A
transfer cut off mid-upload leaves a fragment stored under the hash of the whole file, which every
later lookup then returns as a content match — making a truncated upload permanent rather than
transient. `upload_media` therefore compares the stored byte count (`media_details.filesize`,
falling back to a `HEAD` on `source_url`) against what it sent, on both the create path and the
dedup path. A create that disagrees is deleted and raises **before** the finalise call that would
claim the slug; a dedup hit that disagrees is deleted and re-uploaded. A size nobody will state is
logged as *unverified* and reused — deleting on a number that was never supplied would be deleting
on inference.

**Media has an ownership guard, and it is `meta.content_sha256`.** `delete_media` re-reads the
attachment and refuses unless that key is non-empty — the media counterpart to `_guard_gtin_match`
on pages (§6.1, E8/E11), and for the same reason: on a client's site most attachments are the client's own. Empty or unreadable meta counts as *not ours*, so the failure leans toward leaving an
orphan rather than deleting a stranger's file. `upload_media` returns `MediaUpload(media_id,
created)` so a caller can tell an attachment it added from one dedup handed it, which is what makes
rolling back a failed row safe; `run_execute` uses it to remove media uploaded by a row whose page
write never happened. There is deliberately no sweep for orphans from earlier runs — with no
ownership key beyond the hash, finding one would mean inferring it.


| # | Operation | Contract | Test |
|---|---|---|---|
| 6.1 | `wp_client.upsert_page` | Identical `(post_type, meta.gtin, title, content, language, featured_media)` → same server state, same `WordPressPage` returned | Call twice, assert same `id`; modify content, call again, assert `id` unchanged but content updated |
| 6.2 | `wp_client.upload_media` | Identical file content (by SHA-256) + title → single media asset, **and a stored byte count that agrees with what was sent** | Upload same file twice, assert same media_id; upload a file the server stores short, assert it is deleted and `MediaIntegrityError` raised |
| 6.3 | `gs1_dl_client.upsert` | Identical `(gtin, digital_link_url, links, is_enabled)` → identical server state | Call twice, GET afterward, assert single canonical state |
| 6.4 | `qr.render_qr` | Identical inputs → byte-identical SVG (visually identical PNG) | Render twice, hash both, assert equal |
| 6.5 | `run_execute.py` | Same confirmed plan twice → same final state as running once | Full integration test, run against test WP/GS1, run again, assert state.json unchanged |

---

## 7. Edge case inventory

| # | Input / condition | Expected behaviour | Where handled |
|---|---|---|---|
| E1 | GTIN with leading zeros (`"08712345678905"`) | Preserved; not silently stripped | `parse_excel_row` |
| E2 | GTIN as integer in Excel (openpyxl casts) | Coerced to string, zero-padded if needed | `parse_excel_row` |
| E3 | Duplicate GTINs in export | First occurrence wins; rest WARNING + skipped | `parse_export.py` |
| E4 | Empty Excel row | Skipped silently | `parse_export.py` |
| E5 | Excel row with GTIN but no `product_name` in default_language | `ExportParseError` with GTIN in message | `parse_excel_row` |
| E6 | Excel column mapped to a field ProductRecord doesn't have | `ExportParseError` at config load | `lib.config.load_clients` |
| E7 | `image_url` returns 404 or times out | Featured media skipped; page still created; RunOutcome notes missing image | `wp_client.upload_media` caller |
| E8 | WP page exists but its `meta.gtin` doesn't match row's GTIN | Log ERROR, skip row | `wp_client.upsert_page` |
| E9 | GS1 upsert succeeds but WP URL returned 500 later | State updated for GS1; WP failure logged; run continues | `run_execute.py` |
| E10 | Multilingual: NL succeeds, FR fails | State reflects NL; FR retried on next run | `run_execute.py` |
| E11 | Slug collision with existing non-GTIN page | Raise `WordPressAPIError`; require human intervention | `wp_client.upsert_page` |
| E12 | Template references `{{extras.foo}}` but `foo` not in `extras_columns` | Renders empty; WARNING once per run | `templates.py` |
| E13 | Product data contains `{{` or `}}` | Escape at insertion; use triple-brace `{{{ }}}` never | template author + doc |
| E14 | GS1 API returns 401 mid-run | Raise `GS1APIError`, mark row error, subsequent rows try again (may fail) | `run_execute.py` |
| E15 | `clients.yml` references env var not set | `MissingCredentialError` at first API call (lazy) | `config.resolve_key` |
| E16 | Excel has more columns than `column_map` | WARNING per unmapped column | `parse_export.py` |
| E17 | Excel has fewer columns than `column_map` expects | `ExportParseError` if required; WARNING if optional | `parse_export.py` |
| E18 | Language in `wordpress.languages` has no `product_name.{lang}` for a GTIN | Row for that language classified SKIPPED; noted in chat prompt | `run_plan.py` |
| E19 | State file corrupt / invalid JSON | Backup as `state.json.corrupt.{ts}`, start fresh, log ERROR, **and surface the reset in the plan summary** (see below) | `state.load_state` + `run_plan.py` |
| E20 | Two `run_execute.py` interleave for same client | Not supported. Document risk in troubleshooting.md. No lockfile in v0.1 | doc only |
| E21 | Generator configured but a **NEW or CHANGED** `(GTIN, language)` has no generated tagline (held, blank-1083 product) | Row SKIPPED from the plan so it can never publish a blank page, and dropped again at execute time (`run_execute._drop_without_copy`) because `--plan` confirms every row in a file. **Not applied to a links run** — `run_plan --links-only` keeps the unit and `run_execute --only links` keeps the row, since no page is rendered; a plan built that way is refused in any mode that writes pages. Asked **after** the classification: copy is written per run for the rows a run executes, so an UNCHANGED or HELD unit has none by design and reporting it as a skip turns a correct no-op into a work item. The gap is still reported via `missing_generation_input`, which fires only when 1083 is blank in **every** configured language — one the feed carries in another language is a pending translation, not a missing input | `state.diff_against_state` + `run_plan.py` + `run_execute.py` |
| E22 | `media.require_hero_image` set but a GTIN's source `image_url` is blank | GTIN held out of the plan so a hero-less page can never publish; still reported via `value_blank`. A runtime image fetch failure is unaffected (degrades per E7) | `state.diff_against_state` + `run_plan.py` |
| E23 | A declared source marked `required` — in `gdsn_map` **or** `gdsn_extras` — (or every member of a `required_group`) has no value for a product | **Whole GTIN held**, in every language, so a SKU is never half-published; each unit lands in `PlanDiff.skipped` with the missing attributes named, and the data-quality report's §0 coverage matrix and Summary row show them for the client to fill in MyGS1 | `lib.mandatory.missing_mandatory` + `state.diff_against_state` |
| E24 | `media.restrict_to_mapped_gtins` is set and a GTIN has no client-confirmed video in every language | **Whole GTIN held** and reported (§1b). Previously this narrowed *scope* instead, which made the gap invisible on every surface at once — the product simply vanished rather than appearing as work | `state.diff_against_state`, set supplied by `holds.confirmed_video_gtins` |

**E22/E23/E24 — enforced at plan time, but known before it.** All three drop a whole product and
none of them depends on state or on generated copy, so `lib.holds.held_units` can answer them from
config, the products and the video map alone. `run_generate` asks it and writes no copy for a held
product: on the pilot client that cut the producer calls by about three quarters. It calls the
same predicates `diff_against_state` calls — `mandatory.missing_mandatory`,
`media_video.fully_mapped_gtins`, the blank-`image_url` test — never its own reading of them, since
E23 decides whether a SKU may publish at all. E23 is asked of the *pre*-generation record, so it
ignores a gap `generator.translation_gaps` will close: holding one of those would leave a
publishable product with no copy, and E21 would then drop it silently. E18 and E21 themselves stay
out, being per unit and downstream of generation.

**E19 — why recovery is safe, and why it must still be loud.** State is a *cache* of what the
tool believes it already did, derivable from the live systems, so rebuilding it is safe: every
write path is idempotent (§6.1–§6.5). Without a known page id `wp_client.upsert_page` still
matches the live page by slug then `meta.gtin` and updates it in place (no duplicates),
`gs1_dl_client.safe_upsert` reads before it writes, and `qr.render_qr` is byte-deterministic.
So a reset costs redundant work, not corruption — which is why aborting the run would be the
wrong trade.

But a reset also *reclassifies every row as NEW*, silently converting an incremental re-run
into a full rewrite of live pages and resolver targets. An ERROR in the log is too quiet for
that: the operator is reading the chat, not stderr. So `load_state` returns the empty state
with `State.reset_from_corrupt` set (a load-scoped flag, excluded from serialisation),
`run_plan.py` leads its summary with a warning, and the flow-orchestrator surfaces it **above**
the §10.6.1 counts. The existing confirmation gate is what makes the reset safe in practice —
it only works if the operator is told.

An *unreadable* state file (permissions, I/O fault) is not this case: that is an environmental
fault and still raises `StateError` → exit 2.

---

## 8. Script contracts

### 8.1 `scripts/parse_export.py`

```
Usage: python -m scripts.parse_export CLIENT_ID [--dry-run] [--output PATH]

CLIENT_ID:    key in clients.yml
--dry-run:    validate mapping and report warnings; produce no output file
--output:     override default output/{client_id}/data/products.json

Exit codes:
  0  success
  1  parse errors
  2  config errors
```

Behaviour:
1. Load client config
2. Open Excel at `export.path`
3. Dispatch on `export.format`:
   - `flat` — read the header row; validate required targets; call `parse_excel_row` per row.
   - `gdsn` — `lib.gdsn.read_workbook` + `build_records` (join sheets by GTIN, §3.6). Multiple
     market rows for a GTIN are **aggregated** into one record, not treated as duplicates.
4. On any parse error, write nothing and exit 1.
5. Write `output/{client_id}/data/products.json` (bare JSON array) unless `--dry-run`
6. Print summary: `Parsed N products (M warnings)` to stderr

### 8.2 `scripts/run_plan.py`

```
Usage: python -m scripts.run_plan CLIENT_ID [--products PATH]

--products:   default output/{client_id}/data/products.json

Emits:  output/{client_id}/plan.json (a Plan as JSON)
        output/{client_id}/plan.summary.json (a PlanSummary as JSON, always)
Exit codes: 0 success, 2 config/state error
```

Behaviour:
1. Load client config, state, products
2. For each (product, language in client.languages):
   - Compute content hash, target URL
   - Compare against state
   - Emit `PlanRow` with classification and diff, or a `SkippedUnit` when E18/E21/E22 drops it.
     E18 is asked first (no title, no row); E21 is asked **after** the classification and only of
     a NEW or CHANGED row — see the E21 row in the edge table
3. Write `plan.json`
4. Write `plan.summary.json` — the gate exclusions, the skip tally, the E19 reset flag and the
   quarantine path, plus the summary line verbatim. Written unconditionally, so a missing file
   means the step did not run and an empty tally means it ran and found nothing.
5. Print summary: `N new, M unchanged, K changed`

### 8.3 `scripts/run_execute.py`

```
Usage: python -m scripts.run_execute CLIENT_ID [--plan PATH] [--confirmed PATH]
                                     [--only {pages,links}] [--dry-run] [--revive]
                                     [--i-understand-production]

If --confirmed given: use as ConfirmedPlan; else --plan with all rows confirmed.

--only selects one leg; omitting it does both (the pre-existing behaviour).
  pages  WordPress pages + translation linking. No GS1 record, no QR. Reversible.
  links  GS1 records + QR only, aimed at pages that already exist. Permanent.
Backs the /gs1-pages, /gs1-links and /gs1-publish skills, which supply it after gate 0;
operators do not type it.

--only links precondition: each target is resolved from state.json, else a slug lookup,
else the plan row's target_url, and must serve 2xx/3xx (wp_client.verify_url) before the
resolver is written. Any GTIN with an unverifiable target gets no GS1 write. In code,
not in skill prose: a GS1 record cannot be deleted, so a permanent target on a 404 is
unrecoverable, and prose can be skipped.

Production guard: a real run (not --dry-run) whose gs1.environment is 'production' is
refused unless --i-understand-production is passed (exit 2), in every mode. Keeps a bare
--plan from publishing live pages / permanent GS1 records; flow-orchestrator passes it
after its step-8 environment confirmation (gate 0 in pages mode, where step 8 is skipped).
The refusal message names only what the selected leg actually does.

Emits:  output/{client_id}/runs/{ts}.jsonl (RunOutcome per row)
        output/{client_id}/state.json (updated)
Exit codes: 0 all ok, 1 any errors, 2 config/setup error (incl. refused production run)
```

Per-row: try/except around each step (WP upsert, verify, GS1 upsert, QR render). JSONL log entry per row regardless. Full skeleton in `PROJECT_HANDOVER.md` §10.5.

State is committed per GTIN once every selected leg has succeeded, not per step — a resolver write that fails after the pages were upserted must leave no state, or the next run reads a fresh `content_hash` and never retries the link. A `--only pages` run stores `gs1_link_set_hash: ""`, which `lib.state._classify` reads as "page published, resolver link never written" and reports CHANGED; without it the follow-up links run would find every row UNCHANGED and publish nothing. A `--only links` run against a page this tool does not manage writes the resolver record but **no** state entry, since claiming a `content_hash` for content it never wrote would make the next run skip creating the page.

### 8.4 `scripts/verify_run.py` — **NOT BUILT; superseded**

Originally specified as a post-run sweep (`python -m scripts.verify_run CLIENT_ID [--run PATH]`)
that would HEAD the `wp_url` of every `RunOutcome` with `status == "ok"`. **This script does not
exist**, and it is not planned: verification moved *into* the execute loop instead. `run_execute`
calls `lib.wp_client.WordPressClient.verify_url` on each page immediately after upserting it
(`scripts/run_execute.py` → `_publish_page`), so a page that fails to serve is caught and recorded on
that row's `RunOutcome` during the run rather than in a separate pass afterwards. A row that was
created and *then* failed verification still reports its id and URL, so nothing is lost.

> **`verify_url` uses HEAD, and that is correct here** — it checks a WordPress page URL. Do **not**
> generalise it to Digital Link resolution: `id.gs1.org` **404s to HEAD and 307s to GET**, so resolver
> checks must use GET. See §12 Phase 9 and `docs/troubleshooting.md`.
>
> **A 2xx does not prove the page renders its content.** The ACF write path fails silently, so
> `verify_url` confirms the page is *served*, not that it is *correct*. Verifying a wave means
> fetching the HTML and inspecting it — see `docs/wordpress-onboarding.md`.

### 8.4a Scripts built beyond the original §8 set

These exist and are in active use but were never given §8 contracts. Flags below are transcribed
from each script's `_parse_args`; run any of them with `--help` for the authoritative list. Exit
codes are uniform — **0** success, **1** errors in the work, **2** config/usage error.

```
Usage: python -m scripts.run_generate  CLIENT_ID [--products PATH] [--results PATH]
                                       [--emit | --validate | --backend api]

--emit      Write the units an in-session producer must answer (default)
--validate  Check a session's results against this run; writes nothing
--backend api   Write the results file via the Anthropic API backend (lib/llm.py)

Emits:  output/{client_id}/data/generation_requests.json  (--emit)
        output/{client_id}/data/generation_results.json   (--backend api)
```

`--emit`/`--validate` and `--backend api` are mutually exclusive and share one contract seam:
`generation_results.json`, written fresh each run and read by `run_plan`. There is no cache, so
nothing is ever reused. What *is* narrowed is scope: of the in-scope products, only the
`(GTIN, language)` units a run would create or change are generated for (`preflight.units_needing_copy`
→ `state.classify_units`), because an UNCHANGED row is never confirmed and never executed — and of
those, only the ones whose product the plan will not hold outright (`holds.held_units` → E23/E24/E22,
the plan's own predicates). A unit is left out because nothing will be published for it, never
because copy for it exists. The in-session path needs no API key.

```
Usage: python -m scripts.run_unpublish CLIENT_ID --gtin GTIN [--gtin GTIN ...] [--dry-run]

Retracts each GTIN's Digital Link (PATCH isEnabled=false; links left intact) and drafts its
WordPress pages, then classifies the GTIN as HELD so a later run cannot republish it.
--revive on run_execute is the deliberate opt-in to publish held GTINs again.
```

```
Usage: python -m scripts.build_brick_map CLIENT_ID [--datamodel FILE.xlsx] [--code-column COL]
                                         [--category-column COL] [--sheet SHEET]
                                         [--products PATH] [--check]

Drafts a client's GPC brick → category map from the GS1 DIY sector datamodel (operator-supplied).
--check is a coverage gate: exit 1 if any brick in the export is unmapped. Unmapped bricks warn;
the tool never guesses a category. Per-GTIN exceptions go in categories.overrides.
```

```
Usage: python -m scripts.build_video_map CLIENT_ID [--products PATH] [--check]

Emits a hint skeleton for the operator-authored video filename → GTIN mapping (videos are not in
the feed; files are named by marketing name). --check exits 1 if any file is unmapped.
The mapping requires client sign-off — see §12 Phase 9.5.
```

```
Usage: python -m scripts.report_video_candidates CLIENT_ID [--top-n N] [--format csv|xlsx]
                                                 [--out PATH] [--products PATH]

Emits: output/{client_id}/video-map-candidates.{csv,xlsx}   (pure rows: lib/video_candidates.py)

One row per (language, video file) over the **union** of the folders and the mapping, with each
row's state, its current GTIN, that product's names, and the top N ranked candidates — each with
the value that scored and the field it came from. A report to send to the client, not a gate:
it never exits non-zero over an unmapped file. `build_video_map --check` is the gate.
```

```
Usage: python -m scripts.report_quality CLIENT_ID [--out PATH]

Emits: output/{client_id}/data-quality-report.md   (pure renderer: lib/quality_report.py)
```

Run `report_quality` after `parse_export`, `run_plan`, or `build_video_map` — it renders whatever
those steps last wrote. It is the surface for finding source-data problems to fix in MyGS1 rather
than working around downstream.

### 8.5 `scripts/inspect_export.py`

Utility for onboarding.

```
Usage: python -m scripts.inspect_export EXCEL_PATH

Prints (GDSN exports, §3.6):
  - each worksheet's attributes: label, GDSN attribute id, per-language flag,
    languages present, first 3 sample values
  - a suggested `export` block with a `gdsn_map` for the recognised product-page
    attributes (3297 → product_name, 3336 → brand, 2485 → image_url, …)
```

---

## 9. MCP tool contracts

### 9.1 `gs1-nl-mcp` tools

Input schemas mirror the v2 API `CreateOrUpdateRequest` body but hide plumbing (`accountNumber`, `resolverSettings`, OAuth2 credentials) — the MCP wrapper resolves those from `clients.yml` by `client_id` and mints the token itself.

```yaml
- name: gs1_digital_link_upsert
  description: Set or update the resolver target for one GTIN via v2 API.
  input_schema:
    type: object
    required: [client_id, gtin, item_description, links]
    properties:
      client_id: { type: string }
      gtin: { type: string, pattern: "^[0-9]{8,14}$" }
      item_description: { type: string }
      is_enabled: { type: boolean, default: true }
      links:
        type: array
        items:
          type: object
          required: [link_type, language, link_title, target_url, default_link_type, public, media_type]
          properties:
            link_type: { type: string }              # e.g. "pip", "gs1:productInfo"
            language: { type: string }               # ISO 639-1
            link_title: { type: string }
            target_url: { type: string, format: uri }
            default_link_type: { type: boolean }
            public: { type: boolean }
            media_type: { type: string }             # e.g. "text/html" — required in v2
      application_identifiers:
        type: array
        default: []
        items:
          type: object
          required: [identifier, template_variable]
          properties:
            identifier: { type: string }
            template_variable: { type: string }
  output:
    ok: boolean
    error: string | null

- name: gs1_digital_link_upsert_bulk
  description: Bulk variant. Batches into groups of batch_size internally.
  input_schema:
    type: object
    required: [client_id, entries]
    properties:
      client_id: { type: string }
      entries:
        type: array
        items:
          # Same shape as single upsert input, minus client_id
          type: object

- name: gs1_digital_link_get
  description: Fetch current Digital Link entry for a GTIN. Returns null if not found.
  input_schema:
    type: object
    required: [client_id, gtin]
    properties:
      client_id: { type: string }
      gtin: { type: string, pattern: "^[0-9]{8,14}$" }
  # GET https://{host}/digitallinkv2/v2/digitalLink/01/{gtin14}  (AI "01", not "Gtin")
  # Response = AdvancedDigitalLinkResponse (see PROJECT_HANDOVER §4.2).
  # Not-found (confirmed Phase 2) = 400 "No valid contract found for Gtin with id: {gtin}" -> null.
```

### 9.2 `wordpress-mcp` tools

Names: `wp_upsert_page`, `wp_upload_media`, `wp_find_by_slug`, `wp_verify_url`, `wp_detect_multilingual`. Input/output shapes mirror the lib functions in §4.4.

### 9.3 `qr-render-mcp` tools

```yaml
- name: qr_render
  description: Render a QR symbol for a Digital Link URI.
  input_schema:
    required: [uri, output_dir, gtin, formats, size_mm, error_correction]
    properties:
      uri: { type: string }
      output_dir: { type: string }
      gtin: { type: string }
      formats: { type: array, items: { enum: [svg, png, eps] } }
      size_mm: { type: integer }
      error_correction: { type: string, enum: [L, M, Q, H] }
```

---

## 10. Skills — SKILL.md skeletons

Each skill in `.claude/skills/{name}/SKILL.md`. Common structure:

```markdown
# {Skill Name}

## When to load
{Trigger phrases and situations.}

## What this skill does
{One paragraph.}

## Inputs
{What Claude needs in context.}

## Steps
1. …

## MCP tools used
{Names.}

## Failure modes
{Common problems + handling.}
```

### 10.1 `.claude/skills/gs1-export-parser/SKILL.md`
- Trigger: "parse the export" or user drops an .xlsx in chat
- Steps: identify `client_id` (ask if unclear), run `scripts/parse_export.py`, summarise counts and warnings

### 10.2 `.claude/skills/wordpress-product-page/SKILL.md`
- Trigger: mentioning WP page creation/update
- Steps: verify template exists, verify multilingual plugin detected, render, upsert, verify_url

### 10.3 `.claude/skills/gs1-digital-link/SKILL.md`
- Trigger: setting resolver targets
- Steps: build payload from `ProductRecord` + config, call bulk upsert

### 10.4 `.claude/skills/qr-render/SKILL.md`
- Trigger: generating QR files
- Steps: build URI, call render, present output paths

### 10.5 `.claude/skills/flow-orchestrator/SKILL.md`
- Trigger: "publish {client} to GS1" (preferred), "run the GS1 pipeline for {client}"; "run for {client}" / "process {client}" kept as short forms
- Steps: parse → plan → present diff → collect confirmation → execute → summarise

Full body: TBD during Phase 8; skeletons enough for now.

### 10.6 Chat interaction patterns for flow-orchestrator

Style: **concise, business-like**. Not conversational. Verbose text creates fatigue during batch runs.

#### 10.6.1 Plan summary presentation

After `run_plan.py`:

```
Plan for democlient (test env):
  New:       38
  Unchanged:  7
  Changed:    2

Proceed with all 40 to execute?
[all | new-only | changed-review | cancel]
```

- `all` — confirm every row; execute
- `new-only` — confirm NEW rows only, skip CHANGED
- `changed-review` — walk each CHANGED row's diff and confirm individually
- `cancel` — abort

Off-menu reply → "Please pick one of the listed options, or specify a filter (e.g. 'only GTIN 87123...')."

#### 10.6.2 Per-row diff for changed rows

```
GTIN 8712345678905 (nl) — Cable Organiser Pro
Changes:
  title:      "Cable Organiser" → "Cable Organiser Pro"
  target_url: /democlient/cable-organiser/ → /democlient/cable-organiser-pro/

[apply | skip | show-full-diff]
```

`show-full-diff` prints all fields, re-prompts `[apply | skip]`.

#### 10.6.3 Execute progress

Every 10 rows (runs >20), otherwise only at end:

```
Progress: 10/40 rows processed. 10 ok, 0 error, 0 skipped.
```

Not per-row. Per-row output → JSONL log.

#### 10.6.4 Post-execute summary

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

- `yes` — re-run execute filtered to failed GTINs
- `no` — done
- `detail` — read JSONL entries, explain each

#### 10.6.5 Missing-field handling during plan

```
GTIN 8712345678905 is missing `product_name_fr` (required for language fr).
[skip-row | ask-me-later | fail-run]
```

- `skip-row` — this (GTIN, lang) is SKIPPED; other languages proceed
- `ask-me-later` — batch prompts, present at end
- `fail-run` — abort

`clients.yml` default: `flow.on_missing_field: prompt`.

#### 10.6.6 Language selection

```
Client democlient supports [nl, fr]. Which languages should this run cover?
[all | nl | fr | nl,fr]
```

Default: `all`. Subset filters the plan and summary accordingly.

#### 10.6.7 Environment confirmation

Before every production run:

```
About to execute against PRODUCTION environment (gs1nl-api.gs1.nl).
This will make live changes to https://www.democlient.nl.
Continue?
[confirm | switch-to-test | cancel]
```

Mandatory (`flow.on_production_run: prompt` in `clients.yml`, non-overridable). Confirmation is per-run, not per-session.

---

## 11. Test fixture requirements

### 11.1 What we can build without real data

Unit tests for: Pydantic type validation, `parse_excel_row` with synthetic rows, `qr.render_qr` (deterministic), `state.compute_content_hash`, `state.diff_against_state`, `templates.TemplateEngine.render` with synthetic ProductRecord, retry logic in HTTP clients via `pytest-httpx` mocking.

### 11.2 What needs real data (TODO)

**TODO — needs pilot client's real MyGS1 export:** at `tests/fixtures/pilot_export.xlsx`. Used for `parse_export.py` integration tests, `inspect_export.py` output verification, column-map validation tests.

**TODO — needs real GS1 NL API responses:** captured in `tests/fixtures/gs1_api/`:
- `get_existing_gtin.json` — GET for a GTIN that exists
- `get_missing_gtin.json` — GET for non-existent GTIN
- `post_upsert_success.json` — POST create response
- `post_upsert_update.json` — POST update response
- `post_400_missing_field.json` — deliberate error
- `post_401.json` — bad key

Used for response-shape parsing tests, error handling tests, mocking in `pytest-httpx`.

**TODO — needs staging WordPress:** with pilot client's actual post type (`show_in_rest: true`), Polylang configured, one test category term, automation-user application password.

### 11.3 Fixture directory layout

```
tests/
├── fixtures/
│   ├── pilot_export.xlsx         # TODO
│   ├── pilot_export_expected.json
│   ├── gs1_api/                  # TODO
│   ├── wp_api/
│   │   ├── page_create_response.json    # synthesised
│   │   └── ...
│   └── templates/
│       └── minimal.html
├── lib/
│   ├── test_config.py
│   ├── test_records.py
│   ├── test_gs1_dl_client.py
│   └── ...
└── scripts/
    ├── test_parse_export.py
    └── ...
```

---

## 12. Definition of Done per phase

### Phase 1 — Repo skeleton
- [ ] `ruff check` passes zero warnings
- [ ] `mypy --strict lib` passes
- [ ] `pytest` runs (may pass with zero tests)
- [ ] GitHub Actions workflow committed and green on push
- [ ] `README.md` links to `PROJECT_HANDOVER.md` and this doc

### Phase 2 — GS1 Digital Link client + MCP
- [ ] All §6.3 idempotency contracts tested green
- [ ] Retry logic (§4.3) tested via `pytest-httpx` with mocked 429 and 5xx
- [ ] PII scrubbing verified: unit test asserts secrets not in log output
- [ ] Real test-env call returns expected shape
- [ ] MCP tool callable, returns success for one real GTIN

### Phase 3 — Excel parser + records schema
- [ ] All §2 types defined + validation tests
- [ ] Every edge case §7 (E1–E6, E16–E17) has a unit test. E7 (image 404) is handled in
      `wp_client.upload_media`'s caller and is **deferred to Phase 4** (per §7 routing).
- [ ] `inspect_export.py` runs against pilot export, produces a suggested mapping
      (`gdsn_map` for GDSN exports, `column_map` for flat)
- [ ] `parse_export.py {client}` produces `output/{client}/data/products.json` with zero
      warnings (pilot: over a hundred products, nl + fr)
- [ ] Round-trip: `ProductRecord → JSON → ProductRecord` preserves all fields
- [ ] Spec/schema/`clients.yml` document the GDSN format (§3.6); `lib/config.py` present

### Phase 4 — WordPress client + MCP
- [ ] §6.1 and §6.2 idempotency tested against staging WP
- [ ] Multilingual detection returns correct value on Polylang staging
- [ ] Edge cases E7, E8, E11 covered

### Phase 5 — QR + templates
- [ ] §6.4 idempotency tested
- [ ] Rendered QR at 20mm scans with iOS and Android
- [ ] Template override resolution tested
- [ ] Missing template raises `TemplateError` cleanly

### Phase 6 — lib, scripts, state
- [ ] `run_execute.py` completes for one GTIN end-to-end against staging
- [ ] §6.5 idempotency tested
- [ ] State file atomicity: kill mid-write, verify no corruption

### Phase 7 — Re-run and change detection
- [x] Change classification correctness tested for all edge cases
- [x] Chat-format diff readable and unambiguous, matches §10.6

> **Moved to Phase 8:** "Full re-run flow tested in fresh Claude Code session" was a Phase 7 item, but
> it duplicates Phase 8's own exit gate and cannot be met before it. When it was moved, only
> `flow-orchestrator` and `content-generator` had a SKILL.md; the other four skills were empty
> stubs, and step 1 of the flow delegates parsing to `gs1-export-parser`. A Claude Code test in Phase 7
> would have exercised one-fifth of the surface it is meant to validate. (All six SKILL.md are now
> finalised as of Phase 8, 2026-07-19.) Tracked below as Phase 8's "Full re-run flow (plan → diff →
> confirm → execute) in a fresh Claude Code session".

### Page adapter (pilot) — mapping, data quality, lifecycle
Cross-cuts Phases 6–9; it was built for the pilot client's page model and does not fit one numbered gate.
- [x] Field mapping resolved *with the client* (a field walk), slot semantics verified live
- [x] Ranked `market_priority` replaced a 1:1 `market_language` map — every market row carries
      every language, so the map both mis-resolved and undercounted
- [x] Source-data report emits `value_blank` + `value_inconsistent_across_markets` (per-field
      `report_issues` gate; scoped to published fields)
- [x] Unpublish lifecycle: `scripts/run_unpublish.py` (retract GS1 → draft pages → `HELD` so a run
      never republishes; reversible via `run_execute --revive`), verified live
- [x] Feature/benefit + tagline **generator** (LLM) — `lib/generator.py` (fingerprint cache,
      request/result contract, `merge_generated`), the `run_generate` spine, both producers behind one
      cache seam — the in-session `content-generator` skill and the headless `--backend api`
      (`lib/llm.py`) — the `run_plan` merge, the wired `acf_map`, and `generated_issues.json`
- [x] `net_content` unit-code → functional-name decoding — `reference/measurement_units.json`
      (the datamodel's `MeasurementUnitCode_GDSN` picklist → nl/en/fr) + `lib/units.py`
      (`decode_net_content`), decoded per language at render time in `templates._build_context`
- [ ] Brand-typo report — deferred with the report's scope, to widen past published fields later.

### Phase 7.5 — GPC brick → category mapping
Derive the product-category assignment from the **GS1 DIY sector datamodel**, since GPC bricks do
not map 1:1 onto a client's marketing categories. **The operator supplies the DIY datamodel** at the
start of the phase (like the export and control file).
- [x] DIY datamodel supplied by the operator and parsed (`load_diy_datamodel`)
- [x] Every GPC brick present in the client export maps to a category term — `build_brick_map --check` is green
- [x] Bricks that span categories are resolved by a per-GTIN override list
- [x] `brick_category_map` + overrides live in `clients.yml`, reviewed and signed off by the client
- [x] `run_plan` assigns the correct category for every planned product; unmapped bricks warn rather
      than guess; assignment precedes hashing so a category change classifies CHANGED

> **Done.** Tool layer: `CategoryConfig` + schema, `lib/categories.py` (resolver, coverage,
> DIY-datamodel parser, draft generator), `scripts/build_brick_map.py`, and the `run_plan` wiring,
> all test-covered. The signed-off map lives in the gitignored `clients.yml`.

### Phase 8 — Skills
- [x] Each SKILL.md finalised per §10
- [x] Full flow via chat instruction works end-to-end
- [x] Skills load when expected trigger phrases used
- [x] Full re-run flow (plan → diff → confirm → execute) in a fresh Claude Code session *(moved from
      Phase 7; see the note there)*. The plan half is already exercisable on real data — both
      operator files are in `input/{client_id}/` — so this gate is about the chat surface and the
      execute leg, not the data. **The chat surface (parse → generate → plan → confirm) is
      validated; the execute leg was proven in the Phase 9 pilot. The full plan → diff → confirm →
      execute loop — including the per-row diff gate (§10.6.2) — was walked end-to-end via
      `flow-orchestrator` in the Phase 9.8 validation (2026-07-30); see Phase 9.8 status below.**

> **Status:** All 6 skills finalised, each a documentation wrapper over code that already works
> (`scripts/`, `lib/`, `mcps/`), grounded in the real flags, output paths, and exit codes of what it
> wraps. The end-to-end chat flow (parse → generate → review gate 1 → plan → review gate 2 →
> confirm) was driven on the pilot client's real export; every §10.6 gate presented correctly, and
> each skill loads on its documented trigger phrase. The **execute** leg was left for the Phase 9
> pilot, because validating QR resolution needs a real GTIN.

### Phase 9 — Pilot end-to-end
- [x] ≥10 real products live on pilot WP staging → production
- [x] Every printed QR sample scans and resolves correctly _(all 10 resolve `GET id.gs1.org/01/<gtin>` → 307 → 200; physical phone-scan of a printed sample confirmed working 2026-07-28)_
- [x] No manual corrections needed during the run _(every wave ran 0-error; verification was read-only)_

> The deferred **execute + resolution leg is proven**: the first real GTIN was published live nl+fr,
> renders its ACF content on the public pages, is registered on GS1 production, and its QR resolves:
> `GET https://id.gs1.org/01/{gtin14}` → 307 → nl page → 200. (Gotcha: the resolver **404s to HEAD
> but 307s to GET** — test resolution with GET.) Two items were split out: **media (Phase 9.5)** and
> the **fr-QR strategy**.

### Phase 9.5 — Media (images + video)
- [ ] Product-name → GTIN mapping for the video files built and **client-confirmed** (per language)
- [x] Product images downloaded from the export `image_url`s, uploaded to WP, and rendering on pilot pages
- [ ] NL + FR videos matched via the mapping, uploaded, and set on the correct-language page
- [x] Media wired into `run_execute` (replaces `featured_media=None`); re-runs stay idempotent

> **Images** come from the export `image_url`; print-master TIFFs that WordPress rejects are
> converted (`lib/media.convert_image_for_web`: TIFF/PNG → web JPEG, ~1600px, deterministic).
> **Videos** are **not** in the feed; the operator supplies per-language folders, and the
> name→GTIN mapping is **operator-authored**: `scripts/build_video_map.py` emits a hint skeleton
> (`--check` gates coverage); `.mpg`/`.mpeg` sources are transcoded to H.264 MP4 via ffmpeg.
> `run_execute._row_media` injects the hero image and the language's video into the ACF dict at
> execute time; the hero id is persisted in state.
>
> **Two live findings:** the image ACF write-shape is an **attachment id** (not a URL); and media
> re-runs were **not** idempotent until fixed — the `content_sha256` dedup meta is silently dropped
> on attachments unless registered in REST. The media slug is now **content-addressed**
> (`{base}-{sha12}`): dedup is a pure slug lookup, needing no meta and immune to squatting.

### Phase 9.8 — Operator flow validated under Claude Code
- [x] `flow-orchestrator` driven end-to-end from a **fresh Claude Code session** on ≥1 GTIN (draft-first)
- [x] Every operator gate exercised and confirmed correct: language select → review gate #1 (generated
      copy) → plan-review gate #2 → **production environment-confirmation gate** (`[confirm | switch-to-test
      | cancel]`) → execute → progress → post-execute summary → retry
- [x] Operator **guided step-by-step** at each gate — each verbatim prompt presented and its off-menu reply
      handled; the operator confirms at every gate, nothing auto-proceeds
- [x] Ticks the open **Phase 8 DoD box #4** (full re-run flow plan → diff → confirm → execute in a fresh
      Claude Code session)

> **Status: validated.** `flow-orchestrator` was driven end-to-end in a Claude Code chat with the
> operator answering each gate, on a **reversible dry-run harness** (one NEW row and one CHANGED row
> forced by staling a state entry). Every gate rendered verbatim and was confirmed by the operator,
> through to `--dry-run` execute and the post-execute summary. Nothing was written to WordPress or
> GS1, and the harness was torn down with `state.json` verified byte-identical to its backup.
>
> Split out 2026-07-19 to make explicit what Phase 9's smoke did NOT cover. The execute leg was proven by
> invoking `scripts/run_execute.py` **directly**, which bypasses the entire operator UX: the language
> prompt, both review gates, the mandatory production environment-confirmation gate, progress lines, the
> post-execute summary, and the retry prompt. This phase validates that whole experience through the
> `flow-orchestrator` skill under Claude Code — and the operator is to be **walked through each gate one step at a
> time** (present the gate, wait for the operator's choice, then proceed), never batching or auto-confirming.
> Generation stays on the in-session producer (no API key required). Placed at 9.8 (after media, before
> the ≥10 batch) so the batch is driven through the validated operator flow, not raw scripts.

### Phase 10 — Docs
- [x] Setup steps executed by unfamiliar person succeed _(executed verbatim from a fresh clone,
      2026-07-30 — see status below)_
- [x] Every skill and script has a docstring _(31 modules, 0 gaps; all 6 SKILL.md carry
      `name` + `description`)_
- [x] `troubleshooting.md` covers each error type in §4.1 _(all **13** `lib/errors.py` classes — the 8
      §4.1 names plus the 5 added since — the E1–E22 inventory, and the live-pilot traps)_

> **Status (2026-07-30): complete.** The seven documents `PROJECT_HANDOVER.md` §8.2 assigns to this
> phase now exist in `docs/`: `setup.md`, `troubleshooting.md`, `gs1-nl-onboarding.md`,
> `wordpress-onboarding.md`, `data-source-export-schema.md`, `template-variables.md`, `costs.md`.
> `README.md` was rewritten — it had been announcing *"v0.0.1 — Phase 1 (repository skeleton). Not yet
> functional"* while 10 products were live.
>
> **Everything is derived from the code at HEAD, not from the planning documents** — the modules, each
> script's `_parse_args`, the `lib/config.py` Pydantic models, `.env.example`, and `ci.yml`. Where a
> spec section contradicted the code, the code won and the spec was corrected: §8.4 specified
> `scripts/verify_run.py`, which does not exist (verification lives in `run_execute` via
> `wp_client.verify_url`) and §8 had no contract for the five scripts that were built instead (now
> §8.4a); §4.5 called `WPMLAdapter` a *"stub raises `NotImplementedError`, v0.2"* when it is
> implemented and has been publishing live nl+fr pages; §4.1 listed 8 exception classes when there are 13.
>
> **Box 1 was proven by execution, not inspection.** `docs/setup.md` was run verbatim from a fresh
> clone in a clean venv: `pip install -e ".[dev]"` → `ruff check` → `ruff format --check` →
> `mypy --strict lib` → `pytest`, then
> `clients.example.yml` loading unedited, `--help` on all nine documented scripts, and the read-only
> onboarding leg (`inspect_export` → `parse_export --dry-run`) against the
> real export. The safety claims were exercised too: a real run against `environment: production` is
> **refused with exit 2**, and `--dry-run` bypasses the guard as documented.
>
> That run found a real defect and it was fixed rather than documented around: `setup.md` tells
> operators every script answers `--help`, but `inspect_export` takes a bare path with no argparse, so
> `--help` reached openpyxl and raised `InvalidFileException` — which is **not** an `OSError`, so the
> existing handler missed it and the script died with an unhandled traceback (the same for any typo'd
> path or a real `.xls`). Now handled, with tests.
>
> **Deferred:** nothing in this phase. The five other docs named in `PROJECT_HANDOVER.md` §7's tree
> (`docs/setup.md` etc.) are all present; the handover's separate `costs.md` cross-reference to GS1
> tariffs points at the GS1 NL price page, which should be re-checked at release.

### Phase 11 — Release
- [x] Version bumped in `pyproject.toml` and `package.json` — `0.0.1` → `0.1.0`, also applied to
      the three `mcps/*/package.json` workspace members and `package-lock.json`.
- [x] `CHANGELOG.md` populated — `[Unreleased]` was 92 commits stale (last touched 2026-07-18), so
      Phases 8, 9, 9.5, 9.8, 10 and the generator were reconstructed from the log and the section
      promoted to `[0.1.0]`. The footer `[0.0.1]` link pointed at a release tag that never existed;
      it now points at the commit that set the version.
- [x] Git tag `v0.1.0` pushed
- [ ] ~~MCP registry entry submitted~~ — **will not be done. Decided 2026-07-31: the three
      MCP servers stay private.** This box stays unticked **by choice, not as outstanding work.**
      Submission requires the npm packages to be published and publicly resolvable (ownership is
      verified by reading the published `package.json` for an `mcpName` matching `server.json`), and
      all three are `"private": true`. The `server.json` files are written, committed at
      `mcps/*/server.json`, and schema-valid, so the decision is cheap to reverse.
- [x] Announcement drafted.

---

## 13. Data-gathering plan (to execute later)

### 13.1 Get a real MyGS1 export (blocks Phase 3)

**Prerequisites:** Pilot client cooperating; MyGS1 credentials available.

**Steps:**

1. Log in to MyGS1 (`https://mijn-v2.gs1.nl`) as the pilot client
2. Navigate to **My codes** (Mijn codes)
3. Select "Export" — choose Excel (.xlsx)
4. If filters apply, consider exporting **without filters** first to see all columns
5. Save as `input/{client_id}/products.xlsx`
6. If client is sensitive: ask for 10-row sample or anonymise
7. Run `python -m scripts.inspect_export input/{client_id}/products.xlsx`
8. Take suggested `column_map` and refine
9. Copy final map into `clients.yml`
10. `python -m scripts.parse_export --dry-run` — iterate until zero warnings on required fields

**Output:** Excel at `input/{client_id}/products.xlsx` + populated `column_map` + zero-warning dry-run.

### 13.2 Capture GS1 API v2 responses (blocks Phase 2 completion)

**Prerequisites:** OAuth2 **client id + client secret** for the sandbox in `.env`, and — critically — a **Digital Link contract** on the account (without it, writes return `400 21011 "No valid contract found."`; a not-yet-provisioned contract is a GS1-side blocker).

```bash
export CLIENT_ID=<sandbox client id>       # from MyGS1 / developer portal
export CLIENT_SECRET=<sandbox client secret>
export HOST=gs1nl-api-acc.gs1.nl
```

**Auth (confirmed OAuth2 client-credentials):** mint a JWT, then use it as a Bearer token. `accountNumber` comes from the token's own claim.
```bash
# Mint the access token (lowercase client_id / client_secret headers):
TOKEN=$(curl -s -X POST -H "client_id: $CLIENT_ID" -H "client_secret: $CLIENT_SECRET" \
  "https://$HOST/authorization/token" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
export AUTH_HEADER="Authorization: Bearer $TOKEN"
# accountNumber is in the JWT payload (base64 middle segment) -> accountNumber claim.
export ACCOUNT_NUMBER=<accountNumber claim from the token>
export TEST_GTIN=<a GTIN under that account with a Digital Link contract, 14 digits>
```

A ready-to-run helper (`capture_gs1_oauth.sh`) that mints the token, detects the scheme, and writes all six fixtures was used in Phase 2; it is not shipped.

Run six commands (five capture calls plus a GET when its endpoint is known):

```bash
mkdir -p tests/fixtures/gs1_api

# 1. POST single upsert — successful create/update
curl -X POST -H "$AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -o tests/fixtures/gs1_api/post_success.json \
  -w "%{http_code}\n" \
  -d '{
    "accountNumber": "'$ACCOUNT_NUMBER'",
    "identificationKeyType": "Gtin",
    "identificationKey": "'$TEST_GTIN'",
    "isEnabled": true,
    "itemDescription": "Fixture: test product",
    "resolverSettings": {"useGS1Resolver": true},
    "links": [{
      "linkType": "pip",
      "language": "nl",
      "linkTitle": "Product page",
      "targetUrl": "https://example.com/p/'$TEST_GTIN'",
      "defaultLinkType": true,
      "public": true,
      "mediaType": "text/html"
    }],
    "applicationIdentifiers": []
  }' \
  "https://$HOST/digitallinkv2/v2/digitallink"

# 2. POST single upsert — deliberate 400 (missing required accountNumber)
curl -X POST -H "$AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -o tests/fixtures/gs1_api/post_400.json \
  -w "%{http_code}\n" \
  -d '{
    "identificationKeyType": "Gtin",
    "identificationKey": "'$TEST_GTIN'"
  }' \
  "https://$HOST/digitallinkv2/v2/digitallink"

# 3. POST single upsert — deliberate 401 (the token with a stray character, so it is wrong)
curl -X POST -H "Authorization: Bearer ${TOKEN}x" \
  -H "Content-Type: application/json" \
  -o tests/fixtures/gs1_api/post_401.json \
  -w "%{http_code}\n" \
  -d '{
    "accountNumber": "'$ACCOUNT_NUMBER'",
    "identificationKeyType": "Gtin",
    "identificationKey": "'$TEST_GTIN'",
    "isEnabled": true,
    "itemDescription": "unauth test",
    "resolverSettings": {"useGS1Resolver": true},
    "links": [],
    "applicationIdentifiers": []
  }' \
  "https://$HOST/digitallinkv2/v2/digitallink"

# 4. POST bulk upsert — array of two entries
curl -X POST -H "$AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -o tests/fixtures/gs1_api/post_bulk_success.json \
  -w "%{http_code}\n" \
  -d '[
    {
      "accountNumber": "'$ACCOUNT_NUMBER'",
      "identificationKeyType": "Gtin",
      "identificationKey": "'$TEST_GTIN'",
      "isEnabled": true,
      "itemDescription": "Bulk fixture 1",
      "resolverSettings": {"useGS1Resolver": true},
      "links": [{
        "linkType": "pip",
        "language": "nl",
        "linkTitle": "Product",
        "targetUrl": "https://example.com/1",
        "defaultLinkType": true,
        "public": true,
        "mediaType": "text/html"
      }],
      "applicationIdentifiers": []
    },
    {
      "accountNumber": "'$ACCOUNT_NUMBER'",
      "identificationKeyType": "Gtin",
      "identificationKey": "'$TEST_GTIN'",
      "isEnabled": true,
      "itemDescription": "Bulk fixture 2",
      "resolverSettings": {"useGS1Resolver": true},
      "links": [{
        "linkType": "pip",
        "language": "fr",
        "linkTitle": "Product",
        "targetUrl": "https://example.com/2",
        "defaultLinkType": false,
        "public": true,
        "mediaType": "text/html"
      }],
      "applicationIdentifiers": []
    }
  ]' \
  "https://$HOST/digitallinkv2/v2/digitallinks"

# 5. GET existing GTIN — path segment is the GTIN AI "01" (NOT "Gtin"); capital-L digitalLink
curl -H "$AUTH_HEADER" \
  -o tests/fixtures/gs1_api/get_existing.json \
  -w "%{http_code}\n" \
  "https://$HOST/digitallinkv2/v2/digitalLink/01/$TEST_GTIN"

# 6. GET non-existent GTIN — confirmed not-found = 400 with body
#    "No valid contract found for Gtin with id: {gtin}" (NOT 404).
curl -H "$AUTH_HEADER" \
  -o tests/fixtures/gs1_api/get_missing.json \
  -w "%{http_code}\n" \
  "https://$HOST/digitallinkv2/v2/digitalLink/01/00000000000000"
```

Commit a `README.md` in `tests/fixtures/gs1_api/` documenting what each response represents, the OAuth2 token flow used (mint → Bearer JWT), and — critical — the confirmed not-found behaviour (`400` with `"No valid contract found for Gtin with id: …"`).

**Output:** Six fixture files (post_success, post_400, post_401, post_bulk_success, get_existing, get_missing) plus README.

**Use in code:** Load fixtures in `pytest-httpx` mocks; parse them to derive actual response shape. If response shapes differ from v2 schemas assumed in §2 types, update the types.

**Also record:** the per-environment `accountNumber` (from the token's `accountNumber` claim) into `clients.yml` as `account_number_test` / `account_number_production` — these differ per environment and are the values that must be right before a successful create.

### 13.3 Set up staging WordPress (blocks Phase 4 completion)

**Prerequisites:** Pilot client has a staging WP; you have admin access.

**Full onboarding checklist:** see `PROJECT_HANDOVER.md` §5.4 for the complete fifteen-item WordPress setup reference. This section covers only developer-verification curl commands.

**Steps:**

1. Polylang installed and configured for NL + FR minimum
2. WP admin → Users, create `gs1-publisher` with Editor role
3. Generate application password `gs1-orchestrator`
4. Confirm custom post type registered (`/wp-json/wp/v2/types`)
5. If missing: add `'show_in_rest' => true` to `register_post_type` in the theme's `functions.php` or a plugin
6. Set `DEMOCLIENT_WP_APP_PASS` env var
7. Verify types:
   ```bash
   curl -u "gs1-publisher:$DEMOCLIENT_WP_APP_PASS" \
     https://staging.democlient.nl/wp-json/wp/v2/types
   ```
   Custom post type must appear
8. Test create:
   ```bash
   curl -u "gs1-publisher:$DEMOCLIENT_WP_APP_PASS" \
     -H "Content-Type: application/json" \
     -X POST \
     -d '{"title": "Test", "status": "draft", "content": "test"}' \
     https://staging.democlient.nl/wp-json/wp/v2/{post_type}
   ```
   Expect 201; note returned `id`; delete manually via admin

**Output:** Staging URL, credentials, verified access.

---

## 14. Document metadata

- **Version:** 0.4
- **Companion documents:** `PROJECT_HANDOVER.md` (context and decisions)
- **Owners:** Same as PROJECT_HANDOVER

### Change log
- **0.4 (2026-07-11):** **Auth model corrected to OAuth2 client-credentials** (empirically confirmed against the live acceptance host in Phase 2). §4.3 `_auth_header()`/`auth_scheme` replaced by `_mint_token()`/`_get_token()` (mint a 1h Bearer JWT from `client_id`/`client_secret` at `POST /authorization/token`, cache, refresh on 401). §9.1 wrapper mints its own token. §13.2 capture rewritten to mint-then-call; not-found confirmed `404` empty body; `accountNumber` is per-environment (from the token claim). `GS1Config`/`clients.yml` schema gained `client_id_env_*`/`client_secret_env_*` and `account_number_*`.
- **0.3 (2026-07-04):** Rewritten for **Digital Link API v2** across §4.3, §9.1, §13.2. Key changes:
  - §4.3 `lib/gs1_dl_client.py` method signatures updated for v2 body (`accountNumber`, `identificationKeyType`, `identificationKey`, `resolverSettings`, `mediaType` in links, `applicationIdentifiers`). New `_auth_header()` builds `Authorization: Bearer <token>` (or raw) based on `config.auth_scheme`. Old `Ocp-Apim-Subscription-Key` header removed. Token-scrubbing note added.
  - §9.1 MCP tool schemas mirror v2 body: `item_description` promoted to required, `links[].media_type` added as required, `application_identifiers` array optional. Removed `digital_link_url` (replaced by structured `identificationKeyType`+`identificationKey` at the API level; MCP wrapper handles this from the GTIN input). `gs1_digital_link_get` schema flagged TBD pending portal capture.
  - §13.2 curl commands rewritten for v2: new endpoint URLs (`/digitallinkv2/v2/digitallink[s]`), new body shape, new auth header. Added bulk-endpoint test. GET commands commented out until endpoint schema captured.
  - §4.3 `get()` filled in with real endpoint (`/digitallinkv2/v2/digitalLink/Gtin/{gtin14}`, capital L), response shape mapped to `DigitalLinkRecord`, not-found handling flagged as empirical.
  - §9.1 `gs1_digital_link_get` schema updated: `gtin` gets pattern constraint, response is `AdvancedDigitalLinkResponse`.
  - §13.2 GET curl commands activated for `get_existing` and `get_missing` fixtures; fixture count back to six.
  - LinkResponse has two more fields than LinkRequest (`linkTypeTitle` required, `defaultLinkType`/`public` optional on response). ApplicationIdentifierResponse adds `name`. Response types account for this.
  - §4.1 `GS1APIError` gains `error_results: list[dict] | None` field to hold the parsed 400 body when it follows the standard v2 `ErrorResult` shape.
  - §4.3 gains two new methods:
    - `set_enabled(gtin, is_enabled)` — PATCH `/digitallinkv2/v2/digitalLink/Gtin/{gtin14}/activationStatus`, 204 on success. For lifecycle actions (temporarily disable during recall). Client-only; no MCP tool in v0.1.0.
    - `validate_draft(gtin, application_identifiers)` — POST `/digitallinkv2/digitalLink/validateDraft` (note: no `/v2/` segment in this path). Dry-run validation returning `isValid`, error message, available AIs, `currentAnchorRelative`. Client-only; use in `run_plan.py` deferred to v0.2.
  - §5.1 addendum documents GS1 400 body parsing into `ErrorResult[]` structure.
- **0.2 (2026-05-27):** §5.4 added — Rollback and recovery (Level A + B implemented; Level C design). §10.6 added — Chat interaction patterns for flow-orchestrator with concrete example dialogs. §13.3 updated with cross-reference to PROJECT_HANDOVER §5.4.
- **0.1 (2026-05-27):** Initial.

---

**End of document.**

## Cross-references

- [`PROJECT_HANDOVER.md`](PROJECT_HANDOVER.md) — the "why" companion
