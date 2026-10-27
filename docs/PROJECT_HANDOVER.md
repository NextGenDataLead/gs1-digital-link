# Project Handover — GS1 Digital Link Orchestrator

**Status:** Ready to build (all blocking questions resolved; Digital Link API access confirmed for test and production)
**Audience:** Project manager, developer AI agents, contributing developers
**Purpose:** Single source of truth covering scope, architecture, decisions, plan, risks, and reference artifacts. Read this first before any other repo document.
**Version:** 0.8 (supersedes 0.7)
**Last updated:** 2026-07-04

---

## 1. Executive Summary

We are building an open-source orchestration tool that helps Dutch suppliers go from compliant product data in **GS1 Data Source** to a printable, GS1-compliant QR code (a *QR code powered by GS1*, encoding a GS1 Digital Link URI) with the resolver target pointing at the supplier's own website. The tool also provisions the destination web pages on the supplier's WordPress site.

The system runs inside **Claude Code**, with deterministic Python scripts doing the heavy lifting and Claude handling planning, user interaction, and exception cases. The deployment model is **one client per repository**: you clone it, point it at your own export and site, and run it locally. Credentials live in a gitignored `.env`; `clients.yml` — also gitignored — holds the configuration and the **names** of those environment variables, never their values. There are **no central services**.

> `clients.yml` is still keyed by client id, and every script still accepts one, so a single checkout *can* carry several clients. That capability is retained rather than advertised: the id is optional when exactly one client is defined, and becomes mandatory again the moment a second appears — silently choosing between them would be a way to publish the wrong catalogue to the wrong site.

**Data path:** Product data enters the tool as an **Excel/CSV export from MyGS1** (the free, standard route). Programmatic reads via GS1 Data Link are explicitly out of scope for v0.1.0.

**Resolver control:** The tool calls the **GS1 NL Digital Link API** to set the resolver's redirect target per GTIN. This API is **free of charge**; API keys are requested through **MyGS1**.

**Success criteria for v0.1.0**
- One pilot client end-to-end against the Digital Link API test environment, using an Excel export as the data source
- Re-runnable: a second run with no data change is a no-op; a second run with data changes prompts the user in chat
- Open-source release on GitHub with documented onboarding that lets a second client onboard themselves without paying for anything beyond their existing GS1 Data Source contract

---

## 2. Context and Background

### 2.1 The GS1 ecosystem in the Netherlands

GS1 Nederland issues GTINs (article codes) and operates **GS1 Data Source**, the Dutch product data pool. Suppliers enter product data once and it is shared with retailers, governments, and apps. Data enrichment in Data Source is a **human task** — out of scope for this project.

GS1 NL also operates a **GS1-conformant resolver**: a service that, when someone scans a *QR code powered by GS1* on a product, takes the GTIN-bearing URL and redirects the scanner to the right web destination. Brand owners configure that destination per-GTIN through the **Digital Link API**. Configuring that destination is the central automation problem this tool solves.

### 2.2 The GS1 NL product landscape

Three GS1 NL products are relevant to this project. The name-collision between the standard "GS1 Digital Link" and the paid product "GS1 Data Link" has caused confusion in earlier project docs and must be kept straight.

| Name | What it is | Cost | In scope for v0.1.0? |
|---|---|---|---|
| **GS1 Digital Link** | A *technical standard* for encoding GS1 identifiers as web URIs. Free specification. | Free (standard) | Yes — this is what the QR encodes |
| **GS1 Digital Link API** | GS1 NL's *API endpoint* for setting resolver targets per GTIN. **Free of charge; API key via MyGS1.** | **Free** | **Yes** — the write path this tool automates |
| **GS1 Data Source** | The data pool. MyGS1 UI, Excel export, data publishing. Tiered by turnover. | €291 setup + €291/yr (cat. A–C) up to €8.068/yr (cat. J) | Yes — every client already has this |
| **GS1 Data Link** | A *paid product* providing programmatic API read access to data in the pool. | €1.208 setup + €2.469/yr (cat. A–C) up to €18.502/yr (cat. J) | **No** — Excel export used instead |

**Sources:**
- Pricing: `https://www.gs1.nl/producten-services/data-exchange/tarieven/`
- Digital Link API cost + procurement route: confirmed by GS1 NL
- Digital Link API OpenAPI spec: `https://stgs1corpwebapist.blob.core.windows.net/yaml/digitallinkapi.yml`

**Consequence for the project:** Every client incurs zero incremental spend to use this tool. They already have GS1 Data Source (which is how they got GTINs). The Digital Link API is free. They export from MyGS1 for free. The tool itself is open-source and free. The only paid dependency is what they already had.

### 2.3 The QR code powered by GS1

A *QR code powered by GS1* is a standard QR symbol whose payload is a **GS1 Digital Link URI** of the form:

```
https://id.gs1.org/01/{GTIN14}
```

