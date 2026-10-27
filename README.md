# GS1 Digital Link Orchestrator

Turn compliant product data in **GS1 Data Source** into printable, GS1-compliant
QR codes (a *QR code powered by GS1*, encoding a GS1 Digital Link URI) whose
resolver target points at the supplier's own website — and provision the
destination WordPress pages along the way.

The tool runs inside **Claude Code**: deterministic Python scripts do the
per-row work, while Claude handles planning, user interaction, and exception
cases.

**One client per repository.** You clone it, point it at your own export and
your own WordPress site, and run it locally. Credentials live in a gitignored
`.env`; `clients.yml` — also gitignored — holds the configuration and the
**names** of those environment variables, never their values. There are **no
central services**, nothing to host, and nothing of yours leaves your machine.

## Status

**`v0.5.0` is the first public release.** It was built against a live pilot: a Dutch
manufacturer's WordPress site in Dutch and French, and GS1 Netherlands' production resolver.
Both surfaces have been driven the whole way: the Claude Code flow through its confirmation
gates, and the desktop operator shell from first screen to finished run.

Expect rough edges outside the paths the pilot exercised, and read
[`docs/troubleshooting.md`](docs/troubleshooting.md) before pointing it at a new client. The
MCP servers are deliberately unpublished.

See [`CHANGELOG.md`](CHANGELOG.md) for what has shipped.

## Quickstart

```bash
git clone https://github.com/NextGenDataLead/gs1-digital-link.git
cd gs1-digital-link

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check && ruff format --check    # lint
mypy --strict lib                    # type-check
pytest                               # test suite

cp clients.example.yml clients.yml   # per-client config (gitignored)
cp .env.example .env                 # secrets only    (gitignored)
```

On Windows, install with `uv sync --python 3.11 --extra dev --extra ui --locked` and use
`.venv\Scripts\python` — [`docs/setup.md`](docs/setup.md#1-install).

Then **run it from Claude Code** — that is the operating surface, not an alternative to
the command line:

```
publish {client_id} to GS1
```

This loads the `flow-orchestrator` skill, which drives the pipeline and stops at every
operator gate: language selection → copy review → plan review → per-row diff → a mandatory
production environment-confirmation → execute → summary. Nothing proceeds without your
answer. **Claude.ai, Claude Desktop, and Claude Cowork are out of scope** — Cowork was
removed because it runs in a remote sandbox that would need your production credentials.

The Python scripts underneath (`inspect_export`, `parse_export`, `report_quality`,
`run_plan`, `run_execute`, …) are what Claude Code invokes for you. You run them directly
for install verification and when mapping a new client's export, not to publish.

There is also a **local operator shell** — a desktop window over the same commands, for the
recurring loop when a terminal is not the right surface:

```bash
pip install -e ".[ui]" && python -m ui
```

It subprocesses the same scripts, renders the same gates, and refuses to build a run command
while any required one is unanswered. It holds no LLM credential and never talks to Anthropic —
unless the client's `generator.api_key_env` names a variable that has a value, which turns on
generating copy from the Content screen.
See [`docs/ui-operator-shell.md`](docs/ui-operator-shell.md).

On a machine with no Python it installs by double-click instead — `install.command` /
`install.bat` fetches `uv`, has it fetch Python 3.11, and builds the environment from the
committed `uv.lock`; `start.command` / `start.bat` opens the window. See
[`docs/operator-install.md`](docs/operator-install.md).

**Full walkthrough, including onboarding a new client: [`docs/setup.md`](docs/setup.md).**

## Documentation

[**docs/README.md**](docs/README.md) is the index. The two entry points, because they are
different people:

- **Publishing products?** → [docs/operator-install.md](docs/operator-install.md) to install,
  then [**docs/operator-guide.md**](docs/operator-guide.md) to run a batch. No terminal needed
  for either.
- **Working on the code?** → [docs/setup.md](docs/setup.md), then
  [docs/troubleshooting.md](docs/troubleshooting.md).

The operator's machine:

- [docs/operator-guide.md](docs/operator-guide.md) — the walkthrough of a batch, screen by screen,
  with screenshots and a glossary.
- [docs/operator-install.md](docs/operator-install.md) — the double-click install, the five files
  that arrive by hand, and what IT is being asked to allow.
- [docs/ui-operator-shell.md](docs/ui-operator-shell.md) — why each screen is built as it is, and where the safety
  actually lives. For whoever maintains the shell, not for the operator.

The two external systems:

- [docs/gs1-nl-onboarding.md](docs/gs1-nl-onboarding.md) — contracts, OAuth2, resolution, retraction.
- [docs/wordpress-onboarding.md](docs/wordpress-onboarding.md) — app passwords, post types, WPML, media.

Data in, page out:

- [docs/data-source-export-schema.md](docs/data-source-export-schema.md) — reading a GDSN or flat export.
- [docs/template-variables.md](docs/template-variables.md) — `acf_map` and template variables.
- [docs/costs.md](docs/costs.md) — what running this costs.
- [docs/architecture.svg](docs/architecture.svg) — end-to-end diagram.

The two authoritative design documents — read them before contributing:

- **[docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md)** — the *why*: scope,
  architecture decisions, phase plan, risks, and reference artifacts.
- **[docs/IMPLEMENTATION_SPEC.md](docs/IMPLEMENTATION_SPEC.md)** — the *how*:
  types, module contracts, error handling, idempotency, and Definition of Done
  per phase (§12).

Also: [docs/clients/](docs/clients/) — per-client notes, and the template for a local
live-publish log.

## Cost to users

The tool itself is free (open-source, self-hosted). The only GS1 NL cost a user
incurs is their **existing GS1 Data Source contract** — the same one that gave
them their GTINs. The GS1 Digital Link API (the write path this tool automates)
is free of charge, and the Excel export from MyGS1 is a standard feature. GS1
Data Link (the paid read API) is explicitly out of scope. The one metered
component is optional: see [docs/costs.md](docs/costs.md).

## Safety

This tool writes to a live website and creates **permanent** GS1 records — the Digital Link
API has no DELETE, so a record can only be deactivated, never removed. Accordingly:

- Dry-run before every real run.
- A live production run requires `--i-understand-production`.
- `pytest` deselects the staging tests by default; they write to live WordPress and GS1
  production. Read `.env.example`'s staging block before running `pytest -m staging`.
- Never point a smoke test at a real product's GTIN.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). CI runs `ruff check`, `ruff format --check`,
`mypy --strict lib`, and `pytest` on every push — all four must pass.

## License

[MIT](LICENSE).
