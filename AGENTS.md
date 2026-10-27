# GS1 Digital Link Orchestrator — working notes for Codex

This tool publishes real product pages to a live WordPress site and registers **permanent** records
in the GS1 production resolver. Read [`docs/setup.md`](docs/setup.md) before acting on anything here.

## Publishing goes through `flow-orchestrator`. Always.

The operator gates live **only** in [`.agents/skills/flow-orchestrator/SKILL.md`](.agents/skills/flow-orchestrator/SKILL.md).
Calling `scripts/run_execute.py` directly bypasses every one of them.

When the operator asks to publish — *"publish {client} to GS1"*, *"run the GS1 pipeline for
{client}"*, or the older short forms *"run for {client}"* / *"process {client}"* — **load that skill
and follow it step by step.** If for any reason it is not available as a skill, read the file and
follow it anyway. Do not improvise an equivalent flow: the gates *are* the safety mechanism.

**Publishing has three modes, all through that one sequence:** `/gs1-pages` (WordPress pages only —
reversible), `/gs1-links` (Digital Links only, aimed at pages that already exist — **permanent**),
and `/gs1-publish` (both). Those three skills are thin: each pins the mode and delegates to
`flow-orchestrator`, which supplies `run_execute --only`. A request phrased in plain English for one
leg goes to `flow-orchestrator` too — it classifies the mode at gate 0 and confirms it. Never guess
toward the more destructive mode.

The other five skills in `.agents/skills/` cover the individual steps (parse, generate copy, pages,
Digital Link, QR) and have their own trigger phrases.

## Invariants — these were each learned the hard way

- **A GS1 Digital Link record can never be deleted.** The v2 API has no DELETE; retraction only
  clears links and disables the record. Every write against a real GTIN is permanent.
- **A real production run needs `--i-understand-production`.** The skill appends it *after* the
  operator confirms at a gate — never before, and never on the operator's behalf. (In `pages` mode
  that confirmation is gate 0; the separate environment gate is skipped there because nothing
  irreversible follows.)
- **`--only links` refuses a GTIN whose target URL does not serve.** That check is in
  `run_execute`, not in a skill, precisely because prose can be skipped. Never route around it — fix
  where the page actually is.
- **Dry-run first, always.** `--dry-run` writes nothing.
- **The ACF write path fails silently.** A `200` proves the post exists, not that its fields landed.
  Verify by fetching the rendered HTML.
- **Test resolution with `GET`, not `HEAD`.** `id.gs1.org` 404s to HEAD and 307s to GET.
- **Never run `pytest -m staging` casually.** Those tests write to live WordPress and GS1 production.
  A bare `pytest` is safe: `addopts = "-m 'not staging'"` deselects them.
- **Never invent product data.** Blank or wrong source values get fixed in MyGS1, not filled in
  downstream. `python -m scripts.report_quality` is how they surface.

## There is a second surface: the local operator shell

`pip install -e ".[dev,ui]"` then `python -m ui` — a desktop window over the same commands, for an
operator repeating a known loop. It **subprocesses the scripts and must never import their
`main()`** (see the `.env` rule below), and `ui/session.py` **raises** rather than building a run
command while a required gate is unanswered. It holds no LLM credential and never reaches
Anthropic **unless the client's `generator.api_key_env` names a variable with a value** (the
example config points it at `ANTHROPIC_API_KEY`, but the name is per-client) — that is what turns
on the Content screen's
Generate button, and even then the key is read by the `run_generate` subprocess, never by `ui/`.

On the operator's machine it installs by double-click instead — `install.command` / `install.bat`,
then `start.command` / `start.bat` — from the **committed `uv.lock`**. Do not run those in a
development clone: they replace `.venv` with a 3.11 environment that has `ui` but not `dev`. After
touching `pyproject.toml`, run **`uv lock`** and commit it, or the operator's `uv sync --locked`
refuses to install; `tests/test_packaging.py` and CI's `uv lock --check` both catch that.
The operator-facing docs are `docs/operator-install.md` (getting it onto their machine) and
`docs/operator-guide.md` (running a batch — screenshots, glossary, no terminal). Keep them free of
module names and test files; `docs/ui-operator-shell.md` is where the rationale goes, and it says
so at the top. `docs/README.md` routes by reader.

It is also the only thing that **writes `clients.yml` and `.env`**, on the Setup screen.
`ui/config_edit.py` edits the YAML **as text** — that file is a document whose comments are often
the only record of why a value is what it is, so it is never round-tripped through a YAML dumper —
and validates the candidate with `lib/preflight.check_config` before replacing anything. It still
does not *load* `.env`; it reads it only far enough to say whether a name has a value.

**Never rehearse a screen against a real client's data.** Driving the Data screen with Playwright
uploads through the picker, which replaces `input/{client}/process-list.xlsx` *in place*. Use the throwaway
`democlient` and give it data with **`python -m scripts.make_demo_export`** — a 24-sheet synthetic
GDSN export plus a scope list, written to the paths the config declares, on GS1 prefix `029`
(restricted distribution, never issued, so no demo barcode can name a real product). It defaults to
`clients.example.yml`, where `democlient` lives, and **refuses to overwrite an existing file
without `--force`**, because the one thing it could destroy is a client's real export. Twelve
products: nine publish, three are held — one by each mandatory rule — and the scope list carries one
barcode no export row does. `lib/demo_export.py` is what it is made of, `lib/gdsn_layout.py` the
header shape, and `tests/lib/test_demo_export.py` checks both against `clients.example.yml` rather
than restating it.

The gates themselves live in **`lib/gates.py`** as data, and `flow-orchestrator/SKILL.md` carries a
**Gate index** table that `tests/lib/test_gates.py` checks in both directions. Adding a gate to one
without the other fails CI — that check exists because two implementations of one safety contract
drift silently. Read `docs/ui-operator-shell.md` before changing either.

## Layout

- `lib/` — the library. `scripts/` — fourteen CLI entry points. `ui/` — the operator shell (optional
  `[ui]` extra; nothing in `lib/` or `scripts/` imports it, and the suite passes without it).
  `mcps/` — three TypeScript MCP servers (unpublished by choice).
- **Run `python -m scripts.doctor` before a wave.** It is the preflight: config, scope, generated
  copy for this run, credentials, reachability. Exit 1 on any failure; `--offline` skips everything needing
  a secret or a socket, `--json` is what the shell parses.
- **Credentials come from `.env`**, loaded by `lib/env.py` `load_env()` from each script's
  `if __name__ == "__main__":` block — **never from `main()`**, which the tests call directly, and
  **never from anywhere under `ui/`**, which would put production secrets in a long-lived desktop
  process and arm the staging guards inside it. `tests/lib/test_env.py` enforces both with an AST
  check; it is not boilerplate.
- `clients.yml` (gitignored) holds config and the **names** of env vars, never values. `client_id` is
  optional on every script when exactly one client is defined.
- CI: `ruff check`, `ruff format --check`, `mypy --strict lib`, `pytest`.
- `main` is branch-protected — changes go through a PR.