Optionally with batch (`/10/{batch}`), serial (`/21/{serial}`), and expiry (`?17=YYMMDD`). For consumer-facing marketing/info use cases (this project's focus), the GTIN-only form is what we encode.

The QR works at point-of-sale (from 2027 under GS1 Sunrise) and with any smartphone camera.

### 2.4 The resolver decision

The project uses the **GS1 NL resolver** (not self-hosted) and always redirects to the client's own website. Lowest operational cost, no infrastructure to run. Each client's QRs all carry the GS1 NL resolver domain; the redirect target on each GTIN points at the client's pages.

### 2.5 Sunrise 2027

By the end of 2027, GS1 Sunrise expects retailers worldwide to accept 2D barcodes at point-of-sale. This drives broad supplier adoption between now and then. EU Digital Product Passport regulation is converging on GS1 Digital Link as the data carrier.

---

## 3. Core Architecture Decisions

Each decision is recorded as **Context → Decision → Implication**.

### 3.1 Use the GS1 NL resolver, not our own
**Context:** Three resolver options were available: GS1 NL's, a self-hosted resolver on our own domain (e.g. `id.example.com` running on Vercel/Cloud Run/Container Apps), or a per-client resolver on each client's own domain.
**Decision:** GS1 NL resolver, always.
**Implication:** Lower operational cost; no infrastructure to run.
**Trade-offs considered:**
- **Analytics:** own resolver would give per-scan geo/UA/referrer visibility. Rejected for v0.1.0; can be reconsidered if a client asks specifically.
- **Branding:** own resolver puts the operator's or client's domain in the QR URL. Not important for consumer scans (they see the redirect target, not the resolver URL) but sometimes requested by marketing teams. Not sufficient to justify running infrastructure.
- **Multi-link-type flexibility:** own resolver would give full control over language routing, link types, and A/B testing. GS1 NL's resolver supports the same via the Digital Link API. No functional gap.
- **Future DPP relevance:** EU Digital Product Passport regulation is converging on GS1 Digital Link as the data carrier. Own resolver would give more control over how DPP data is served, signed, and versioned. Revisit when DPP requirements firm up (~2027).
- **Vercel specifically** was evaluated as a serverless target (Edge Functions + Neon Postgres / Vercel KV). Fits the pattern well at low-to-medium scan volumes; would migrate to Cloud Run / Container Apps if the tool grew to very high volume or strict EU data-residency became a hard requirement.
Reconsider if: (a) a client explicitly requests their own resolver domain, (b) DPP compliance requires local resolver control, (c) analytics from own-resolver becomes a monetisable feature.

### 3.2 Pattern A: WordPress page first, then GS1 redirect
**Context:** Either create the page then set the redirect, or set the redirect first and create the page later relying on deterministic URLs.
**Decision:** Pattern A.
**Implication:** We verify the destination page returns HTTP 200 before pointing the resolver at it.

### 3.3 Multi-client, open-source
**Context:** Project will be open-sourced; users bring their own credentials.
**Decision:** No hardcoded keys, site URLs, or tenant identifiers. All per-client config in a gitignored YAML file with env-var references for secrets.
**Implication:** `clients.yml` schema is part of the public contract.

### 3.4 Hybrid orchestration: deterministic Python + Claude/MCPs
**Context:** Looping in chat is slow and lossy; pure Python loses Claude's planning strengths.
**Decision:** Python scripts run the deterministic per-row loop; Claude does planning, prompting on changes, and exception interpretation.
**Implication:** Shared `lib/` layer used by both MCPs (single-call) and scripts (bulk).

### 3.5 Per-client page templates
**Context:** Each client's WordPress theme and content needs differ.
**Decision:** Default templates in repo; per-client overrides in `templates/{client_id}/`.
**Implication:** Template engine: Mustache.

### 3.6 One WordPress page per language
**Context:** Some clients are multilingual.
**Decision:** One page per language. Multilingual plugin links translations when present; otherwise siblings live independently.
**Implication:** On the GS1 side, each language gets its own `LinkInputModel` entry.

### 3.7 Re-run change detection with in-chat prompting
**Context:** Re-runs should not silently overwrite changes, but also shouldn't require a separate review tool.
**Decision:** State file per client; `run_plan.py` computes a diff; Claude shows it in chat; `run_execute.py` runs only what's confirmed.
**Implication:** Two-phase execution (plan → confirm → execute).

### 3.8 Excel export is the data path
**Context:** Options were Excel export (free, standard MyGS1 feature) or GS1 Data Link API (€2.469+/yr per client per the tarieven page).
**Decision:** Excel export, exclusively. GS1 Data Link is out of scope.
**Implication:**
- One data path in the code — no `mode` toggle, no dual maintenance
- Onboarding docs describe a single flow
- The tool itself costs the user nothing; their existing GS1 Data Source contract is all that's needed
- Manual export step is accepted as the human/machine boundary
**Rationale (historical):**
- v0.2 and v0.3 of this document originally had API mode as primary and Excel as fallback
- Investigation of the GS1 NL tarievenpagina (`https://www.gs1.nl/producten-services/data-exchange/tarieven/`) established that GS1 Data Link (the read API) costs €2.469/yr at the smallest category, scaling to €18.502/yr for the largest — per client
- For a typical client (SME product supplier, tens to hundreds of SKUs), that spend is too steep to require of the client just to save the manual export step
- Even at higher SKU counts the API mode would need to justify itself against "5 minutes per export cycle"
- Excel path stayed as the honest default; API mode can be added in v0.2 as opt-in for clients who happen to have GS1 Data Link for other reasons
Reconsider if: (a) GS1 NL introduces a cheaper own-data-only read tier, (b) a paying client explicitly requests API mode, (c) an existing client's export cycle grows unmanageable manually.

### 3.9 No central infrastructure
**Context:** Could be hosted SaaS or self-hosted.
**Decision:** Self-hosted only.
**Implication:** Each user runs the tool in their own Claude Code session or local environment. All state and credentials stay with the user.

### 3.10 Cost transparency for end users
**Context:** Open-source users will reasonably want to know what they'll have to pay GS1 NL.
**Decision:** Onboarding docs state upfront that beyond the client's existing GS1 Data Source contract, nothing costs anything. A `docs/costs.md` page links to the GS1 NL tarieven page and confirms the tool itself is free.
**Implication:** No pricing duplication; single source of truth.

---

## 4. Technical Design

### 4.1 GS1 NL API landscape

Only one GS1 NL API is used: the **Digital Link API v2**.

| Environment | Hostname | Purpose |
|---|---|---|
| Test (a.k.a. acceptance, sandbox) | `gs1nl-api-acc.gs1.nl` | Integration testing |
| Production | `gs1nl-api.gs1.nl` | Live use |

Developer portal (browses schemas, changelog, "Try it" console): `https://gs1nl-api-acc-developer.gs1.nl/`.

**API version:** v2. All endpoints live under the path prefix `/digitallinkv2/v2/`. The schemas below are captured from the portal's Open API 3 definition.

**Authentication (confirmed in Phase 2 — OAuth2 client-credentials):** Access is **not** a static token. GS1 NL issues a **client id + client secret** per environment (via MyGS1 / the developer portal). The client mints a short-lived JWT and then calls the Digital Link API with it:

1. `POST https://{host}/authorization/token` with **headers** `client_id: <id>` and `client_secret: <secret>` (lowercase header names). Returns `{"access_token": "<JWT>", "token_type": "Bearer", "expires_in": 3600}`.
2. Call the Digital Link API with `Authorization: Bearer <JWT>`. The JWT lives **1 hour**, so the client mints, caches, and refreshes it (and re-mints on a `401`).

The JWT carries an `accountNumber` claim (the entitled account) and `apiScopes: ["DigitalLinkApi"]`. `clients.yml` names the credential env vars per environment (`client_id_env_test`/`client_secret_env_test`, `…_production`); the earlier `auth_scheme` / single-token model is retired.

> **Historical note:** the spec originally modeled a single static "API access token" in the `Authorization` header with a Bearer-vs-raw switch. Phase 2 empirically found the real mechanism is the OAuth2 flow above; this section is the corrected, observed behaviour.

**Historical note:** an earlier version of the API (v1) used `Ocp-Apim-Subscription-Key` as the header — the Azure API Management default. v2 changed this to `Authorization`. Risk R10 in §9.1 anticipated this migration; v2 is the observed post-migration state.

### 4.2 GS1 NL Digital Link API v2

Portal reference: `https://gs1nl-api-acc-developer.gs1.nl/api-details#api=digitallink-api-v2` (schemas as Open API 3 YAML/JSON downloadable from the portal).

**Endpoints** (test hostname shown; production replaces `-acc` in hostname):

| Method | URL | Operation |
|---|---|---|
| `POST` | `https://gs1nl-api-acc.gs1.nl/digitallinkv2/v2/digitallink` | Create or update one Digital Link |
| `POST` | `https://gs1nl-api-acc.gs1.nl/digitallinkv2/v2/digitallinks` | Create or update many (body is a JSON array of the single-request shape) |
| `GET` | `https://gs1nl-api-acc.gs1.nl/digitallinkv2/v2/digitalLink/01/{identificationKey}` | Read one (path segment is the GTIN AI `01`, not `Gtin`) |
| `PATCH` | `https://gs1nl-api-acc.gs1.nl/digitallinkv2/v2/digitalLink/01/{identificationKey}/activationStatus` | Toggle `isEnabled` without touching links / resolverSettings. 204 on success |
| `POST` | `https://gs1nl-api-acc.gs1.nl/digitallinkv2/digitalLink/validateDraft` | Dry-run validation of a draft record. Returns `isValid` + available AIs + anchor info |

**Path anomalies (confirmed in Phase 2, preserve exactly):**
- **GET/PATCH key on the GTIN application identifier `01`, not the string `Gtin`.** The v2 OpenAPI shows `{identificationKeyType}` in the path, but the deployed API expects the AI code (`01` for GTIN) there — `/digitalLink/Gtin/{gtin}` returns `404` for every GTIN.
- POST create/update endpoints use lowercase `digitallink`; GET, PATCH, and ValidateDraft use capital-L `digitalLink`.
- ValidateDraft is the only endpoint without a `/v2/` segment in the path (`/digitallinkv2/digitalLink/validateDraft`, not `/digitallinkv2/v2/digitalLink/validateDraft`).
- Both may be portal-doc quirks or genuine API behaviour. The client preserves exact case and path structure; if the API turns out to be case-insensitive or the anomalies are corrected, the client still works.

**`CreateOrUpdateRequest` body:**

```json
{
  "accountNumber": "<client account, likely GLN — verify in MyGS1>",
  "identificationKeyType": "Gtin",
  "identificationKey": "<GTIN as digits, e.g. 08712345678905>",
  "isEnabled": true,
  "itemDescription": "<human-readable item description>",
  "resolverSettings": {
    "useGS1Resolver": true,
    "resolverDomainName": null
  },
  "links": [
    {
      "linkType": "pip",
      "language": "nl",
      "linkTitle": "Product page",
      "targetUrl": "https://www.democlient.nl/democlient/{slug}/",
      "defaultLinkType": true,
      "public": true,
      "mediaType": "text/html"
    }
  ],
  "applicationIdentifiers": []
}
```

**Field notes:**

- `accountNumber` — the account under which the Digital Link is created. **Environment-specific** and taken from the minted token's `accountNumber` claim (Phase 2 found the sandbox account is `8712345000010`, not Democlient's production GLN). `clients.yml` holds `account_number_test` / `account_number_production` separately.
- `identificationKeyType` — enumerated string. `"Gtin"` for product Digital Links; other values exist for `Sscc`, `Gln`, etc., but aren't used in v0.1.0 scope.
- `identificationKey` — the identifier value. For GTIN: the digit string, padded to 14 characters (`gtin14`).
- `resolverSettings.useGS1Resolver` — always `true` for our scope (matches decision §3.1). `resolverDomainName` is only meaningful when `useGS1Resolver` is `false`; can be `null` otherwise.
- `links[].mediaType` — required in v2 (was absent in v1). For our WordPress-page targets: `"text/html"`. If a link points to a JSON/API endpoint instead, use `"application/json"`.
- `applicationIdentifiers[]` — optional; used for batch/serial/expiry qualifiers on the identifier via GS1 Application Identifier codes. Not used in v0.1.0 scope; send as empty array.
- **Bulk endpoint** takes a JSON array `[CreateOrUpdateRequest, ...]` — same shape wrapped in `[]`. Client batch size configurable via `clients.yml`.

