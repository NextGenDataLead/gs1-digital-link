# Setup

From a clean machine to a validated dry run, then onboarding a client of your own.

**This tool is driven from Claude Code or from the local operator shell — not by typing the script commands yourself** — see [Two ways to drive it](#two-ways-to-drive-it) before anything else, because it changes how you should read the rest of this file.

Every command here is verified against the code at HEAD, not against a plan. If a command in this file does not do what it says, the file is wrong — please fix it.

- New to the project? Read this top to bottom.
- Onboarding a **second** client? Skip to [Onboard a client](#onboard-a-client).
- Just want to publish a wave? [Running it](#4-running-it).
- Something broke? [`troubleshooting.md`](troubleshooting.md).

## What you are setting up

The tool turns a GS1 Data Source export into (a) WordPress product pages, one per `(GTIN, language)`, and (b) GS1 Digital Link resolver entries whose QR codes point at those pages. There is **no server and nothing to host** — everything runs locally, against your own WordPress site and your own GS1 account, with your own credentials.

## Two ways to drive it

> **Two sanctioned surfaces, one set of gates. Typing the script commands yourself is neither of them.**
>
> **From Claude Code** — normally with a slash command, **`/gs1-publish`** (or `/gs1-pages` / `/gs1-links` for one leg) — which loads the `flow-orchestrator` skill, walks you through the operator gates, and invokes the Python scripts for you. Plain language works too (*"publish {client_id} to GS1"*), but the slash command is preferred: it pins the mode instead of leaving it to be inferred. See [Which flow do you need?](#which-flow-do-you-need).
>
> **From the local operator shell** — `python -m ui`, a desktop window over the same commands, for the recurring loop when a terminal is not the right surface. It subprocesses the same scripts, renders the same gates from the same source, and **refuses to build a run command while any required gate is unanswered**. It holds no LLM credential and never talks to Anthropic unless the client's `generator.api_key_env` names a variable that has a value, which enables generating copy from its Content screen. See [`ui-operator-shell.md`](ui-operator-shell.md), and [`operator-install.md`](operator-install.md) for the double-click install that puts it on a machine with no Python.
>
> Pick by who is doing the work, not by capability. The shell is for an operator repeating a known loop; Claude Code is for onboarding a client, diagnosing something odd, or any step where the answer is not already known.
>
> **Claude.ai, Claude Desktop, and Claude Cowork are explicitly out of scope.** Cowork was evaluated and removed: it executes in a remote cloud sandbox, which would mean handing production WordPress and GS1 credentials to an environment outside your control, and its network egress to `www.democlient.nl` was unproven. Both sanctioned surfaces run on your machine with your credentials staying on it.

So why does this document list Python commands at all? Three reasons, and none of them is "type these during a normal run":

1. **Verifying the install** — §1 and §2 below are genuinely something you run once, by hand.
2. **Knowing what is being run on your behalf** — both surfaces invoke exactly these commands, and the gates they present map onto them. When something fails, [`troubleshooting.md`](troubleshooting.md) talks about them by name.
3. **Onboarding a new client**, where the read-only inspect/parse loop is iterative and hands-on.

For a real publishing run, use one of the two surfaces above — see [Running it](#4-running-it).

## Prerequisites

| | Requirement | Notes |
|---|---|---|
| **Claude Code** | required for onboarding; optional for a routine wave | One of the two operating surfaces. The other is the local shell (`.[ui]` extra), which needs no Claude Code. Onboarding a client still wants it. See the note above. |
| Python | **3.11 or newer** | `requires-python = ">=3.11"` in `pyproject.toml`. CI pins 3.11; the suite also passes on 3.14. |
| Node.js | 20 or newer | Only needed to build the MCP servers in `mcps/`. The Python pipeline does not need it. |
| Git | any recent | |
| `ffmpeg` | **video only** — images use Pillow, already installed | Only consulted when a client sets `media.video_transcode: true`, but the pilot needs it. Image conversion has no external dependency; see [Why media gets converted](#why-media-gets-converted) for the two separate problems. |
| GS1 | a Data Source contract **and** a Digital Link contract | See [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md). Without the Digital Link contract every write fails `400 21011`. |
| WordPress | 5.6+, REST reachable over HTTPS | See [`wordpress-onboarding.md`](wordpress-onboarding.md). |

### Why media gets converted

Two different problems, two different fixes — worth knowing because both are silent if you skip them.

**Images — WordPress rejects the source files.** GDSN feeds carry **print masters**: in the pilot, 92% were `image/tiff` and many ran 10–45 MB at 3200×3200. WordPress will not accept those. So `lib/media.convert_image_for_web` converts **every** image — TIFF, PNG, and already-JPEG alike — to a baseline web JPEG capped at `media.image_max_dim` (default 1600 px). Converting uniformly rather than only when needed also makes the output byte-deterministic, so re-runs reuse the existing attachment instead of piling up duplicates. **No extra tooling needed** — this is Pillow, already a dependency.

**Video — the browser won't play the source files.** The operator's video folders hold `.mpg` / `.mpeg`, which are **MPEG-1/2**. WordPress will generally *store* those, but an HTML5 `<video>` element **cannot play them**, so the page publishes with a video that silently does nothing. That is what `media.video_transcode: true` fixes: `lib/media_video.prepare_video` shells out to **ffmpeg** to produce an H.264/AAC MP4 with `faststart` for web streaming and metadata stripped.

**This is the one step with an external binary dependency.** If `ffmpeg` is missing or fails, the transcode returns `None`, the video is skipped with a warning, and the page still publishes — so a missing `ffmpeg` costs you the video, not the run. Install it (`brew install ffmpeg` on macOS, `winget install Gyan.FFmpeg` on Windows, then a new terminal) before a wave that includes video, or override the path with `media.ffmpeg_bin`.

## 1. Install

```bash
git clone https://github.com/NextGenDataLead/gs1-digital-link.git
cd gs1-digital-link

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

**On Windows**, or anywhere `uv` is installed (`winget install astral-sh.uv`), install from the
lockfile instead — the same environment CI's Windows job tests:

```
git clone https://github.com/NextGenDataLead/gs1-digital-link.git
cd gs1-digital-link
uv sync --python 3.11 --extra dev --extra ui --locked
.venv\Scripts\activate
```

The interpreter is then `.venv\Scripts\python` (from Git Bash, which Claude Code uses on Windows:
`.venv/Scripts/python`). If this clone is to work on an installation's real data, set
`GS1_DATA_DIR` to that data folder (`setx GS1_DATA_DIR "%USERPROFILE%\GS1 Digital Link data"`,
then a new terminal) — never copy `clients.yml`, `.env`, `input/` or `output/` into the clone,
which would be a second data folder with a second ledger.

Add `ui` to that if you want the local operator shell — `pip install -e ".[dev,ui]"`. It is an
optional extra rather than a dependency: NiceGUI pulls FastAPI, uvicorn, pywebview and bundled
Vue/Quasar assets, none of which any publishing path touches, and the test suite and
`mypy --strict lib` both pass without it. See [Two ways to drive it](#two-ways-to-drive-it) below.

> **This is the development install, and it is not what the operator does.** On their machine
> nothing is preinstalled and nothing is typed: `install.command` (macOS) or `install.bat`
> (Windows) fetches `uv`, has `uv` fetch Python 3.11, and builds `.venv` from the committed
> `uv.lock` — so that machine gets the versions that were tested rather than whatever resolves
> that day. Then `start.command` / `start.bat` opens the shell. See
> [`operator-install.md`](operator-install.md).
>
> **Do not run `install.command` in this clone.** It replaces `.venv` with a Python 3.11
> environment holding the `ui` extra and *not* `dev`, so `pytest`, `mypy` and `ruff` vanish from
> it. And after changing `pyproject.toml`, run **`uv lock`** and commit the result — the
> operator's install refuses to resolve anything the lockfile does not already have.

## 2. Verify the install

These are the four commands CI runs (`.github/workflows/ci.yml`). All four must pass before you trust anything else.

```bash
ruff check                 # lint
ruff format --check        # formatting
mypy --strict lib          # type-check
pytest                     # test suite
```

Expected, on a clean checkout:

```
All checks passed!
142 files already formatted
Success: no issues found in 24 source files
725 passed, 2 skipped, 5 deselected
```

**Only the last two lines are worth comparing.** The formatted-file count moves whenever anyone adds
a file — ruff 0.16 formats Python blocks inside Markdown, so every document is counted alongside the
Python files. A different number there means the docs changed, not that anything is wrong. What
matters is that each command exits **0**.

CI runs one more check after these four: **`uv lock --check`**, which fails if `uv.lock` has drifted
from `pyproject.toml`. It needs `uv` installed, so it is not in the list above; `pytest` covers the
same drift offline in `tests/test_packaging.py`.

**Why a bare `pytest` is safe.** `pyproject.toml` sets `addopts = "-m 'not staging'"`. The 5 deselected tests are the staging integration tests — they write to a **live** WordPress site and the **GS1 production** resolver. They are deselected by default deliberately, because relying on their `skipif` env-var guard was not enough: a shell that had sourced `.env` satisfied that guard, and a bare `pytest` then hit production. Do not run `pytest -m staging` unless you have read `.env.example`'s staging block in full and understand that a GS1 record **cannot be deleted**.

The 2 skipped tests need optional local tooling and skipping them is normal.

## 3. Configure

Two files, both gitignored, neither ever committed:

```bash
cp clients.example.yml clients.yml   # per-client, non-secret configuration
cp .env.example .env                 # secrets only
```

**`clients.example.yml` is a template to replace, not a config to adopt.** It ships with one worked example client (`democlient:`) because an empty skeleton teaches nothing — the example shows what a fully-tuned client looks like, `gdsn_map` and `acf_map` and all. When you onboard your own client you **replace that block**, you do not publish alongside it.

It is nonetheless useful exactly once, before you have any real credentials, as an **install smoke test** — it is a known-good file, so if the loader parses it your install is sound and any later failure is your config, not your environment:

```bash
python -c "from lib.config import load_clients; print(sorted(load_clients('clients.yml')))"
# -> ['democlient']      # proves the loader works. It does NOT mean you are set up for Democlient.
```

**Delete or replace the `democlient:` block before you configure your own client** — and do it properly, because the single-client default makes a leftover example load-bearing. With exactly one client defined, commands infer it and act on it without you naming it. So a `clients.yml` containing only the stale example means a bare `python -m scripts.run_plan` acts on **that** example; and a `clients.yml` containing the example *plus* yours makes the id mandatory again, so every command fails until you name one. Neither is dangerous — the example points at a site you have no credentials for — but both waste time.

### Where the data lives — `GS1_DATA_DIR`

By default the repository folder is also the data folder: `.env`, `clients.yml`, `input/` and
`output/` (and in it `state.json`, the ledger of permanent GS1 writes) sit beside the code. Set
**`GS1_DATA_DIR`** to an existing folder to keep them there instead, so that replacing or updating
the code can never replace the ledger:

```bash
export GS1_DATA_DIR=~/gs1-data   # must exist; a missing folder is an error, not a fallback
python -m scripts.doctor --offline   # reads ~/gs1-data/clients.yml, runs inside ~/gs1-data
```

Every script and the shell then work *inside* that folder, so relative paths — in `clients.yml`
and on the command line — resolve against it. What ships with the code stays with the code:
`schema/`, `reference/`, `templates/_default/` and `clients.example.yml`. A client's own voice
prompt and page templates are data: `input/{client}/prompts/` and `input/{client}/templates/` in
the data folder, with the program folder's `prompts/{client}/` and `templates/{client}/` as the
fallback. Unset, nothing changes.

### Secrets

`clients.yml` holds only the **names** of environment variables — never a value. The values live in **`.env` in the data folder — the repository root unless `GS1_DATA_DIR` says otherwise — which is the single source of truth**. `.env.example` documents every variable; copy it, fill it in, and keep it `chmod 600`. It is gitignored.

Each script loads it for you. `python -m scripts.<name>` calls `load_env()` (`lib/env.py`) at process start, which reads `.env` with `override=False` — so a variable already exported in your shell still wins, and CI, which has no `.env`, is unaffected:

```bash
python -m scripts.run_plan                  # .env is loaded automatically
```

> **`.env` is plain text.** Keep it `chmod 600`, out of any shared backup, and rotate anything that
> leaks. A WordPress application password is revoked and reissued in seconds from
> Users → Application Passwords.

Two things this deliberately does **not** do:

- **The test suite never loads `.env`.** `load_env()` is called from each script's `if __name__ == "__main__":` block, not from `main()` — and the nine test modules under `tests/scripts/` call `main()` directly. `.env` carries all four variables the staging guards gate on, so loading it anywhere in the test path would arm tests that write to the live WordPress site and the GS1 production resolver. Running those tests stays a deliberate act: `set -a; source .env; set +a && pytest -m staging`.
- **It is not imported by `lib/`.** A library must not have import side effects, so nothing is loaded merely by importing the package.

Both rules hold for the operator shell too, and one more besides: `ui/` never loads `.env` at all — an AST check in `tests/lib/test_env.py` fails the build if any module under it does. A long-lived desktop process holding production credentials, with the staging-guard variables armed inside it, buys nothing that the subprocesses do not already provide.

> **Once `clients.yml` loads, its operator-facing half is editable as a form** — site URL, user, post type, languages, environment, account numbers, credential variable *names*, and the two file paths — on the Setup screen of the operator shell, with live Test buttons and write-only credential fields over `.env`. It writes only what you changed, preserves the rest of the file byte for byte, and refuses a result that would not load. It does **not** replace the onboarding below: the form is not shown over a file that will not parse, and `gdsn_map`, `acf_map`, `brick_category_map` and `generator` stay read-only there by design. See [`ui-operator-shell.md`](ui-operator-shell.md).

One trap survives: **quote the WordPress application password.** WordPress issues it as six space-separated groups. `python-dotenv` parses the unquoted value correctly, but `source .env` — which you still need for the staging tests — stops at the first space and loads the variable empty, producing a baffling `401` with a password you know is correct. Keep the quotes.

Note also that **environment variables do not survive between separate Claude Code tool calls**, so any manual `source .env` must be joined to the command that needs it with `&&`, in one call.

**Most steps need no credentials at all.** Parse, plan, report, and the map builders are entirely local; only `run_execute` and `run_unpublish` talk to WordPress and GS1. You can get a long way before credentials matter.

### Check it before you need it

```bash
python -m scripts.doctor              # everything, including credentials and reachability
python -m scripts.doctor --offline    # only what needs no network and no secrets
```

Prints one line per check with what to do about each failure, and exits `1` if anything failed. Run it after editing `.env` or `clients.yml`, after a credential rotation, and before any wave.

It exists because the alternative is finding out late. A missing secret used to surface at the *first API call*, so parse, plan and a clean dry-run could all pass before it fired. Generated copy that no longer matched the export surfaced not at all — those units simply vanished from the plan (E21). The doctor checks: the config against its schema (**every** offending field, not just the first), how many products are actually in scope after the process list and the video allowlist, whether this run's generated copy covers those, the process list, category and video mapping, `ffmpeg` when it is used, and — unless `--offline` — that the site serves, that the WordPress credential authenticates *and* can still publish, and that the GS1 resolver accepts your credentials.

It writes nothing, and it deliberately never reads `state.json`: an idle peek at a corrupt one would quarantine it (E19), and a diagnostic must not change what the next run does.

### Client configuration

`clients.yml` is validated against `schema/clients.schema.json` and parsed into the Pydantic models in `lib/config.py` — that module is the authoritative field list, including defaults. The blocks:

| Block | Purpose | Reference |
|---|---|---|
| `export` | Where the export is and how to read it (`format: flat \| gdsn`, `column_map` / `gdsn_map`, `market_priority`) | [`data-source-export-schema.md`](data-source-export-schema.md) |
| `wordpress` | Site URL, credentials env var, `post_type`, `languages`, `acf_map`, `multilingual_plugin` | [`wordpress-onboarding.md`](wordpress-onboarding.md) |
| `gs1` | Account numbers, credential env vars, `environment: test \| production` | [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) |
| `template` | HTML template overrides (optional — ACF clients don't need it) | [`template-variables.md`](template-variables.md) |
| `qr` | Formats, `size_mm`, error correction, `dpi` | |
| `categories` | GPC brick → site category, plus per-GTIN overrides | |
| `generator` | Optional LLM content generation | [`costs.md`](costs.md) |
| `media` | Images and video, field names, write shape | [`wordpress-onboarding.md`](wordpress-onboarding.md) |
| `process_list` | Operator file listing exactly which GTINs a run may touch — every GTIN in it is processed | |

## 4. Running it

### Which flow do you need?

Three commands, one gated sequence. Pick by what you want written:

| Command | Writes | Reversibility |
|---|---|---|
| `/gs1-pages` | WordPress product pages only. No resolver record, no QR. | **Reversible** — edit or delete the page |
| `/gs1-links` | GS1 Digital Link records and QR only, pointing at pages that **already exist**. Touches no page. | **PERMANENT** |
| `/gs1-publish` | Both: pages first, then links pointing at them. The normal full publish. | **PERMANENT** |

Plain language works too — *"publish democlient to GS1"*, *"just set the Digital Links for democlient"* — and lands in the same place: `flow-orchestrator` classifies which mode you meant and confirms it at gate 0 before anything runs. The slash commands only skip the guessing.

> **`/gs1-links` is the one to be careful with.** Its targets are not pages the tool just created and verified — they come from `state.json`, a slug lookup, or `wordpress.target_url_pattern`. A GS1 record **can never be deleted**, so a QR printed against a wrong URL is permanent.
>
> `run_execute` therefore HEADs every target before it writes, and refuses any GTIN whose target does not serve. That refusal is in the script, not in the skill's instructions — so it happens even on a manual invocation. If you see `refusing to point a permanent GS1 record at it`, the page is not where the plan thinks it is: fix `target_url_pattern` or publish the page, then re-run. Do not route around it.

> **After `/gs1-pages`, the rows keep planning as CHANGED** until `/gs1-links` finishes them. That is deliberate: a page published without its resolver link is not done, and the plan says so rather than reporting the product complete.

**Say what you want in Claude Code:**

```
publish {client_id} to GS1
```

That loads the `flow-orchestrator` skill, which drives the whole pipeline and stops at each operator gate: **intent confirmation (gate 0)** → language selection → the generated-content review gate → the plan review gate → a per-row diff gate for changed rows → a **mandatory production environment-confirmation gate** → a **mandatory dry run** → execute → progress → post-run summary → retry. Nothing proceeds without your answer, and the skill passes `--i-understand-production` only *after* you confirm at a gate.

Gate 0 states the mode, cross-checks the export file against `clients.yml`, gives the product count and environment, and — for anything that writes to GS1 — warns that the records are permanent. In `pages` mode it also stands in for the production environment gate, since nothing irreversible follows.

**Use this for every real run.** Those gates are the reason nothing has been published by accident, and they exist only on this path — invoking the scripts directly bypasses all of them. Other useful phrasings: *"parse the export for {client_id}"*, *"generate content for {client_id}"*, *"create product pages for {client_id}"*, *"render QR for {client_id}"*, *"update the Digital Link for {client_id}"* — one per skill in `.claude/skills/`.

## Onboard a client

Read-only until the final step. This is the one workflow where working hands-on with the scripts is expected: mapping an unfamiliar export is iterative, and you want to see each result before deciding the next change.

1. **Add the client** to `clients.yml`. Copy the example block and change `client_id`, `display_name`, `export.path`, and the `wordpress` / `gs1` blocks — then remove the leftover `democlient:` example so your live config describes only your own clients. Add the credential env vars to `.env` (see [Secrets](#secrets)); `clients.yml` gets the **names** only.

2. **Drop the export** at `input/{client_id}/products.xlsx`. `input/` and `output/` are gitignored, so client data never enters the repository. Create the directory if it does not exist.

3. **Inspect it** and let the tool draft your mapping:
   ```bash
   python -m scripts.inspect_export input/{client_id}/products.xlsx
   ```
   For a GDSN datapool export this prints each sheet's attributes with their GDSN attribute ids and sample values, plus a suggested `export` block. Refine it into `gdsn_map` (or `column_map` for a flat export) — details in [`data-source-export-schema.md`](data-source-export-schema.md).

4. **Iterate until the parse is clean:**
   ```bash
   python -m scripts.parse_export --dry-run
   ```
   Repeat until there are no warnings on required fields. `brand` and `product_name` are mandatory. Then drop `--dry-run` to write `products.json`.

5. **Review data quality** — `python -m scripts.report_quality`. Fix what belongs in MyGS1 at the source. Blank or wrong source data must not be invented downstream.

   Run `python -m scripts.doctor` here too, before you go near credentials. It will tell you how many products are actually in scope and what is removing the rest — the number an operator most needs and is least often given.

6. **Map the page fields.** Set `wordpress.acf_map` (or a template) so every page slot has a source. See [`template-variables.md`](template-variables.md).

7. **Plan:**
   ```bash
   python -m scripts.run_plan
   ```
   Confirm the counts are what you expect before going further.

8. **Dry-run the write:**
   ```bash
   python -m scripts.run_execute --plan output/{client_id}/plan.json --dry-run
   ```

9. **Publish a small first wave — from chat, not from the command line.** Say *"publish {client_id} to GS1"* and take the gates one at a time. Two or three GTINs, not the whole batch, and keep `gs1.environment: test` until a page renders correctly. Then verify each one properly:
   - Fetch the page HTML and confirm the content is actually **rendered**. A `200` proves nothing — the ACF write path fails silently.
   - Check resolution with **GET**: `curl -sS -o /dev/null -w '%{http_code}' -L https://id.gs1.org/01/{gtin14}` → 307 → your page → 200. The resolver **404s to HEAD**.

10. **Scale up** once a wave is verified, and record what went live.

## Safety rules

- **Publish through Claude Code's gated flow**, not by invoking `run_execute` yourself. The gates are the safety mechanism; the scripts have only the production guard.
- Dry-run before every real run.
- A live production run requires `--i-understand-production`. That prompt is the guard working, not an obstacle to route around.
- Never `pytest -m staging` casually — it writes to live WordPress and GS1 production.
- A GS1 Digital Link record **cannot be deleted**. `run_unpublish` deactivates it; the disabled record stays on the account permanently. Never point a smoke test at a real product's GTIN.
- `clients.yml`, `.env`, `input/`, `output/` are gitignored. Keep it that way, and keep secrets out of `clients.yml` — it holds env var *names*. 
- Keep credentials in `.env` and nowhere else. A Claude Code `settings.json` `env` block also works, but it is machine-wide — those secrets end up in the environment of *every* command in *every* project, which is how a password once got echoed into a chat transcript. Rotate anything that leaks; a WordPress application password is revoked and reissued in seconds from Users → Application Passwords.
- Verify the webpages manually, never just rely on a status code.
- Checking the pipeline still works against production is its own procedure — see [`verifying-live.md`](verifying-live.md). Do **not** unpublish a product to create something to test with: it classifies HELD and the run does nothing, having taken the product down for no result.

## Next

- [`operator-install.md`](operator-install.md) — the two-double-click install for the operator's machine.
- [`operator-guide.md`](operator-guide.md) — the operator's walkthrough of a batch, with screenshots.
- [`ui-operator-shell.md`](ui-operator-shell.md) — why each of that shell's screens is built as it is, and where its safety lives.
- [`troubleshooting.md`](troubleshooting.md) — every error type, and the traps already paid for.
- [`verifying-live.md`](verifying-live.md) — how to prove the flows still write to production, without degrading a live product.
- [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) · [`wordpress-onboarding.md`](wordpress-onboarding.md) — the two external systems.
- [`data-source-export-schema.md`](data-source-export-schema.md) · [`template-variables.md`](template-variables.md) — data in, page out.
- [`costs.md`](costs.md) — what running this costs.
- [`architecture.svg`](architecture.svg) — end-to-end diagram.
- [`IMPLEMENTATION_SPEC.md`](IMPLEMENTATION_SPEC.md) §8 — full script contracts.
