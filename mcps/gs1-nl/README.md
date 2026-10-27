# gs1-nl-mcp

MCP server wrapping the **GS1 NL Digital Link API v2**. Exposes three tools
(IMPLEMENTATION_SPEC §9.1):

| Tool | Purpose | Gated |
|---|---|---|
| `gs1_digital_link_upsert` | Set/update the resolver target for one GTIN | intent, + production |
| `gs1_digital_link_upsert_bulk` | Bulk variant; batches into `batch_size` internally | intent, + production |
| `gs1_digital_link_get` | Fetch the current entry for a GTIN (`null` if not found) | no — read-only |

## The writes are gated, and fail closed

**A GS1 Digital Link record can never be deleted.** The v2 API has no DELETE; retraction only
clears links and disables the record. So the two write tools do not execute on a model's say-so:
each asks the **operator** first, through MCP elicitation, which puts the question to the human
running the client rather than to the assistant calling the tool.

The gates are the ones in [`lib/gates.py`](../../lib/gates.py) — the same contract
`flow-orchestrator` uses, read from a generated module (see below), not restated here. `intent`
(step 0) names what is about to be written and whether it is permanent. Against a `production`
environment the `production` gate (step 8) is asked as well, second and separately, because
`lib/gates.py` marks it "mandatory, non-overridable, and enforced per run rather than per session".

**A client that cannot ask a human cannot write.** `elicitInput` throws when the client has not
declared the `elicitation` capability, and the tools let that refusal stand rather than proceeding
unattended — the alternative would be a gate that disappears exactly when no one is watching.
A declined *or dismissed* prompt is a refusal; consent is never inferred.

`src/gates.generated.ts` is written by `python -m scripts.export_gates` from `lib/gates.py`.
Do not edit it: `tests/lib/test_gates_export.py` and a CI step both fail when it is stale, so the
safety contract has one source rather than one per language.

The tools hide plumbing (`accountNumber`, `resolverSettings`, credentials) and
resolve it from `clients.yml` by `client_id`. The HTTP client mirrors the
authoritative Python client (`lib/gs1_dl_client.py`): identical hosts, path prefix,
path-case anomalies (capital-L `digitalLink` for GET, lowercase for POST),
OAuth2 token minting, and the retry policy (§4.3 / §5.1).

## Configuration

Resolved per call from `clients.yml`:

- **File location** — `clients.yml` in the working directory, or set `GS1_CLIENTS_FILE`.
- **Auth (OAuth2 client-credentials)** — the client mints a short-lived JWT from
  the `client_id`/`client_secret` env vars named by `gs1.client_id_env_test` /
  `client_secret_env_test` (or the `_production` pair when
  `environment: production`), caches it until it nears expiry, and sends it as a
  Bearer token. Credentials and token are never logged.
- **Account** — `gs1.account_number_test` / `account_number_production` (differs
  per environment).

## Develop

```bash
npm ci                       # from repo root (npm workspaces)
npm -w mcps/gs1-nl run build # tsc -> dist/
npm -w mcps/gs1-nl test      # vitest
npm -w mcps/gs1-nl start     # serve over stdio
```

## Status

Code-complete and unit-tested against mocked HTTP and an in-memory MCP transport.
The real-GTIN test-environment call (and confirmation of Bearer-vs-raw and the
not-found status code) is pending captured fixtures / a test token — see
IMPLEMENTATION_SPEC §13.2.