**Cost:** Free.
**Access:** OAuth2 client id + secret via MyGS1 / developer portal (separate pair per environment); the client mints a 1h Bearer JWT from them (§4.1).
**Environments:** Both test and production available and confirmed accessible.

**`AdvancedDigitalLinkResponse` (GET response body):**

```json
{
  "accountNumber": "string",
  "identificationKeyType": "Gtin",
  "identificationKey": "string",
  "isEnabled": true,
  "itemDescription": "string",
  "useGs1Elabel": false,
  "isElabelSupported": false,
  "digitalLinkUrl": "https://id.gs1.org/01/{gtin14}",
  "resolverSettings": {
    "useGS1Resolver": true,
    "resolverDomainName": "https://id.gs1.org"
  },
  "links": [{
    "linkType": "string",
    "linkTypeTitle": "string",
    "language": "string",
    "linkTitle": "string",
    "targetUrl": "string",
    "defaultLinkType": true,
    "public": true,
    "mediaType": "string",
    "isElabelLink": false
  }],
  "applicationIdentifiers": [{
    "identifier": "string",
    "name": "string",
    "templateVariable": "string"
  }]
}
```

**Response notes:**

- All top-level fields are documented as **optional** in the schema — the API is permissive on what it returns.
- `digitalLinkUrl` — the computed Digital Link URI (e.g. `https://id.gs1.org/01/08712345678905`). Returned by the API; the client can use it directly for QR encoding rather than constructing locally, though the local construction is deterministic and gives the same result.
- `linkTypeTitle` — human-readable name for `linkType` (e.g. `"pip"` → `"Product Information Page"`). Not present on request bodies; present on responses only.
- `applicationIdentifiers[].name` — human-readable name for the AI code (e.g. `"10"` → `"Batch/Lot Number"`).
- **Not-found (confirmed in Phase 2):** a GET for a non-existent GTIN returns **`400`** with the plain-string body `"No valid contract found for Gtin with id: {gtin}"` (not 404), which the client maps to `None`. GET returns the full record even when `isEnabled` is false. Business errors on writes return `400` with the standard `ErrorResult[]` array body (e.g. `21011 "No valid contract found."` when the GTIN has no valid contract/product under the account; `21001` for missing required fields). The response also carries `useGs1Elabel`, `isElabelSupported`, a populated `resolverDomainName` (e.g. `https://id.gs1.org`), and `links[].isElabelLink` — beyond the fields listed above.

**`UpdateDigitalLinkIsEnabledStatusInputModel` (PATCH activationStatus body):**

```json
{ "isEnabled": true }
```

204 No Content on success. Useful for lifecycle actions (temporarily disable a QR during a recall investigation) without rewriting the full record. In v0.1.0 exposed as `set_enabled()` on the client; no MCP tool wrapper for it yet (can be added in v0.2 if a workflow needs it).

**`ValidateDigitalLinkDraftModel` (ValidateDraft request body):**

```json
{
  "identificationKey": "08712345678905",
  "identificationKeyType": "Gtin",
  "applicationIdentifiers": [
    { "identifier": "10", "templateVariable": "batch" }
  ]
}
```

**`ValidateDigitalLinkDraftResponse`:**

```json
{
  "availableApplicationIdentifiers": [
    { "identifier": "string", "name": "string", "templateVariable": "string" }
  ],
  "validationResult": {
    "isValid": true,
    "validationErrorMessage": "string",
    "currentAnchorRelative": "string"
  }
}
```

Semantics per field:
- `availableApplicationIdentifiers` — the AIs the API knows about for this identifier type. Discovery helper for callers building `applicationIdentifiers[]` payloads.
- `validationResult.isValid` — go/no-go signal for the caller.
- `validationResult.validationErrorMessage` — human-readable reason when `isValid` is false.
- `validationResult.currentAnchorRelative` — semantic unclear from docs alone; probably the resolver's canonical URL structure for this identifier. Empirical during Phase 2.

**Use cases:** Pre-flight validation of a batch before bulk upsert (catches errors earlier, better chat UX). Not integrated into `run_plan.py` for v0.1.0 (deferred to v0.2 to avoid scope creep), but the client exposes `validate_draft()` so a future skill can call it.

**Standardized error response for 400:**

Several POST/PATCH endpoints return a structured error body on 400:

```json
[
  {
    "identifier": "08712345678905",
    "errors": [
      { "code": "GS1_INVALID_GTIN", "message": "GTIN checksum invalid" }
    ]
  }
]
```

The client parses this into `ErrorResult[]` and exposes it on `GS1APIError.error_results` when present — much better than treating the body as an opaque string.

### 4.3 Excel export from MyGS1 (the data path)

**Source:** MyGS1 (`https://mijn-v2.gs1.nl`) → article list → export.

> **Phase 3 update — the pilot uses the richer GDSN export.** Democlient's real export is
> the full **GS1 Data Source / GDSN datapool** export (24 module worksheets, 7 header rows,
> `Gtin` + `TargetMarketCountryCode` composite key, `LanguageCode`/`Value` pairs), not the
> flat article-list export the table below assumes. The parser handles both via
> `export.format` (`gdsn` | `flat`); the GDSN mapping model is documented in
> `IMPLEMENTATION_SPEC.md` §3.6. The generic column names below describe the flat format.

**Format:** Excel (.xlsx) preferred; CSV supported as fallback.

**Column schema** (documented in `docs/data-source-export-schema.md`):

| Column | Required | Notes |
|---|---|---|
| `gtin` | Yes | 8–14 digit GTIN |
| `brand` | Yes | Brand name |
| `product_name_{lang}` | Yes (per language) | e.g. `product_name_nl`, `product_name_fr` |
| `description_short_{lang}` | Recommended | Short description per language |
| `description_long_{lang}` | Optional | Long description per language |
| `gpc_brick_code` | Yes | GPC classification |
| `net_content` | Optional | e.g. `250 g` |
| `image_url` | Optional | Direct URL to product image (downloaded and uploaded to WP) |
| `category` | Optional | Maps to taxonomy term |

**Image handling** (configurable per client):

1. **URL in export:** Client's Excel has an `image_url` column. Tool downloads → uploads to WP → sets as `featured_media`.
2. **Local folder:** Client puts images in `input/{client_id}/images/{gtin}.jpg`. Tool uploads from there.
3. **Manual after creation:** Client adds featured images via WP admin after pages are created. Tool skips media handling.

**Parser:** `scripts/parse_export.py` reads the file, validates columns per client schema, normalises into the internal `ProductRecord` shape.

**Schema-finalisation gate:** Before Phase 3 starts, obtain a real (anonymised if needed) MyGS1 export from the pilot client so the parser is built against the actual columns Data Source produces, not against assumptions.

