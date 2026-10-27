# GS1 NL onboarding

What a client needs on the GS1 side before the tool can write a single Digital Link, and how the
integration actually authenticates.

Derived from `lib/gs1_dl_client.py` and `lib/config.py`.

## What you need

| Requirement | Why | Blocking? |
|---|---|---|
| A **GS1 Data Source** contract | It issued your GTINs and holds the product data. The MyGS1 Excel export is the tool's input. | Yes |
| A **Digital Link contract** on the same account | Authorises the Digital Link API write path. | **Yes — hard blocker** |
| OAuth2 **client id + client secret**, per environment | How the tool authenticates. Issued via MyGS1 / the developer portal. | Yes |
| The **account number**, per environment | Digital Links are created under it. | Yes |

> **The Digital Link contract is the one that gets missed.** Without it, every write returns `400 21011 "No valid contract found."` — with perfectly valid credentials. It is a GS1-side provisioning matter: no code change or config change fixes it, the account needs the contract added. It is a *different* contract from the Data Source one that gave you your GTINs. During this project's own build, a sandbox account had working credentials and no Digital Link contract, and that blocked the phase until GS1 provisioned it.

**Cost:** the Digital Link API this tool automates is **free**, as is the MyGS1 Excel export. The only
GS1 cost is the Data Source contract you already have. See [`costs.md`](costs.md).

## Authentication: OAuth2 client-credentials

The API uses **OAuth2 client-credentials with a short-lived JWT** — not a static API token.

```
POST https://{host}/authorization/token
  headers: client_id, client_secret
  -> {"access_token": "...", "token_type": "...", "expires_in": 3600}
```

The client mints the token on first use, caches it, and re-mints **60 s before expiry**. If the response omits `expires_in`, it assumes 3600 s. The token is then sent as a `Bearer` header on every call. The `accountNumber` claim inside the token is the account the entry is created under.

**A 4xx from the token endpoint raises `ConfigError`, not `GS1APIError`** — rejected credentials are a configuration fault, not an API outage. Errors are logged with the body scrubbed.

## Hosts and paths

| Environment | Host |
|---|---|
| `test` (acceptance / sandbox) | `gs1nl-api-acc.gs1.nl` |
| `production` | `gs1nl-api.gs1.nl` |

Selected by `gs1.environment` in `clients.yml`. The path prefix is `/digitallinkv2/v2/`.

Two path quirks are real and preserved deliberately — do not "fix" them:

- **Case differs by verb.** Writes use lowercase `digitallink` (`POST /digitallinkv2/v2/digitallink`);
  GET and PATCH use capital-L `digitalLink` (`/digitallinkv2/v2/digitalLink/01/{gtin14}`).
- **GET/PATCH key on the Application Identifier `01`**, not the literal string `Gtin`.
- ValidateDraft omits the `/v2/` segment.

## Configuration

```yaml
gs1:
  account_number_test: "..."
  account_number_production: "..."
  client_id_env_test: CLIENT_GS1_CLIENT_SANDBOX_ID
  client_secret_env_test: CLIENT_GS1_CLIENT_SANDBOX_SECRET
  client_id_env_production: CLIENT_GS1_CLIENT_ID
  client_secret_env_production: CLIENT_GS1_CLIENT_SECRET
  environment: test                 # test | production
  digital_link_url_pattern: "https://id.gs1.org/01/{gtin14}"
  batch_size: 50
  resolver_settings:
    use_gs1_resolver: true
    resolver_domain_name: null
```

These are env var **names**. The values belong in `.env` and nowhere else. `lib/config.py` is the authoritative field list including defaults.

Link entries are declared separately, one per link type:

```yaml
gs1_links:
  - link_type: "gs1:pip"           # product information page
    default: true
    public: true
    title_pattern: "..."           # optional
```

## Verifying resolution

