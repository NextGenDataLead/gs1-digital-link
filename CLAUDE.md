# GS1 Digital Link Orchestrator — working notes for Claude Code

This tool publishes real product pages to a live WordPress site and registers **permanent** records
in the GS1 production resolver. Read [`docs/setup.md`](docs/setup.md) before acting on anything here.

## Publishing goes through `flow-orchestrator`. Always.

The operator gates live **only** in [`.claude/skills/flow-orchestrator/SKILL.md`](.claude/skills/flow-orchestrator/SKILL.md).
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
toward the more destructive mode. (In the operator shell the mode is chosen with the batch, on the
Data screen, and gate 0 shows it for confirmation — it is never defaulted.)

The other five skills in `.claude/skills/` cover the individual steps (parse, generate copy, pages,
Digital Link, QR) and have their own trigger phrases.

**Moving the work to or from another machine** (a handover pack, in any wording, Dutch included)
goes through [`.claude/skills/gs1-handover/SKILL.md`](.claude/skills/gs1-handover/SKILL.md): it
checks the ledger is current, asks about the passwords, dry-runs before receiving, and says what
happens next. Never copy `state.json` by hand instead.

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
than restating it. It also writes the videos (`lib/demo_videos.py`): placeholder files, a
`mapping.yml` in the row-per-line shape the editor accepts, and `test-uploads/video-signoff.xlsx` —
so the video half of the Data screen rehearses too. With `publish_without_video` on (as the example
sets it), nine of the twelve are eligible and two of those go live without a video somewhere; three
are not eligible — two for missing data, one for two Dutch videos. Applying the sheet takes the
missing videos from two to one.

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
- **`GS1_DATA_DIR` separates data from code** (`lib/data_dir.py`). Unset, the repo is the data folder,
  as it always was. Set, `.env`, `clients.yml`, `input/` and `output/` are read there, and every
  entry point `enter_data_dir()`s into it from its `__main__` block — same rule, same AST test as
  `load_env()`. Never build a data path from the code root; `tests/lib/test_data_dir.py` checks.
- **When more than one person publishes, only the ledger holder publishes.**
  The ledger moves between machines with `python -m scripts.handover pack` / `unpack` (the
  `gs1-handover` skill walks it)
  (`lib/handover.py`), never by copying `state.json`; `unpack` refuses when the ledger here knows a
  write the pack's does not. Before any publish, ask who holds it if that is not established.
- **On Windows** the clone is installed with
  `uv sync --python 3.11 --extra dev --extra ui --locked`, the interpreter is `.venv/Scripts/python`
  from Git Bash, and `GS1_DATA_DIR` names the one data folder the app also uses. A clone with no
  `GS1_DATA_DIR` and no `clients.yml` has no ledger: stop and say so rather than create one. CI runs
  pytest on `windows-latest` too (ruff and mypy on Linux only).
- **The installers record a data folder** (`lib/install_location.py`, run as `scripts.data_folder`):
  remembered → the program folder if it already holds data → a new one in home. `start` passes it
  as `GS1_DATA_DIR`. It never writes into a folder that already holds data, and refuses rather
  than guess when the record is stale or two folders hold data — each guess is a second ledger.
  In a dev clone `start` resolves to the clone itself and writes nothing.
- **Two install routes.** The double-click `uv` installers are the default; the container image
  (`Dockerfile`, `compose.yml`, published to GHCR on a `v*` tag) is for machines where IT runs
  containers. The image holds code only — data is the `/data` volume — and `tests/test_container.py`
  keeps `.env`, `clients.yml`, `input/` and `output/` out of it and the port on `127.0.0.1`.
- `clients.yml` (gitignored) holds config and the **names** of env vars, never values. `client_id` is
  optional on every script when exactly one client is defined. The live one is in the data folder;
  its reviewed copy is committed at **`config/{client_id}.yml`** (`lib/committed_config.py`,
  `python -m scripts.config_sync check|save|take`), and preflight warns when they differ. `.env` is
  never committed: credentials are per person.
- CI: `ruff check`, `ruff format --check`, `mypy --strict lib`, `pytest`.
- `main` is branch-protected — changes go through a PR.