### 4.4 WordPress integration

**API:** WordPress REST API at `/wp-json/wp/v2/`.

**Authentication:** WordPress Application Passwords. Avoid JWT plugins.

**Post creation:** `POST /wp-json/wp/v2/{post_type}`. Note `post_type` is **not always `page`** (Democlient uses `democlient`). Schema captures this per client. Post type must have `show_in_rest: true` — verify during onboarding.

**Per-product payload:**
```json
{
  "title": "Product Name",
  "slug": "p-{gtin}",
  "status": "publish",
  "content": "<rendered HTML from template>",
  "featured_media": <optional media_id>,
  "meta": { "gtin": "...", "brand": "..." }
}
```

**Idempotency:** Look up by `meta.gtin` (preferred) or slug before creating.

**Multilingual:**
- **Polylang detected when:** `GET /wp-json/pll/v1/languages` returns 200
- **WPML detected when:** `/wp-json/sitepress-multilingual-cms/...` routes exist
- **None:** Independent sibling pages with language-prefixed slugs

**Democlient specifics (worked example):**
- Custom post type `democlient`
- Custom taxonomy `democlient-categories` with `-fr` suffix (Polylang convention)
- Languages NL + FR; NL at site root, FR at `/fr/`

### 4.5 Credentials strategy

One key per client, one env var per key:

```yaml
gs1:
  admin_gln: "8712345000003"
  subscription_key_env: DEMOCLIENT_GS1_KEY
```

`lib/config.py` reads the named env var at call time. No secrets are ever committed or logged.

### 4.6 QR rendering

Library: **`qrcode`** (Python, with `pillow`).

**Parameters:**
- Error correction: `M` default, `H` for harsh print
- Minimum print size: 15×15 mm; recommended 20×20 mm
- Quiet zone: 4 modules (standard)
- Formats: SVG, PNG, EPS

**URI uppercase trick:** `HTTPS://ID.GS1.ORG/01/{GTIN}` reduces symbol size. Path stays case-sensitive.

**Output:** `output/{client_id}/qr/{gtin}.{ext}`.

### 4.7 Configuration and secrets

**Config file:** `clients.yml` (gitignored). Schema in §10.1.

**Secrets:** Never in YAML. Each secret field is a `*_env` key naming an environment variable, loaded from `.env` (gitignored).

### 4.8 State and idempotency

**State file:** `output/{client_id}/state.json`. Per GTIN per language: WP page id, URL, featured media id, content hash, GS1 link set hash, last run timestamp.

**Content hash** covers: title, content body, featured-media reference, GS1 `targetUrl`, GS1 `linkTitle`. Changes trigger a "changed" diff in `run_plan.py`.

**State persistence:** State written to local files under `output/{client_id}/state.json` in the repo working tree.

---

## 5. Onboarding Process (per client)


Free end-to-end; no extra GS1 NL contract required beyond what the client already has.

### 5.1 Prerequisites

- Active **GS1 NL contract** with Company Prefix issued
- **MyGS1** access for at least one user at the client (`https://mijn-v2.gs1.nl`)
- A **GLN** representing the company (the `adminGln`)
- A WordPress site they own, with admin access

### 5.2 Steps

**1. Request Digital Link API credentials via MyGS1 / developer portal.** Auth is OAuth2 client-credentials (§4.1): obtain a **Client ID + Client Secret** per environment and subscribe to the Digital Link API v2 product. Copy them into `.env` — test/sandbox under `{CLIENT}_GS1_CLIENT_SANDBOX_ID`/`_SECRET`, production under `{CLIENT}_GS1_CLIENT_ID`/`_SECRET`. Confirm the account has a **Digital Link contract** (without it, creates fail `21011 "No valid contract found."`).

**2. Activate Digital Link on each GTIN.** In MyGS1, per article: set a default `pip` link, then Edit → Web page → check "Activeer GS1 Digital Link" → save. This can be done in bulk via the Digital Link API after the first GTIN by setting `isEnabled: true` on the upsert.

**3. Export from MyGS1 to Excel.** Save the file at `input/{client_id}/products.xlsx`.

**4. WordPress onboarding.**
- WP admin → `Users → Profile` → **Application Passwords** → name `gs1-orchestrator` → generate → copy into `.env`.
- Confirm post type at `/wp-json/wp/v2/types`; each one needed must show `show_in_rest: true`.
- Confirm multilingual plugin at `/wp-json/pll/v1/languages` (Polylang) or `/wp-json/sitepress-multilingual-cms/` (WPML).
- Populate the `wordpress` block in `clients.yml`.

**5. Smoke test.** Mint a token, then do a **read-only** GET (harmless — no writes):
```bash
H=gs1nl-api-acc.gs1.nl
TOKEN=$(curl -s -X POST \
  -H "client_id: $DEMOCLIENT_GS1_CLIENT_SANDBOX_ID" \
  -H "client_secret: $DEMOCLIENT_GS1_CLIENT_SANDBOX_SECRET" \
  "https://$H/authorization/token" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
curl -i -H "Authorization: Bearer $TOKEN" \
  "https://$H/digitallinkv2/v2/digitalLink/01/00000000000000"
```
Mint `200` + `{"access_token": ...}` = credentials live; GET `400 "No valid contract found for Gtin with id: …"` = auth works and not-found confirmed (path uses the GTIN AI `01`). Mint `400 "Your ClientId or ClientSecret might be incorrect."` = wrong/inactive credentials. The account you may write to is the `accountNumber` claim inside the JWT.

**6. Run the tool.**

### 5.3 Onboarding checklist

```
[ ] Client has GS1 NL contract + GLN + MyGS1 access
[ ] Digital Link API key requested via MyGS1 (test + production)
[ ] Digital Link activated on at least one GTIN in MyGS1
[ ] Excel export downloaded to input/{client_id}/products.xlsx

WordPress (see §5.4 for details on each):
[ ] WordPress core version 5.6+
[ ] REST API reachable (/wp-json/ returns JSON)
[ ] HTTPS enforced
[ ] Security-plugin conflicts identified and resolved
[ ] Automation user created with Editor role
[ ] Application password generated and copied to .env
[ ] Custom post type registered with show_in_rest: true
[ ] Custom taxonomies (if any) registered with REST
[ ] Required taxonomy terms exist in WP
[ ] Multilingual plugin configured (Polylang for multilingual clients)
[ ] Media library upload limits sufficient for product photos
[ ] Theme's single-{post_type} template inspected
[ ] Permalinks set to Post name or custom
[ ] Slug strategy confirmed with client

[ ] clients.yml and .env populated
[ ] Smoke test against test environment: token mints (200) and a GET returns 200 or 400-not-found (not 401)
[ ] Dry-run on 1 GTIN in test succeeds
[ ] Production key configured
[ ] Production dry-run succeeds
[ ] Pilot run on first 10 real products
[ ] Go-live with full catalogue
```

### 5.4 WordPress site setup — detailed reference

Before Phase 4 (or before onboarding any new client to an already-built tool), work through this checklist against the target WordPress site. Most items are not the tool's requirements — they are WordPress's requirements for REST-based automation with Application Passwords. Common failure modes and their causes are documented per item.

**5.4.1 WordPress core version 5.6+** — Reason: Application Passwords ship in WP core from 5.6. Older versions need a plugin, which we do not support. Check via WP admin Dashboard → Updates, or `curl https://{site}/wp-json/ | jq .description`.

**5.4.2 REST API is publicly reachable** — `curl https://{site}/wp-json/` must return JSON. Some hosts (SiteGround, legacy WP Engine) block `/wp-json/` by default; re-enable at host/plugin level.

