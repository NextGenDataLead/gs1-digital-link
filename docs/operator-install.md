# Installing on the operator's machine

Two double-clicks, on a machine with no Python, no Homebrew and no developer tools.

| | |
|---|---|
| **macOS** | `install.command`, then `start.command` |
| **Windows** | `install.bat`, then `start.bat` |

That is the whole install. This page exists for the three things around it: what the maintainer
has to hand over besides the folder, the two ways a corporate machine refuses to run the file, and
what IT is being asked to allow.

> Developing on this repository instead? Use [`setup.md`](setup.md). **Do not run
> `install.command` in a development clone** — it replaces `.venv` with a Python 3.11 environment
> holding the `ui` extra and *not* `dev`, so `pytest` and `mypy` disappear from it.

---

## 1. Get the folder

Download the latest release from
**[github.com/NextGenDataLead/gs1-digital-link/releases](https://github.com/NextGenDataLead/gs1-digital-link/releases)**:
under *Assets*, **Source code (zip)**. Unzip it anywhere the operator can write — Desktop,
Documents, wherever. (A `git clone` of a release tag works too.)

This folder is the **program** only. The operator's settings, credentials and the record of what
was published live in a separate **data folder**, which the installer sets up — so a newer release
can replace this folder without touching any of it. See [Updating](#updating-to-a-new-release).

**The folder must stay together.** `install.command` on its own, moved to the Desktop, installs
nothing — it says so rather than guessing.

## 2. Run the installer

Double-click `install.command` (macOS) or `install.bat` (Windows). It takes a few minutes on a
first run, and prints what it is doing.

It:

1. installs **`uv`** into `~/.local/bin` — a single static binary, unless one is already on the
   machine, in which case that one is used;
2. has `uv` fetch its own **CPython 3.11**, into `~/.local/share/uv/python` (macOS/Linux) or
   `%LOCALAPPDATA%\uv\data\python` (Windows);
3. builds **`.venv`** inside the folder from the committed `uv.lock`;
4. finds this machine's **data folder**, and prints where it is:
   - on a machine that has been set up before, the same folder as last time;
   - on a machine whose program folder already holds settings and a publishing record — set up
     before data folders existed — that folder, exactly where it is, with nothing in it changed;
   - otherwise a new folder, **`GS1 Digital Link data`** in the user's home folder, with a blank
     settings file and a blank, owner-only credentials file in it.

   It notes the choice in the user's application-settings folder, so the next release finds it.
   If the noted folder has gone, or two folders both hold data, **it stops and says so** rather
   than guess: either guess could hide the record of what was already published.

   If this release carries a client's own **voice prompt or page templates** and the data folder
   has none of them yet, it copies them into `input/{client}/prompts` and
   `input/{client}/templates` there. It is the one thing it ever adds to a folder that already
   holds data, and it never overwrites: a folder that has them keeps its own.

No administrator rights, nothing installed system-wide, and nothing written outside the folder and
the user's own home directory. The versions come from `uv.lock` rather than from a fresh
resolution, so this machine gets what was tested — the installer passes `--locked`, which means it
would rather stop than quietly install a different set.

Re-run it any time. It is idempotent.

## 3. The five files that are not in the download

None of these is in the download. They belong in the **data folder** the installer named — on a
machine set up before data folders existed, that is the program folder itself.

**Normally all five arrive together, in one handover pack** — made on **Setup → Make a handover
pack** on the machine that has them, received on **Setup → Receive a handover pack** on this one
(see [below](#what-arrives-through-the-app-and-what-does-not)). Two exceptions:

- **`.env` travels only when "Include the passwords" is ticked** when the pack is made. Unticked,
  enter the credentials on **Setup → Credentials** instead.
- **The video files never travel in a pack** — only their index, `mapping.yml`. They come on a
  disk; see the paragraph after the table.

The pack also carries the client's own **voice prompt and page templates**, from
`input/{client}/prompts` and `input/{client}/templates`, so a release that does not ship them still
writes copy and pages the client's way. Preflight's *Prompt and templates* line says where this
machine reads them from.

Never copy `state.json` across by hand: the pack is what checks it is not older than the one here.

The table is what each file is, and what happens without it. **A missing one does not always
announce itself** — the first two stop the shell dead, the last three fail in three different and
much quieter ways.

| File | Goes | What it is | Missing it |
|---|---|---|---|
| `clients.yml` | top level of the data folder | the site settings | Setup says the config did not load; nothing runs |
| `.env` | top level of the data folder, `chmod 600` | the credentials | every live check fails; nothing runs |
| `input/{client}/process/selection/selections.xlsx` | the path in `process_list.path` | the barcodes this run may touch | Data shows a red band. It has an upload — see below |
| `output/{client}/state.json` | exactly there — the path is not configurable | **the ledger of what is already published** | **see the warning below. This is the expensive one.** |
| `input/{client}/videos/mapping.yml` | the path in `media.video_map_path` | which video belongs to which product — edited by hand afterwards, or filled in from the client's sign-off sheet on Data | the preflight **fails** (`cannot read …`); the machine cannot reach a runnable state |

> **`state.json` is the one to get right.** It records every `(GTIN, language)` this tool has
> already published. Without it, a run classifies **every already-published GTIN as NEW** — a
> second WordPress page for each, and another **permanent** GS1 Digital Link record for each. A GS1
> record can never be deleted, only disabled. Nothing downstream notices: the plan looks like a
> normal first run, and the counts are the only clue.

`.env` should be readable by its owner only. The tool sets that itself whenever it writes the file
— on Setup, on a first install, and when it receives a handover pack — on macOS and on Windows.

**If this client publishes video, the video files come too** — the folders named under
`media.video_folders`, usually several gigabytes, so they travel on a disk rather than by mail. The
mapping above is only the index to them. And with `media.restrict_to_mapped_gtins` on, a product
with no confirmed video in **every** language is silently out of scope: it never reaches the plan,
and the run reports success having skipped it.

### What arrives through the app, and what does not

Three files change hands every batch, and all three have an upload control, on the **Data**
screen, so nothing needs copying into a folder by hand:

- **The product selection list** (step 1). It is read before it is installed, so a file that will
  not open is refused while the list you were using stays put. Your upload is kept as
  `process/uploads/product-list.xlsx`, and every selection you save with **Next** is dated beside
  the live one. Uploading the list again is how you start the selection over.
- **The GS1 Data Source export** (step 2). It becomes `process/uploads/GS1 export/export.xlsx`,
  with a dated copy of every upload beside it.
- **The client's video sign-off sheet** (step 3), when one comes back.

The generated copy is not uploaded. A machine with an Anthropic key writes it on **Content**; one
without has none to write, and a page already live keeps the text it has.

**Moving a whole setup to another machine** — a first install, or handing the publishing over to
someone else — is two buttons rather than five files: **Setup → Make a handover pack** on the
machine that has it, and **Setup → Receive a handover pack** on the other. It carries the ledger,
and refuses to replace one that knows about a publish the pack does not, or knows a newer version
of it.

### Returning the ledger

`state.json` travels **both ways**. After a publish from the operator's machine, that copy knows
things the maintainer's does not — new page ids, new URLs, new GS1 link hashes — and it is the only
record of them.

**The machine that published owns the file, and only the machine holding it publishes.** It goes
back with the same handover pack — made on Setup there, received on Setup here — never by copying
`state.json` across, and never by merging by hand. Two divergent ledgers is how the same
product gets published twice, which costs a duplicate page and a second permanent GS1 record. The
receiving refuses exactly that case. If you are unsure which copy is newer, check the live site before
running anything — see [`verifying-live.md`](verifying-live.md).

The human-readable live log (`docs/clients/{client}-live-log.md`) is a convenience, not the source
of truth. Where the two disagree, `state.json` wins: it is what the pipeline actually wrote.

A new data folder already has a blank `clients.yml` and `.env`; the maintainer's copies replace
them, or they can be filled in on the **Setup** screen.

## 4. Start it

Double-click `start.command` / `start.bat`. It prints the data folder it is using, then a desktop
window opens on `127.0.0.1:8477` — no browser tab, no URL anyone else can reach.

**[`operator-guide.md`](operator-guide.md) takes it from here** — the walkthrough of a batch,
screen by screen, with screenshots. Read that next.

`./start.command --browser` serves the same pages in a browser instead, for a machine where the
webview will not open.

## Updating to a new release

1. Download and unzip the new release, as in step 1 — into a **new** folder.
2. Double-click its installer. It finds the same data folder and says so; nothing in it changes.
3. Start the new version with its own `start.command` / `start.bat`.

The old program folder can then be deleted — **unless the installer said the data is in it.**
On a machine set up before data folders existed, the data stays in that first folder until it is
deliberately moved, and the installer says *KEEP IT* in capitals. Believe it: deleting that
folder deletes the record of what was published.

---

## When the machine refuses to open the file

Both are the operating system doing its job on an unsigned file downloaded from elsewhere, and
both are one-time.

**macOS — "cannot be opened because it is from an unidentified developer".** Right-click (or
Control-click) the file → **Open** → **Open** in the dialog. That records an exception for that
file; double-clicking works from then on. From a terminal the equivalent is
`xattr -dr com.apple.quarantine /path/to/the/folder`.

**Windows — "Windows protected your PC".** **More info** → **Run anyway**.

**If the machine is managed and the dialog offers no way through,** it is MDM policy rather than
Gatekeeper or SmartScreen, and no incantation gets around it. The options are to have IT sign and
notarise the two scripts, to have IT package the install, or to run the four commands by hand once
(they are the contents of `install.command` — a `curl`, a `uv python install`, and a `uv sync`).

## When the downloads are blocked

The installer fetches from `astral.sh` (the `uv` binary), `github.com` (the CPython build) and
`pypi.org` (the packages). On a network that blocks the middle one, point `uv` at an internal
mirror before running it:

```bash
export UV_PYTHON_INSTALL_MIRROR=https://internal.mirror.example/python-build-standalone
./install.command
```

For PyPI the equivalent is `UV_DEFAULT_INDEX`. If all three are blocked, the install has to be
prepared on a machine that can reach them and copied over whole — `.venv` included.

---

## Where IT runs containers instead

Some company machines refuse the installer outright but already run containers — Docker
Desktop, Podman Desktop or Rancher Desktop, put there by IT. On those, the same application comes
as a ready-made image instead, and nothing above applies.

1. Make a folder for this tool's data, for example `gs1-data` in Documents. **This folder is the
   installation.** It will hold the passwords, the client settings, the uploaded files and the
   record of everything published to GS1. Back it up; it is the only copy.
2. Put `compose.yml` (from the repository) next to it, and start it from that folder:

   ```bash
   docker compose up -d
   ```

   With Podman, `podman compose up -d`. If the data folder is somewhere other than `./gs1-data`,
   say where first: `GS1_DATA=~/Documents/gs1-data docker compose up -d`.
3. Open **http://127.0.0.1:8477** in the browser. On the very first start the data folder is
   filled with a blank settings file and a blank passwords file; fill them in on the **Setup**
   screen, exactly as on an installed machine.

Reports download through the browser, like any web page. To update, `docker compose pull` and
then `docker compose up -d` — the data folder is untouched, because the image never contains
any of it.

**On Linux** the folder must be writable by the container's user (number 1000). If it belongs to
someone else, the container stops at once and says so; `sudo chown 1000:1000 gs1-data` fixes it.

**Only this machine can reach it.** The shell answers on `127.0.0.1` only, exactly like the
installed version.

---

## For IT

The security posture of the running application is in
[`ui-operator-shell.md`](ui-operator-shell.md#for-it) — loopback-only socket, no telemetry, no
auto-update, and no Anthropic egress or LLM credential unless the variable named by the client's
`generator.api_key_env` is deliberately given a value (a fresh install does not). What the
*install* adds to that:

- **Everything downloaded is version-pinned and reviewable.** `uv` is pinned to an exact version
  in both installers; the Python build is 3.11, the same one CI runs the test suite on; every
  package comes from `uv.lock`, which is committed — 86 packages with hashes, in the repository,
  vettable before anything is installed.
- **User-scope only.** No administrator rights, no service, no scheduled task, no PATH change
  beyond `uv`'s own line in the user's shell profile. Besides the program folder, a first install
  creates one data folder in the user's home folder and a one-line text file naming it, in the
  user's application-settings folder (`~/Library/Application Support/GS1 Digital Link` on macOS,
  `%APPDATA%\GS1 Digital Link` on Windows). Setting `GS1_DATA_DIR` before starting overrides it.
- **The credentials predate this.** `.env` holds a WordPress application password with editor
  rights and GS1 production OAuth credentials, plaintext at mode 600. That is how the tool has
  always worked from a terminal; the installer neither improves nor worsens it, and a secret
  manager is the answer if that is the blocker.
- **One unconstrained outbound behaviour**, worth knowing about: when publishing, the tool fetches
  product images from whatever URLs the GS1 feed carries, with no allowlist.

---

## For maintainers

**After changing `pyproject.toml`, run `uv lock` and commit the result.** Otherwise the operator's
`uv sync --locked` refuses to install — deliberately, because the alternative is that machine
silently getting different versions from everyone else. `tests/test_packaging.py` catches it
offline and CI's `uv lock --check` catches it properly.

The uv version and the Python version are written out in `install.command`, `install.bat`,
`start.command`, `start.bat`, `.github/workflows/ci.yml` and the `Dockerfile`. To bump either, change every copy —
the same test fails if one moves alone. There is no `.python-version` file on purpose: pyenv reads
that file too, and it would break `python` in this directory for anyone who has pyenv without the
pinned version installed.

**The container image is published by tagging a release** (`v*`):
`.github/workflows/container.yml` builds it for amd64 and arm64 and pushes it to
`ghcr.io/nextgendatalead/gs1-digital-link` as the version and as `latest`. On every pull request
that touches the code or the image it is built and started for real, without publishing.

**A release is a tag plus a GitHub release.** `gh release create vX.Y.Z --generate-notes` makes
the *Source code (zip)* operators download, and the tag publishes the container image. Before
tagging, the `Installers` workflow must be green: it runs `install.command` / `install.bat` and
`start --help` for real on macOS and Windows, which is the only place those scripts execute
outside an operator's machine.
