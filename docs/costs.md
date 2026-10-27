# Costs

What running this tool costs. Short answer: **the tool is free, and for the default in-session workflow
there is no incremental cost at all.**

## The tool

Free. Open-source (MIT), self-hosted, no central services, nothing to host. You run scripts on your own
machine against your own WordPress site and your own GS1 account. There are no per-seat, per-GTIN, or
per-page fees, and no telemetry.

## GS1

| Item | Cost |
|---|---|
| **GS1 Data Source** contract (data pool + MyGS1 + Excel export) | **Already paid** — it is what issued your GTINs |
| **Digital Link contract** on the account | Required, and must be provisioned by GS1 — see [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) |
| **GS1 Digital Link API** (the write path this tool automates) | **Free** |
| MyGS1 Excel export | **Free** — a standard feature |
| **GS1 Data Link** (the paid read API) | **Out of scope — this tool does not use it** |

So the only GS1 cost is a contract you already hold. Source:
`https://www.gs1.nl/producten-services/data-exchange/tarieven/` — confirm current tariffs there.

## WordPress

Whatever you already pay for hosting. The tool adds pages, attachments, and REST traffic to a site you
already run. Two things worth budgeting for:

- **Media storage.** Images are converted to ~1600 px web JPEGs, so they are small — but you get one
  hero per product, plus a transcoded MP4 per product per language if you publish video. Video
  dominates the footprint.
- **Upload limits.** You may need `upload_max_filesize` / `post_max_size` raised, which on some managed
  hosts means a plan change.

## Content generation — the only variable cost

This is the one place the tool can spend money, and it is **optional in two senses**: only clients with
`generator.enabled: true` use it at all, and even then the default backend is free.

### The in-session producer — free

```bash
python -m scripts.run_generate --emit      # queue pending requests
python -m scripts.run_generate --validate  # check the results against this run
```

Claude writes the copy **inside your Claude Code session**. It needs **no API key** and adds **no API
charge** — it is part of the session you are already having. This is the default and it is what the
pilot used.

### The headless API backend — metered

```bash
python -m scripts.run_generate --backend api
```

This calls the Anthropic Messages API directly (`lib/llm.py`) and is billed per token. Use it for
unattended runs; otherwise prefer the in-session path.

Configured per client:

```yaml
generator:
  enabled: true
  model: claude-sonnet-5        # the default
  max_tokens: 1024
  api_key_env: ANTHROPIC_API_KEY
```

**Pricing** (per million tokens, Anthropic first-party API):

| Model | Input | Output |
|---|---|---|
| `claude-sonnet-5` (default) | $3.00 — **$2.00 introductory through 2026-08-31** | $15.00 — **$10.00 introductory** |
| `claude-opus-5` | $5.00 | $25.00 |
| `claude-haiku-4-5` | $1.00 | $5.00 |

Verify current rates at `https://platform.claude.com/docs/en/pricing` before budgeting — these change.

### What a run actually costs

Each unit of work is **one `(GTIN, language)` pair**, and each is a short, structured request: the
product's context in, one forced `produce_copy` tool call out, capped at `max_tokens` (default 1024).
Output is genuinely short — a tagline plus a handful of bullets.

```
cost ≈ (units × input_tokens  ÷ 1e6 × input_rate)
     + (units × output_tokens ÷ 1e6 × output_rate)
```

Illustrative, at ~1.5k input and ~400 output tokens per unit on `claude-sonnet-5`:

| Scale | Units | At introductory $2 / $10 | At standard $3 / $15 |
|---|---|---|---|
| One product, nl + fr | 2 | well under $0.01 | well under $0.01 |
| A 20-product batch, nl + fr | 40 | ~$0.28 | ~$0.42 |
| A ~130-product catalogue, nl + fr | 254 | ~$1.75 | ~$2.65 |

**Single-digit dollars for a whole catalogue.** Measure your own token counts rather than trusting the
per-unit estimate above — `client.messages.count_tokens()` gives real numbers for your prompt and
product data.

### What keeps it low — and what does not

- **Re-running re-pays, but only for what the re-run publishes.** There is no cache: copy is written
  fresh for every run and never stored. What a run *asks* for, though, is the rows it will actually
  execute — the NEW and CHANGED ones. Publishing 20 GTINs in waves therefore costs roughly those 20
  once, not the whole scope per wave: each wave re-pays for its own rows, and the GTINs already live
  from an earlier wave are not generated for at all.
  The no-cache half was a deliberate trade: at ~$1.75–$2.65 for a ~130-product catalogue, cost is not the constraint, and a store that decides when to skip work is
  one more thing that can be quietly wrong. Idempotency comes from the other end instead — generated
  copy is excluded from the content hash, so re-writing it does not republish a page. **Scoping to
  NEW/CHANGED is not a cache in disguise:** a unit is skipped because nothing will be published for
  it, never because copy for it exists somewhere.
- **The feed's own copy is free.** A product whose attr 1067 is short enough to publish verbatim never
  reaches a producer at all: `merge_generated` takes it straight from the feed, every run, at no cost.
- **Held rows cost nothing.** A GTIN with no usable source input is skipped from the plan (E21) rather
  than sent to the model to have copy invented for it. Fixing source data in MyGS1 is both cheaper and
  more correct than generating around a gap.
- **Prompt caching would cut it further.** The bulk of each request is the shared system prompt and
  voice guide; cache reads bill at ~0.1×. Not currently wired up — for a catalogue that costs a couple
  of dollars, it has not been worth the complexity.

## Not a cost

- **QR rendering** — local, `qrcode[pil]`, free at any volume.
- **Re-runs** — idempotent *in what they publish*. Re-running converges on the same state; it does
  not duplicate pages, attachments, or GS1 records. It **does** re-pay for generated content, which
  is the trade described above.
- **Dry runs** — `--dry-run` performs no writes and makes no billable calls.

## Summary

| | Cost |
|---|---|
| The tool | Free |
| GS1 Digital Link API | Free |
| GS1 Data Source contract | Already paid |
| WordPress | Existing hosting |
| Content generation, in-session | Free — no API key |
| Content generation, `--backend api` | A few dollars per full catalogue |
| QR rendering | Free |

## See also

- [`setup.md`](setup.md) — the pipeline.
- [`template-variables.md`](template-variables.md#generated-content) — how generated content is produced.
- [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) — the contracts you need.