**5.4.3 HTTPS enforced** — Application Passwords work only over TLS. No workaround for staging on plain HTTP — provision a certificate (Let's Encrypt is fine for staging).

**5.4.4 Security-plugin conflicts** — Wordfence, iThemes/Solid Security, All In One WP Security can block Application Passwords or REST calls. Check REST endpoints allowed for authenticated users, Application Passwords feature not disabled at plugin level, `Authorization` header not stripped, IP throttling won't rate-limit batch runs.

**5.4.5 Automation user created** — Dedicated WP user, not repurposed. Name `gs1-publisher` or `gs1-orchestrator`. Role **Editor** (Author too restrictive for custom post types with `map_meta_cap`; Admin gives too much access). Email a dedicated address or `+bot@` alias.

**5.4.6 Application Password generated** — WP admin → user's profile → Application Passwords → name it `gs1-orchestrator` → generate. Copy immediately (WordPress shows it exactly once). Store in `.env` under `{CLIENT}_WP_APP_PASS`. If missing, check `wp-config.php` for `define('WP_APPLICATION_PASSWORDS', false);` and remove.

**5.4.7 Custom post type registered with REST support** — Every post type the tool writes to must have `show_in_rest => true`. Test: `curl https://{site}/wp-json/wp/v2/types | jq 'keys'` must include target post type slug with valid `rest_base`. For Democlient: `democlient`.

**5.4.8 Custom taxonomies registered with REST support** — If tool sets taxonomy terms, taxonomy must be REST-enabled. Test: `curl https://{site}/wp-json/wp/v2/taxonomies | jq 'keys'`. For Democlient: `democlient-categories`.

**5.4.9 Required taxonomy terms exist** — Terms referenced from Excel `category` column must exist in WP before the tool runs. v0.1.0 strategy: unknown-term rows are flagged, user prompted in chat. Auto-create deferred to v0.2 to avoid silent duplicate proliferation from typos.

**5.4.10 Multilingual plugin configured** — v0.1.0 supports **Polylang** only (WPML deferred to v0.2). Configure languages matching `wordpress.languages`, default matching `wordpress.default_language`, URL strategy: subdirectory (`/fr/`). Test: `curl https://{site}/wp-json/pll/v1/languages` returns 200 with configured languages. Verify custom post type is translatable in Polylang settings.

**5.4.11 Media library configured for uploads** — `upload_max_filesize`, `post_max_size`, `memory_limit` must accommodate expected image sizes (5+ MB common for product photos). Default 2 MB will reject. JPEG/PNG work out of the box; WebP requires WP 5.8+.

**5.4.12 Theme's single-{post_type} template inspected** — The tool writes HTML into WP `content` field. Custom post types typically have `single-{post_type}.php`; inspect for surrounding layout. Our template output should be self-contained (no theme-specific CSS class assumptions). Create one test page manually to see how it renders visually before Phase 5's real print+scan test.

**5.4.13 Permalinks configured** — **Settings → Permalinks** must be **Post name** or custom structure including slug. Plain (`?p=123`) unsupported. Common CPT structures: `/{post_type}/{slug}/` or `/{lang}/{post_type}/{slug}/` (Polylang).

**5.4.14 SEO plugin scope (v0.1.0 limitation)** — Yoast/Rank Math have their own REST endpoints for meta title, description, Open Graph. v0.1.0 does **not** write to these. Meta title/description fall back to what the SEO plugin auto-generates. Manual editing required for bespoke meta. Yoast/Rank Math adapters planned for v0.2.

**5.4.15 Slug strategy confirmed with client** — **GTIN-based** (`p-{gtin}`): deterministic, no collisions, immune to product-name changes. **Default.** **Human-readable**: SEO-friendlier but risks `-2` suffixes on duplicates. Set `wordpress.slug_pattern: "p-{gtin}"` in `clients.yml`. Once printed on packaging, the slug lives forever in the QR-target URL. Confirm **before any production run**.

### 5.5 Discovering a client's site

Before onboarding (§5.4), inspect the client's public site: the assumptions that drive
`clients.yml` — post type, category taxonomy, multilingual plugin, URL structure — come from
here. Public inspection is a starting point; verify each finding in WP admin during onboarding.
The playbook:

1. Inspect public URLs — identify custom post type from URL patterns
2. Check `/wp-json/wp/v2/types` and `/wp-json/wp/v2/taxonomies` for the REST-registered types
3. Check `/wp-json/pll/v1/languages` for Polylang; `/wp-json/sitepress-multilingual-cms/` for WPML; neither for single-language sites
4. View source of a category or archive page to identify slug patterns and multilingual conventions
5. Record findings in a new `docs/clients/{client_id}.md`

---

## 6. Component Catalog

### 6.1 MCPs

| Name | Build / Adopt | Purpose |
|---|---|---|
| `gs1-nl-mcp` | **Build** | Wraps the Digital Link API (three tools: single upsert, bulk upsert, get) |
| `wordpress-mcp` | **Adopt, likely fork** | WP REST API: per-call credentials, custom post types, Polylang/WPML aware |
| `qr-render-mcp` | **Build (small)** | Render QR symbols on demand |
| Filesystem MCP | Adopt reference | Read inputs, write outputs |

### 6.2 Skills

| Name | Purpose |
|---|---|
| `gs1-export-parser` | Parse the Excel export; produce `ProductRecord[]` |
| `wordpress-product-page` | Hold template logic; render and post pages; verify 200 |
| `gs1-digital-link` | Build the API payload, batch the bulk POST |
| `qr-render` | Apply sizing/format rules; output file |
| `flow-orchestrator` | Top-level: read export, plan, ask user, execute, summarise |

### 6.3 Python scripts

| Script | Purpose |
|---|---|
| `scripts/parse_export.py` | Excel/CSV → normalised JSON dataset |
| `scripts/run_plan.py` | Diff dataset against state file; emit `plan.json` |
| `scripts/run_execute.py` | Read confirmed plan; loop WP → GS1 → QR per row; write JSONL log |
| `scripts/verify_run.py` | After execute: HEAD each target URL, summarise failures |

### 6.4 Shared `lib/`

| Module | Purpose |
|---|---|
| `lib/gs1_dl_client.py` | GS1 NL Digital Link API client |
| `lib/wp_client.py` | WordPress REST client |
| `lib/multilingual.py` | Polylang/WPML adapters |
| `lib/qr.py` | QR rendering |
| `lib/templates.py` | Template loading and rendering (Mustache) |
| `lib/state.py` | State file load/save/diff |
| `lib/config.py` | Load and validate `clients.yml`; resolve subscription keys |
| `lib/records.py` | Internal normalised `ProductRecord` schema |

### 6.5 Templates

```
templates/
├── _default/
│   ├── product.nl.html
│   ├── product.en.html
│   └── product.fr.html
└── {client_id}/
    ├── product.nl.html
    └── product.fr.html
```

Variables documented in `docs/template-variables.md`.

---

## 7. Repository Structure

```
gs1-digital-link-orchestrator/
├── README.md
├── LICENSE                          # MIT
├── CHANGELOG.md
├── CONTRIBUTING.md
├── .gitignore
├── clients.example.yml
├── .env.example
├── pyproject.toml
├── package.json
├── schema/
│   └── clients.schema.json
├── docs/
│   ├── architecture.svg
│   ├── setup.md
│   ├── costs.md
│   ├── gs1-nl-onboarding.md
│   ├── wordpress-onboarding.md
│   ├── data-source-export-schema.md
│   ├── template-variables.md
│   └── troubleshooting.md
├── lib/
│   ├── gs1_dl_client.py
│   ├── wp_client.py
│   ├── multilingual.py
│   ├── qr.py
│   ├── templates.py
│   ├── state.py
│   ├── config.py
│   └── records.py
├── scripts/
│   ├── parse_export.py
│   ├── run_plan.py
│   ├── run_execute.py
│   └── verify_run.py
├── mcps/
│   ├── gs1-nl/
│   ├── wordpress/
│   └── qr-render/
├── .claude/skills/          # Claude Code discovers project skills HERE, not in ./skills
│   ├── gs1-export-parser/
│   ├── wordpress-product-page/
│   ├── gs1-digital-link/
│   ├── qr-render/
│   ├── content-generator/
│   └── flow-orchestrator/
├── templates/
│   ├── _default/
│   └── {client_id}/
├── input/                           # gitignored; uploaded exports + images
│   └── {client_id}/
├── output/                          # gitignored
│   └── {client_id}/
│       ├── state.json
│       ├── data/
│       ├── qr/
│       └── runs/{timestamp}.jsonl
└── tests/
    ├── fixtures/
    ├── lib/
    └── scripts/
```

---

## 8. Project Plan — Step by Step

### 8.1 Phase overview

| # | Phase | Effort (dev-days) | Calendar | Exit gate |
|---|---|---|---|---|
| 0 | Foundation | 0.5 | 1 d | API keys in .env; pilot client confirmed |
| 1 | Repo skeleton & config | 0.5 | 0.5 d | Repo published; schema committed |
| 2 | GS1 Digital Link client + MCP | 1–2 | 2–3 d | One real GTIN updated in test env |
| 3 | Excel parser + records schema | 1 | 2 d | Pilot client's export → `ProductRecord[]` |
| 4 | WordPress client + MCP | 1.5 | 3–4 d | Page created on staging WP |
| 5 | QR rendering + templates | 0.5 | 1 d | Printed QR scans and resolves |
| 6 | `lib/`, scripts, and state | 2 | 3–4 d | `run_execute.py` completes for 1 GTIN |
| 7 | Re-run & change detection | 1 | 2 d | Re-run with change triggers chat prompt |
| 7.5 | GPC brick → category mapping | 1 | 1–2 d | Every export brick maps to a category; overrides signed off |
| 8 | Skills & flow orchestrator | 1 | 2 d | End-to-end via Claude Code chat works |
| 9 | Pilot client end-to-end | 2 | 1 wk | 10+ real products, no manual fixes |
| 10 | Docs | 1.5 | 2 d | Fresh user can onboard from docs alone |
| 11 | Production cut + 0.1.0 release | 0.5 | 1 d | Tagged release; MCP registry entry |
| **Total** | | **~12–14 dev-days** | **~4–5 wk calendar** | |

### 8.2 Phase detail

**Phase 0 — Foundation:** Pick MIT license, register GitHub repo name, decide pilot client. Confirm pilot client has GS1 NL contract, GLN, MyGS1 access, WordPress site with admin access. Confirm Digital Link API test/production keys functional (curl smoke test from §5.2 step 5). Obtain a sample MyGS1 Excel export from the pilot client to lock the column schema. Exit gate: keys in `.env`, smoke tests pass, sample export in hand.

**Phase 1 — Repo skeleton & config:** Initialise repo with structure in §7. Commit MIT `LICENSE`, baseline `README.md`, `CHANGELOG.md`, `.gitignore`, `clients.example.yml`, `.env.example`, `schema/clients.schema.json`, `pyproject.toml`, `package.json`. Set up GitHub Actions: lint, tests. Exit gate: `git clone` + setup + lint + tests pass on a clean machine.

**Phase 2 — GS1 Digital Link client + MCP:** Build `lib/gs1_dl_client.py` (OAuth2 client-credentials token minting per §4.1, upsert/upsert_bulk/get, retries, JSONL logging). Build `mcps/gs1-nl/` TypeScript with MCP SDK — three tools. Integration test against test env with one real GTIN. Exit gate: a test GTIN's redirect can be set via both the Python lib and the MCP tool.

**Phase 3 — Excel parser + records schema:** Build `lib/records.py` internal `ProductRecord` schema. Build `scripts/parse_export.py`. Test with pilot client's real export. Exit gate: `output/{client_id}/data/products.json` contains full normalised catalogue.

**Phase 4 — WordPress client + MCP:** Survey existing WP MCPs (adopt vs. fork). Build `lib/wp_client.py` — idempotent upsert, media upload, plugin detection, translation linking. WP MCP tools. Integration test against staging WP. Exit gate: page lives at expected URL on staging WP, returns 200.

**Phase 5 — QR + templates:** `lib/qr.py` with size/format options. `lib/templates.py` with override resolution. Ship default templates. Build pilot client's first template. Build `qr-render-mcp`. **Manual test:** render QR for one pilot GTIN, print at 20mm, scan with iOS + Android. Exit gate: scan-to-page works on real hardware.

**Phase 6 — lib, scripts, state:** `lib/state.py`, `lib/config.py`. `scripts/run_execute.py` per §10.5 skeleton. Unit tests for `lib/` with mocked HTTP. Exit gate: `python scripts/run_execute.py {client} plan.json` completes for 1 GTIN.

**Phase 7 — Re-run & change detection:** `scripts/run_plan.py` — hash per (GTIN, language); classify new/unchanged/changed. `flow-orchestrator` skill — format diff for chat, collect decisions, emit `plan.confirmed.json`, invoke execute. Test: change one product name, re-run, confirm prompt appears. Exit gate: full re-run flow works in Claude Code chat.

**Phase 7.5 — GPC brick → category mapping:** Derive each product's site category from its GPC brick, using the **GS1 DIY sector datamodel** as the classification source — **the operator supplies the DIY datamodel at the start of the phase** (as with the export and control file; it is not sourced by the tool). A straight brick→category map is not sufficient: one brick can span several marketing categories, and a client's own categorisation is not purely semantic. So the phase produces a `brick_category_map` **plus a per-GTIN override list** in `clients.yml`, reviewed and signed off by the client. Exit gate: every brick in the export maps to a category, overrides cover the exceptions, and `run_plan` assigns the category for every planned product (unmapped bricks warn, never guess).

**Phase 8 — Skills & flow orchestrator polish:** Finalise SKILL.md files. Test in fresh Claude Code session: user uploads export, says "run for {client} in test". Exit gate: end-to-end flow works from a single chat instruction.

**Phase 9 — Pilot client end-to-end:** Run end-to-end against pilot's test environment. Iterate on edge cases. Run first 10 real products through to production. Capture client-specific quirks in `docs/clients/{client_id}.md`. Exit gate: live pages, live redirects, ready-to-print QRs for ≥10 products with no manual corrections.

**Phase 10 — Docs:** `docs/setup.md`, `docs/costs.md`, `docs/gs1-nl-onboarding.md` (§5 content), `docs/wordpress-onboarding.md` (app password, post type, plugin checks), `docs/data-source-export-schema.md`, `docs/template-variables.md`, `docs/troubleshooting.md`. Polish `README.md` with quickstart + architecture.svg. Exit gate: fresh user can clone, follow setup.md, and onboard a second client without asking questions.

**Phase 11 — Production cut + 0.1.0 release:** Pilot client moves to production. Tag 0.1.0. Publish MCP to registry. Announcement (LinkedIn / dev.to). Open GitHub Issues templates. Exit gate: 0.1.0 released; first external user successfully onboards.

### 8.3 Dependencies

```
Phase 0 ──► Phase 1 ──► Phase 2 ──┐
                  ├─► Phase 3 ─────┤
                  ├─► Phase 4 ─────┤
                  └─► Phase 5 ─────┼─► Phase 6 ──► Phase 7 ──► Phase 8 ──► Phase 9 ──► Phase 10 ──► Phase 11
```

Phases 2–5 can run in parallel with multiple developers; with one developer, sequence as shown.

---

## 9. Risks and Open Questions

### 9.1 Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | WordPress MCP ecosystem too immature; build from scratch | Medium | Medium | Time-boxed survey in Phase 4; budget contingency |
| R2 | Per-client template variation explodes | Low at 5 clients; high at 50 | Medium | Document patterns; consider `templates/_shared/` partials |
| R3 | Polylang/WPML differences balloon scope | Medium | Medium | v0.1.0 = Polylang only (Democlient uses it); WPML in 0.2 |
| R4 | State persistence across sessions | High | Medium | State to local files under `output/` in the repo working tree |
| R5 | Idempotency edge cases | High | Low–Medium | Each step does a lookup before action |
| R6 | Wrong `digitalLinkUrl` baked into printed QRs | Low (caught Phase 5) | High | Real print + scan test before any client uses production |
| R7 | Secret leakage via Claude chat history | Medium | High | All secrets in env vars; documented prominently in setup.md |
| R8 | Open-source maintenance burden | Medium | Medium | Issue templates and contribution guide upfront |
| R9 | Excel column schema varies between MyGS1 exports (over time, or between sectors) | Medium | Medium | Per-client column overrides in `clients.yml`; Phase 0 uses real pilot export to define baseline |
| R10 | GS1 NL migrates auth model (e.g. from subscription key to OAuth2 Client Credentials) | **Materialised — v2 is OAuth2** | Low | Phase 2 confirmed v2 uses **OAuth2 client-credentials**: `POST /authorization/token` (client_id/client_secret) → 1h Bearer JWT. Implemented in `lib.gs1_dl_client` (`_mint_token`/`_get_token`) and the TS MCP; token minting/refresh is isolated, so a future change is contained |

Notable removals since v0.3:
- R1 v0.3 ("Digital Link write API may be paid") — **resolved**: confirmed free by GS1 NL
- R2 v0.3 (subscription-key model ambiguity) — **resolved**: single-key model confirmed
- R11 v0.3 (rate limits) — **not applicable**: no read-API integration

### 9.2 Open questions

All previously open questions blocking development have been answered:
- Digital Link API cost — **free**
- API procurement route — **via MyGS1**
- Test and production availability — **both live and accessible**
- Data Link scope — **out of scope**

Minor items still worth confirming during Phase 2 integration work (not blocking):
- Exact allowed `linkType` values (empirically discoverable from the API; will document as we hit them)
- Exact rate limits (empirically discoverable; not a blocker at pilot volumes)
- `digitalLinkUrl` field: use canonical `https://id.gs1.org/01/{GTIN14}` unless the test API rejects it

### 9.3 Open questions per client during onboarding

- Custom post type name and `show_in_rest` status
- Multilingual plugin (Polylang / WPML / none)
- Default and additional languages
- Slug strategy preference (GTIN-based vs. human-readable)
- SEO plugin (Yoast / Rank Math / none) — for future meta-field support
- Image handling pattern (URL in export, local folder, manual after creation)

---

## 10. Reference Artifacts

### 10.1 `clients.example.yml`

```yaml
# clients.example.yml — copy to clients.yml and fill in.
# clients.yml is gitignored. Never commit real credentials.
# Secrets live in environment variables; this file points at them by name.

version: 1

defaults:
  gs1:
    environment: test               # test | production
    api_version: v2                 # locked; endpoint prefix is /digitallinkv2/v2/
    # Auth is OAuth2 client-credentials; the client mints a 1h JWT (Bearer) — §4.1.
    identification_key_type: "Gtin" # enum in v2; always Gtin for our scope
    digital_link_url_pattern: "https://id.gs1.org/01/{gtin14}"
    resolver_settings:
      use_gs1_resolver: true        # matches decision §3.1
      resolver_domain_name: null    # only used when use_gs1_resolver: false
    default_media_type: "text/html" # for HTML product-page targets
    batch_size: 50                  # for bulk endpoint
  wordpress:
    post_type: page
    post_status: publish
    multilingual_plugin: none       # none | polylang | wpml
    default_language: nl
    languages: [nl]
    image_handling: url_in_export   # url_in_export | local_folder | manual
  qr:
    formats: [svg, png]
    size_mm: 20
    error_correction: M
    dpi: 300
  flow:
    on_change: prompt
    on_missing_field: prompt
    batch_size: 50

clients:

  # Example — Democlient: custom post type, Polylang, NL + FR
  democlient:
    display_name: "Democlient B.V."
    enabled: true

    gs1:
      # accountNumber differs per environment — take each from the minted token's
      # accountNumber claim (sandbox and production are different accounts).
      account_number_test: "8712345000010"
      account_number_production: "8712345000003"
      # OAuth2 client credentials per environment (issued via MyGS1 / dev portal)
      client_id_env_test: DEMOCLIENT_GS1_CLIENT_SANDBOX_ID
      client_secret_env_test: DEMOCLIENT_GS1_CLIENT_SANDBOX_SECRET
      client_id_env_production: DEMOCLIENT_GS1_CLIENT_ID
      client_secret_env_production: DEMOCLIENT_GS1_CLIENT_SECRET

    export:
      path: "./input/democlient/products.xlsx"
      # Excel column name (as it appears in the header row, case-sensitive) →
      # canonical ProductRecord field path. See IMPLEMENTATION_SPEC.md §3.2 for
      # the full mapping semantics (per-language paths use dot notation).
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

    wordpress:
      site_url: "https://www.democlient.nl"
      username: "gs1-publisher"
      app_password_env: DEMOCLIENT_WP_APP_PASS
      post_type: democlient
      multilingual_plugin: polylang
      languages: [nl, fr]
      default_language: nl
      image_handling: url_in_export
      taxonomies:
        democlient-categories:
          map_from_column: "category"
      slug_pattern: "p-{gtin}"
      target_url_pattern: "{site_url}/{lang_segment}{post_type}/{slug}/"

    template:
      override_dir: templates/democlient
      files:
        nl: product.nl.html
        fr: product.fr.html

    gs1_links:
      - link_type: "gs1:pip"
        default: true
        public: true
        title_pattern: "{product_name}"
```

### 10.2 `.env.example`

```bash
# Copy to .env and fill in. .env is gitignored.

# Democlient — OAuth2 client credentials per environment (the client mints a
# short-lived token from these); plus the WordPress application password.
# Test / acceptance (sandbox):
DEMOCLIENT_GS1_CLIENT_SANDBOX_ID=
DEMOCLIENT_GS1_CLIENT_SANDBOX_SECRET=
# Production:
DEMOCLIENT_GS1_CLIENT_ID=
DEMOCLIENT_GS1_CLIENT_SECRET=
# WordPress
DEMOCLIENT_WP_APP_PASS=
```

### 10.3 System architecture diagram

See `docs/architecture.svg`. Top: GS1 Data Source (manual enrichment). Excel export flows into a Claude Code session. Orchestrator runs WordPress upsert → verify 200 → GS1 redirect set → QR render, per GTIN per language. Outputs: live WP page, configured resolver redirect, QR file.

### 10.4 What GS1 NL confirmed

GS1 NL confirmed that the Digital Link write API is free of charge, is procured through MyGS1,
and has separate test and production keys. The tarieven page at
`https://www.gs1.nl/producten-services/data-exchange/tarieven/` is the live source of truth for pricing.

### 10.5 `run_execute.py` skeleton

```python
"""Deterministic execution of a confirmed run plan.

Per row: WP upsert → verify 200 → GS1 set redirect → render QR.
Idempotent. Resumable. Each row's outcome appended to a JSONL log.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

from lib.config import load_clients
from lib.state import load_state, save_state
from lib.wp_client import WordPressClient
from lib.gs1_dl_client import GS1DigitalLinkClient
from lib.qr import render_qr
from lib.templates import render_template


def main(client_id: str, plan_path: Path) -> int:
    cfg = load_clients()[client_id]
    state = load_state(client_id)
    plan = json.loads(plan_path.read_text())

    wp = WordPressClient(cfg.wordpress)
    gs1 = GS1DigitalLinkClient(cfg.gs1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(f"output/{client_id}/runs/{ts}.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for row in plan["confirmed"]:
        gtin, lang = row["gtin"], row["language"]
        outcome = {"gtin": gtin, "language": lang, "ts": datetime.now(timezone.utc).isoformat()}
        try:
            html = render_template(cfg, row)
            page = wp.upsert_page(
                post_type=cfg.wordpress.post_type,
                slug=row["slug"],
                title=row["title"],
                content=html,
                featured_media=row.get("featured_media_id"),
                language=lang,
                existing_id=state.get(gtin, {}).get(lang, {}).get("wp_page_id"),
            )
            outcome["wp_page_id"] = page.id
            outcome["wp_url"] = page.url

            if not wp.verify_url(page.url):
                raise RuntimeError(f"WP URL {page.url} did not return 200")

            gs1.upsert_link(
                gtin=gtin,
                digital_link_url=cfg.gs1.digital_link_url_pattern.format(gtin14=gtin.zfill(14)),
                link_type="gs1:pip",  # a GS1 Web Vocabulary CURIE; bare "pip" is unrecognised
                language=lang,
                target_url=page.url,
                link_title=row["title"],
            )
            outcome["gs1_set"] = True

            qr_paths = render_qr(
                uri=cfg.gs1.digital_link_url_pattern.format(gtin14=gtin.zfill(14)),
                output_dir=Path(f"output/{client_id}/qr"),
                gtin=gtin,
                formats=cfg.qr.formats,
                size_mm=cfg.qr.size_mm,
                ecc=cfg.qr.error_correction,
            )
            outcome["qr_paths"] = [str(p) for p in qr_paths]
            outcome["status"] = "ok"

            state.setdefault(gtin, {})[lang] = {
                "wp_page_id": page.id,
                "wp_url": page.url,
                "wp_featured_media_id": row.get("featured_media_id"),
                "content_hash": row["content_hash"],
                "last_run": outcome["ts"],
            }

        except Exception as exc:
            outcome["status"] = "error"
            outcome["error"] = repr(exc)

        with log_path.open("a") as f:
            f.write(json.dumps(outcome) + "\n")

    save_state(client_id, state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
```

### 10.6 Cost summary for end users

The tool itself is free (open-source, self-hosted). GS1 NL costs users incur are limited to what they already have:

| Product | Status |
|---|---|
| GS1 Data Source (data pool + MyGS1 UI + Excel export) | Client already pays — required to have GTINs |
| GS1 Digital Link API (write) | Free |
| GS1 Data Link (read API) | **Out of scope — the tool does not use it** |

Source: `https://www.gs1.nl/producten-services/data-exchange/tarieven/`.

---

## 11. Glossary

| Term | Definition |
|---|---|
| **GTIN** | Global Trade Item Number. 8/12/13/14 digits; pad to 14 for Digital Link URIs. |
| **GLN** | Global Location Number. `adminGln` in the API identifies the client account. |
| **GS1 Data Source** | The Dutch product data pool. Paid via Data Source contract. |
| **GDSN** | Global Data Synchronisation Network. The international standard Data Source implements. |
| **GS1 Digital Link** (standard) | Free *technical standard* for encoding GS1 identifiers as web URIs. |
| **GS1 Digital Link API** | GS1 NL's *free API endpoint* for setting resolver targets per GTIN. Requested via MyGS1. |
| **GS1 Data Link** (product) | GS1 NL's paid API-read product. **Out of scope for this project.** |
| **Resolver** | The web service that redirects a Digital Link URI to a configured target URL. |
| **Link type** | The kind of information requested by a scan: `pip`, `gs1:recipeInfo`, etc. |
| **QR code powered by GS1** | A standard QR symbol whose payload is a GS1 Digital Link URI. |
| **Sunrise 2027** | GS1's deadline for retailers to accept 2D barcodes at point-of-sale. |
| **MCP** | Model Context Protocol — connects tools/services to AI agents like Claude. |
| **Skill** | A reusable instruction set Claude loads when relevant. |
| **Application Password** | WordPress core feature for non-interactive REST API automation. |
| **Polylang / WPML** | The two dominant WordPress multilingual plugins. |
| **Azure API Management** | Platform GS1 NL uses; explains the `Ocp-Apim-Subscription-Key` header. |
| **MyGS1** | The portal where GS1 NL members manage Company Prefix, GTINs, and Data Source records — and now, Digital Link API keys. |

---

## 12. Document metadata

- **Version:** 0.9
- **Status:** Ready to build
- **Last updated:** 2026-07-11
- **Changes from 0.8:**
  - **§4.1/§4.2 auth corrected to OAuth2 client-credentials** (empirically confirmed in Phase 2, replacing the assumed static token + `auth_scheme` model): `POST /authorization/token` with `client_id`/`client_secret` headers → 1h Bearer JWT; the client mints/caches/refreshes it. `accountNumber` is **per-environment** (sandbox `8712345000010`, production `8712345000003`; the API also accepts the GLN). **GET/PATCH key on the GTIN AI `01`, not `Gtin`** (using `Gtin` 404s everything). Not-found confirmed as `400 "No valid contract found for Gtin with id: …"` (not 404). MyGS1-UI Digital Link activations are visible via the v2 API (same system). Production create/get/set_enabled verified end-to-end; the sandbox `21011` is a sandbox provisioning gap (no valid test GTINs/contract).
  - §5.1/§5.2 onboarding, §8.2, §9.1 R10, §10.1 `clients.example.yml`, and §10.2 `.env.example` updated to per-environment `client_id_env_*`/`client_secret_env_*` + `account_number_*`; `auth_scheme`/`token_env` retired.
- **Changes from 0.7:**
  - §4.1 rewritten for **Digital Link API v2**: host names captured (`gs1nl-api-acc.gs1.nl` test, `gs1nl-api.gs1.nl` production), path prefix `/digitallinkv2/v2/`, auth header changed from `Ocp-Apim-Subscription-Key` to `Authorization` token (Bearer default, raw as fallback via `gs1.auth_scheme`).
  - §4.2 rewritten with new endpoints (`/digitallinkv2/v2/digitallink` single, `/digitallinkv2/v2/digitallinks` bulk) and new `CreateOrUpdateRequest` body schema (`accountNumber`, `identificationKeyType`, `identificationKey`, `isEnabled`, `itemDescription`, `resolverSettings`, `links[]` with new required `mediaType`, `applicationIdentifiers[]`).
  - §5.2 step 5 smoke-test curl rewritten for v2 endpoint + Authorization header.
  - §9.1 R10 status changed from "Low–Medium" to **"Materialised — v2"** with note that the adapter pattern still applies for any future v3 auth changes.
  - §10.1 `clients.example.yml` restructured: `gs1.admin_gln` → `gs1.account_number`; `subscription_key_env_*` → `token_env_*`; added `api_version`, `auth_scheme`, `identification_key_type`, `resolver_settings` defaults, `default_media_type`.
  - §10.2 `.env.example`: env var names renamed `DEMOCLIENT_GS1_KEY_*` → `DEMOCLIENT_GS1_TOKEN_*` for accuracy.
  - GET endpoint captured: `GET /digitallinkv2/v2/digitalLink/{identificationKeyType}/{identificationKey}` (note mixed case `digitalLink` vs lowercase POST paths). Response `AdvancedDigitalLinkResponse` documented in §4.2. **Not-found HTTP status remains empirical** — v2 docs list 200/400/500, no 404.
  - PATCH `/activationStatus` endpoint documented in §4.2: toggle `isEnabled` without full-record rewrite; 204 on success.
  - ValidateDraft POST endpoint documented in §4.2: dry-run validation returning `isValid`, error message, available AIs, and `currentAnchorRelative`. **Note this endpoint lacks the `/v2/` path segment** (`/digitallinkv2/digitalLink/validateDraft`, not `.../v2/digitalLink/...`).
  - Standardized 400 error response body shape documented (`[{identifier, errors: [{code, message}]}]`) — enables structured error handling in the client.
  - Path anomalies (case-sensitivity, ValidateDraft path structure) collected in a table below the endpoints list; preserved exactly in the client until Phase 2 empirical verification.
- **Changes from 0.6:**
  - §5 preamble pointer added.
- **Changes from 0.5:**
  - §3.1 extended: full reasoning for GS1 NL resolver over self-hosted (incl. the Vercel evaluation)
  - §3.8 extended: historical rationale for Excel over GS1 Data Link API
  - §5.5 added: site discovery playbook
  - §9.1 R10 added: GS1 NL auth model change risk
  - §10.4 expanded
- **Changes from 0.4:**
  - §5.4 added: WordPress site setup — detailed reference (15 sub-items)
  - §5.3 checklist expanded
- **Changes from 0.3:**
  - Digital Link API confirmed free by GS1 NL; keys obtained for test and production
  - GS1 Data Link removed from scope
  - Multiple §-level cleanups
- **Open items:** None blocking. Development can begin immediately.

---

**End of document.**

## Cross-references

- [`IMPLEMENTATION_SPEC.md`](IMPLEMENTATION_SPEC.md) — the "how" companion
- [`architecture.svg`](architecture.svg) — system diagram