After a write, check the resolver end to end. **Use GET.**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://id.gs1.org/01/{gtin14}       # -> 307
curl -sS -o /dev/null -w '%{http_code}\n' -L https://id.gs1.org/01/{gtin14}    # -> 200 at your page
```

> **`id.gs1.org` 404s to HEAD but 307s to GET.** A HEAD 404 is not evidence of a broken record. This
> costs an afternoon every time it is forgotten — `curl -I` is the wrong tool here.

Also confirm in MyGS1 that the entry is **enabled** and carries the expected link types per language.

### One QR, one default language

A bare `https://id.gs1.org/01/{gtin14}` resolves to the **default** language target only. There is no robust way to make a single printed QR route by the scanner's language — so a multilingual site should send the QR to the default language and let visitors switch languages on the site. The pilot made exactly this call.

## Deactivating an entry

**The v2 API has no DELETE.** An entry can only be switched off:

```bash
python -m scripts.run_unpublish --gtin {gtin} --dry-run   # preview
python -m scripts.run_unpublish --gtin {gtin}
```

`retract` PATCHes `isEnabled` to `false` — the API equivalent of clearing MyGS1's *"Activeer GS1 Digital Link"* checkbox. It **deliberately leaves the links intact**: deactivating already stops the entry resolving, so wiping the link/language/title configuration would buy nothing and cost the configuration a later reactivation would have to re-enter by hand.

Retracting a GTIN that has no entry returns `False` without writing, so it is safe in a teardown, and retracting twice re-sends the same end state.

**The consequence:** a deactivated, linkless record stays on the account **forever**. Therefore:

> Never point a smoke test at a real product's GTIN. Use a disposable GTIN in your company prefix and nothing else. The staging tests in this repo enforce that twice — the GTIN must sit in an allowlisted prefix, **and** a pre-flight aborts if a page exists that the tests did not create. Neither guard is sufficient alone: a real product's GTIN passes the prefix check, and the tests would then overwrite its live page with every ownership guard correctly passing.

## Safety guards

- **`OverwriteError`** — `safe_upsert` reads before it writes and refuses to replace an existing Digital Link unless `overwrite=True`, returning the prior snapshot for rollback. Mandatory for any production run.
- **The production guard** — a real `run_execute` against `environment: production` is refused with exit 2 unless `--i-understand-production` is passed.
- **Retry budgets** — 429 retries up to 5 times, 5xx and timeouts up to 3, both with exponential backoff. 400/401/403 are terminal and never retried.

## Errors

| Symptom | Meaning |
|---|---|
| `400 21011 "No valid contract found."` on a **write** | No Digital Link contract on the account. GS1-side. |
| The same 400 on a **GET** | Interpreted as "no entry exists"; `get()` returns `None`. Not an error. |
| `ConfigError: GS1 authorization rejected the credentials` | Wrong or expired client id/secret, or the wrong environment's pair. |
| `MissingCredentialError` | The env var named in `clients.yml` is not in the environment. Scripts load `.env` automatically, so check the value is there, non-empty, and spelled as `clients.yml` names it — see [`troubleshooting.md`](troubleshooting.md#missingcredentialerror-when-you-expected-the-credentials-to-be-there). |
| `GS1APIError` with `status_code == 0` | Transport failure below HTTP, not a server response. |
| `OverwriteError` | The GTIN already has a live entry. Confirm you mean to replace it. |

`GS1APIError` carries `error_results` (the parsed v2 `ErrorResult[]` when the body follows that shape), `response_body` (raw fallback), and `request_id`. **Quote the `request_id` when reporting anything to GS1.** Full reference: [`troubleshooting.md`](troubleshooting.md).

## See also

- [`setup.md`](setup.md) — install and the pipeline.
- [`wordpress-onboarding.md`](wordpress-onboarding.md) — the other half of a page's identity.
- [`troubleshooting.md`](troubleshooting.md) — every error type.
- `IMPLEMENTATION_SPEC.md` §4.3 (client shape), §5.1 (HTTP matrix), §6.3 (upsert idempotency).
