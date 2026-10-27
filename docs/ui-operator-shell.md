# The operator shell

> **Looking for how to publish a batch?** That is
> [`operator-guide.md`](operator-guide.md) — a walkthrough of the four screens with screenshots,
> written for the person running the tool. **This page is not that.** It explains *why each screen
> is built the way it is*, usually by naming the defect that produced the current design, and it
> names Python modules and test files throughout. It is for whoever maintains the shell.

A local desktop window over the same commands you would otherwise type. It exists so that the
recurring loop — drop a new export and a new scope list, choose the batch, import the copy, run the
flow, read
the result — does not require a terminal, a virtualenv, or knowing which of eleven scripts to call.

On the operator's machine there is nothing to install first and nothing to type: double-click
**`install.command`** (macOS) or **`install.bat`** (Windows), then **`start.command`** /
**`start.bat`**. See [`operator-install.md`](operator-install.md), which also covers the two ways a
managed machine refuses to open an unsigned file.

From a development clone it is the same program, started the usual way:

```bash
pip install -e ".[ui]"
python -m ui
```

It binds to `127.0.0.1` only and opens in a native window rather than a browser tab. `python -m ui
--browser` serves the same pages in a browser on the same loopback address, for a machine with no
webview available. `--container` is the container image's mode: it listens on the container's
interfaces, because the container's loopback is unreachable from the machine, and leaves
loopback-only to `compose.yml`'s `127.0.0.1:8477` mapping. It refuses to start outside a container
(`ui/serving.py`), where nothing would map the port and the shell would face the network. In both
browser modes reports download through the browser (`ui/handover.py`): the native window's copy
to Downloads would land on the serving side.

---

## What it is not

**It has no LLM credential and no connection to Anthropic — unless you give it one.** Leave the
client's `generator.api_key_env` unset and nothing here reaches Anthropic: content generation
happens on the maintainer's machine, in a Claude Code session with the `content-generator` skill,
and `generation_results.json` is handed over as a file and uploaded on the Content screen.

That split removes an entire class of IT objection and a per-token cost from the operator's
workstation, at the price of one file changing hands per batch. It is the default and it stays
supported. But it is also the reason the shell was not, on its own, a route from dataset to pages:
a brand-new product needs copy, and copy could not be written here.

So the key is now an **optional** field on the Setup screen. Set it and the Content screen writes
this run's copy itself; leave it blank and the screen says so and offers the import instead. The
credential is read by the `run_generate` subprocess from `.env`, never by this application — the
same arrangement that keeps every other secret out of the desktop process.

**It does not reimplement the pipeline.** Every action is a subprocess running exactly the command
a person would run, from the repository root, and every screen shows that command. So the terminal,
the skills, and [`verifying-live.md`](verifying-live.md) all stay valid as a fallback when something
here is wrong.

**It does not replace the Claude Code flow.** `publish {client} to GS1` still works and still goes
through `flow-orchestrator`. The shell is a second surface over the same gates, not a fork of them.

---

## The screens

Six screens, in two groups. **The rail is the argument**: the numbered four are the loop an
operator repeats per batch, and everything else is deliberately not numbered.

| # | Screen | What it is for |
|---|---|---|
| 1 | **Data** | Upload the selection list, the export and the client's video sign-off sheet; edit the video mapping; choose the batch; read the client's issue report. |
| 2 | **Content** | Generate or import `generation_results.json`, check its coverage, read the copy. |
| 3 | **Preflight** | `python -m scripts.doctor`, rendered as a list to work down. Offline by default. |
| 4 | **Publish** | The nine gates, one at a time. |
| — | **Setup** | The operator-facing half of `clients.yml` and `.env`, as a form, with live Test buttons. |
| — | **Runs** | Every row of every run, as it was recorded at the time, and whether the site agrees. |

Load this batch's inputs · review its copy · check *this batch* · publish it.

**All six used to be numbered 1-6, and that was wrong in the same way twice.** Setup is configured
once and then left alone; Runs is read afterwards. Numbering them alongside the four said they were
one sequence, so the work an operator actually repeats was buried between machine configuration at
one end and history at the other. They keep a permanent place in the rail — below a rule, under
*This machine* — because a tool nobody can find is a tool nobody uses; only below 55rem, where the
rail used to stack as a full-width block with no way past it, does anything fold behind a `☰`.

**There were seven, and the seventh was filed in the wrong place.** *Video mapping* sat under *This
machine* beside Setup and Runs, as "one input file's editor". It is not that: the mapping decides
whether a product can be published at all, so it was the input deciding a batch's size, filed with
the things touched once. It is part of Data now — see *The videos, on the Data screen* below.

**And Preflight used to sit at 2.** Four of the doctor's checks have the remedy "Run `parse_export`
first" — which is the Data screen — so it told an operator to go and do a later step and come back,
and on a machine being set up from scratch most of the list could not answer its own questions yet.
Its headline is "N of M in scope", a statement *about the export just loaded*, so it belongs after
the loading. Nothing is lost by moving it: the credential checks it also carries are on the Setup
screen's Test buttons, at the moment the field is edited.

The order lives in `ui/theme.py`'s `WAVE` and nowhere else — each screen reads its own eyebrow from
it via `theme.eyebrow`, and `tests/ui/test_pages_contract.py` holds both the numbering and the
membership of the two groups, in both directions against the registered routes.

Each rail entry also carries one **fact** from `ui.context.rail_facts` — the export's age, the
plan's row count — so "have I done Data yet?" is answerable from any screen. Facts and not ticks:
a tick on Data because *an* export exists cannot say it is the *right* export, and a tick that lies
is worse than no tick. Everything in there must stay stat-cheap, because it runs on every render of
every screen; `tests/ui/test_shell_chrome_contract.py` fails if it grows a subprocess.

### Every button says that it is working

A button that runs something disables itself for the duration and shows a spinner and the seconds
elapsed. That is not decoration. `run_execute` prints one line when it starts and one when it
finishes, so a twenty-row publish leaves the console silent for about ninety seconds — and with
nothing disabled and nothing moving, the operator reasonably concluded it had not worked and
clicked again. Live pages were rewritten twice. No damage that time, because pages are
matched by slug and `meta.gtin` and updated in place; the same second click in `links` or `both`
mode is aimed at records that can never be deleted.

The guard is in `ui/theme.py`, on `action` and `quiet_action`, rather than at the two dozen call
sites — a guard on the execute button alone would have left the defect on the other twenty-three,
and a screen written next month inherits this one without an edit. It is not a progress bar:
`run_execute` reports every ten rows by design, so a bar would be fake precision on a twenty-row
run. It disables the button that was clicked and not the screen, because that is what actually
happened.

**The other half is that the command runs off the event loop.** A blocking `subprocess.run` inside
a click handler holds the loop until the command has already finished, so every UI change queued
before it — including the one saying the command is running — reaches the browser with nothing
left to report. `runner.run_json_off_the_loop` was written for exactly this and had been adopted
on one screen; six buttons across five others still blocked, so on those the spinner would never
have animated. Both halves are checked by `tests/ui/test_run_feedback_contract.py`, which derives
the handler list from the code rather than keeping one: a list of known offenders goes stale in
both directions, and the button added next month is the case it would miss.

### Data

**The screen is a procedure, so it is numbered.** Five filled numerals: **1** the product selection
list, **2** the export and **3** the video sign-off sheet, **three across** (`steps-3up`) because
they are three documents from three places and none waits on another — the sheet's review renders
full width under the row, where its column pickers fit; *Clear all*; **4** what the batch publishes;
**Coverage**; and **5** choosing and saving. A jump row under the title reaches each. Below 70rem the three stack.

**Step 5 is the selection first, and everything under it is about the selection** (operator,
2026-10-09: "this way we are able to get the issues with the selected dataset and not the whole
dataset"). The table at the top is **every row of the list**, each with a tick box, a short
**Status** (`ui.batch_grid.row_status`: can run · can run · no video · not in the export · not
eligible · link problem · checking link) and a **Detail**. Under it, one folded table per problem,
holding only the **ticked** rows that have it — not in the GS1 export, then not eligible and missing
video(s) for a batch that writes pages, or *link doesn't work* for a links batch. **Coverage counts
the same ticked products** (`ui.batch_grid.funnel`): selected → can run, and why the rest cannot,
with the list's size as context only.

**Next saves the ticked rows that can run** (`runnable_keys`), never a ticked row the run would
drop: the saved file is the batch, and such a row would read as chosen on every later screen. The
caption names how many ticked products are left out before Next is pressed. Ticks are seeded from
the saved batch by barcode (`unticked_by`), so a list just uploaded arrives fully ticked — its
problems show at once — and a reload shows the batch that was saved.

**Eligibility depends on what the batch publishes.** For pages and both it is
`lib.eligibility.eligibility` — `lib.holds.held_products` (the plan's E23/E24/E22, in the plan's
order) joined to the video status — so *can run* means exactly what the plan would publish;
`tests/lib/test_eligibility.py` pins that. **A links-only batch is judged on its link alone**
(`links_eligibility`): no video, image or mandatory page field holds it, because it writes no page,
and `run_plan --links-only` and `run_execute --only links` drop those holds too. Its target is the
*Link naar site* address or else this tool's own default-language page (`link_targets`), and it
must be on the site's host and load: `lib.link_targets` checks each address once per process, off
the event loop, following redirects and requiring a 2xx at the end — stricter than the run's own
HEAD, which still refuses a non-serving target before every GS1 write. Until the answer arrives a
row reads *checking link* and cannot run.

Unticks live in a per-client `batch_grid.Ticks` for the life of the process, so a mapping write or
a mode change rebuilds step 5 without costing a choice. A new list (or Clear all) forgets them,
since every row renumbers.

**Clear all — start fresh replaced *Start again from my uploaded file*.** Everything on the screen is
read from disk, so the operator's question was never "undo my ticks" (re-uploading does that) but
"how do I begin again". `lib.batch_reset.clear_batch` moves the live selection, the uploaded list,
the export and `products.json` into `input/{client}/superseded/cleared-{stamp}/` with a note —
**moved, never deleted**, and every dated archive stays where it is. The video mapping is never part
of it: it is the client's sign-off and outlives batches.

**The selection list is step 1 because it is the spine.** Everything else on the screen is measured
against it — the export is joined to it, the video status and the report are scoped to it. The cost
is that the list is a *join* against the export, so with the list alone the grid is the whole list
and its "not in the export" table is empty; step 1's ⓘ and the warning band both say so and name
step 2. The previous order never showed that state, and it is the honest one. The explanation of what a file *is* sits behind an **ⓘ** on the heading rather
than in a paragraph under it. Below 60rem the two stack, where a column would be narrower than a
picker and its label.

**The foot of the screen is the client's issue report, not the data-quality report** (operator,
2026-10-09: the long report "has gotten a bit out of hand"). It is the one-to-two pager the client
receives — failed products, issues by category, each failed product's reasons — built in-process
from the same verdict step 5 shows (`lib.issue_report.selection_issues`), so it follows the ticks
and needs no subprocess. Every run writes the same report (`runs/{stamp}/issues.pdf` and `.xlsx`)
about the **same ticked products**: Next saves only the runnable ones, so it also writes
`selections.ticked.json` beside the selection — the ticked products and their issues, with the
selection's sha256 — which the run copies in only while that hash still matches. The same snapshot
re-ticks the products on reopening, so the screen and the run report never cover different sets. The long data-quality report is archived from this screen,
not deleted: `report_quality` still writes it, every run still writes its `data-quality.md`, and
the Runs screen still builds the complete one.

**A batch requires both files, every visit.** Until an export and a list have each arrived *and
been accepted* in this render, there is no selection, no quality report and no way onward — the
Next button is disabled. Not shown empty, not shown stale, not shown at all. A screen that offered
a batch built from whatever was left on disk is one that lets a run inherit the previous batch's
scope without anybody deciding to.

**Arrival is remembered per client for the life of the process** — `ui.pages.data._BATCHES`, a
module-level dict. The screen rebuilds on every visit, so a local would reset the moment the
operator stepped to Content and back, and demanding both uploads again for a trip to the next
screen is not what "every run brings both files" means: a run is a batch, not a page view.
Restarting the shell starts a fresh batch, which is the operator's own loop — open it, do a wave,
close it. It needs no storage secret, no cookie and no connected client, none of which this shell
has; `app.storage.tab` would need all three. What it gives up is the two-window case, where both
windows share one batch — not a real configuration for a loopback native window driven by one
person, and the wrong answer there is mild.

**Saving records itself**, so that re-uploading the list afterwards can say what it just did:
"Installed — this replaced the selection you saved earlier." Uploading writes the file straight to
the control path, which is what makes it the undo; the same act silently discards a save, and
saying so is the difference between an undo and a loss.

And it is why `docs/images/data.png` now
shows the landing state — the throwaway `democlient` has no parseable GDSN export, only a
stand-in, so its screenshot cannot reach the populated screen. A faithful synthetic export would
fix that.

**A `Next` button at the foot, and it is the save.** The rail is navigation for somebody who knows the shape of the
tool; `theme.onward` is for somebody following the procedure for the first time, who has finished a
screen and wants to be told where the next thing is.

It carries the save because on a screen with unsaved work "go on" and "commit what I chose" are one
intention, and two buttons is how the second gets missed.

**What the save will do is said before the click, in the button's caption** — "Next saves 35 of 37
row(s) — 2 dropped — and goes on to the copy", updating as rows are ticked. That is the surviving
mitigation for the inverted tick box, and it is in the only place that works. It was a toast
*after* the save for one round, which was wrong twice: a notification does not survive a page
change, so it was racing the navigation; and the fact it carried is one somebody has to **act** on,
which means it has to arrive while they can still change their mind.

The toast is now one word — "Saved" — and the navigation waits `_TOAST_BEAT` = 4 s for it, which is
long enough to read one word and was not long enough to read the eleven-word version it replaced.
The button disables for that wait, because four seconds of an enabled button that does nothing
visible is four seconds in which it gets pressed again.

The count line, the held-video line and the Save and Restore buttons that used to sit between the
table and the report are all gone. **Restore is gone entirely**, and that is a deliberate trade: a
batch already requires both uploads, so uploading the list again *is* the undo, and a button whose
job is to avoid a step the screen insists on anyway is a second way to do one thing.

It leaves the inverted tick box with two of its four mitigations rather than four — the old
wording is deleted and the save still reports the delta; the red Save button and the one-click way
back both went with the restructure. The remaining protection is that the file the operator
uploaded is still on their machine, minutes old. The
video consequence moved into step 3's ⓘ; the per-row mark still carries the fact. `theme.section(step=…, explain=…)` and `theme.subhead` own that shape, so a screen
cannot invent a second one.

`theme.explanation` is both a tooltip and a toggle: hover answers it for an operator already
reaching past it, and the press is what makes it reachable at all on a touch screen and by
keyboard, where there is no hover. **What may go behind it is the constrained part** — text that
is true every time and needed once. Never a warning, never a count, never anything true only
today; those stay on the page in a band, because an ⓘ is discovered at the operator's leisure and
a stale export is not. `tests/ui/test_shell_chrome_contract.py` pins that rule in the docstring.

**The two figures are gone, and nothing replaced them.** `products parsed` and `export modified` sat side by side at
`--text-hero`, the size reserved for a number somebody acts on. Neither was: they are the *state
of a step*, and side by side they read as one fact about one file while being two facts about two
files with two modification times, either of which can be the stale one — which is the defect the
staleness band exists to catch. There is no standing tally of what was loaded last time: the upload says what it read, on
the line beneath it, and that line is about the upload that just happened.

**Three documents, three sections, three uploads.** The export is product *data*; the selection
list is *which products*; the sign-off sheet is *which video is which product's*. They come from
different places and confusing them is the most expensive mistake this screen affords, so each has
its own name, its own section and its own upload — and the sign-off upload refuses a workbook headed
`Gtin` + `TargetMarketCountryCode` by name, because `read_sheet`'s second pass would otherwise turn
an export into a grid and reject every row of it about the wrong thing. The config key stays
`process_list` — it is in `clients.yml`, `schema/clients.schema.json`, `ProcessListConfig`, the
doctor payload and five call sites, and renaming it would break every install. Only the words the
operator reads changed.

Both uploads **replace the configured path in place**. Writing anywhere else would produce a file
the tool cannot see, and neither `parse_export` nor the scope-list reader takes an input-path
override.

**Both uploads validate, and there is no separate button to make them.** There were two — *check
the parse* and *parse and save* — and the second was the one that mattered, so the first was a step
an operator could skip into a run built on a workbook nobody had opened. Uploading is now both.

They reach the same guarantee by different routes, because `parse_export` has no input-path
override. The scope list is read **before** it is installed, from a temporary directory, through
`lib.process_list.read_process_list` — `ui.video_map_edit.write_validated`'s pattern. The export
cannot be, so it is backed up, written, parsed, and **rolled back from the backup if the parse
fails**. Either way the file the operator was working from survives a bad upload, which is the
property that matters: a failed read that left the bad file in place would mean every screen after
it describes a workbook nobody can use, with no way back but a re-upload of a file they may no
longer have.

`theme.upload` carries the spinner. The point of it is not the transfer — the file moves a few
centimetres to the same machine — it is the seconds the handler spends reading a 500 kB workbook,
which would otherwise look like nothing had happened. That is how an operator comes to press a
thing twice.

**`process/uploads/product-list.xlsx` is the upload, kept byte for byte**, and every upload is also
kept dated beside it. `.bak.xlsx` held only *the previous save*, so after two saves the operator's
original was gone; archiving on the way **in** is what lets the undated name hold "the list I sent"
rather than "whatever it looked like last time". Each run copies it into its own folder as
`selection-uploaded.xlsx`, which is what the result sheet reads to name the rows the operator
dropped — reading it out of `input/` afterwards described whatever the next batch had put there.
**It never decides what gets written** — a design that derived the control file from it would put a
wrong join between the operator and their own list, silently.

**Which export a selection was ticked against is recorded, not inferred.** `process/history.jsonl`
takes one line per upload and per save; a run writes `runs/{stamp}/inputs.json`. Both name the
archived filename and carry its sha256 — the **name is the reference and the hash is the check**,
the convention `lib/generator.py` states as "a validity check only, never a reuse key". Two facts
live nowhere else: what the operator called each file when they sent it (uploading renames it), and
which export each set of ticks was chosen against. The second is what makes the stale-batch warning
possible at all, and a selection with no record reads as *not recorded* — a third answer, never as
agreement and never as staleness. Every batch saved before this existed is in that state.

**The batch in force is read from disk, on every screen.** `lib/batch.in_force` describes it, and
Data reads it to decide whether there is a batch. No screen shows it as a card any more: Data
dropped it on 2026-10-06 (the uploads above it already say which files these are), and Content,
Preflight and Publish on 2026-10-07 — with the steps opening only through Next (`ui/progress`),
the batch on those screens is always the one just saved on Data, and gate 0 names the export.

**What the batch publishes is part of it** (2026-10-09). The mode — pages, links or both — is
chosen on Data, step 4, before anything is counted, because it changes what a product needs: a
links-only batch writes no page, so `ui/progress` passes over Content (`not_needed`), and the
doctor's `--mode links` marks the page-only checks (`lib.preflight.PAGE_ONLY_CHECKS`) not
applicable. It is saved by Next as `mode` on the selection's ledger line (`lib.provenance`, ledger
version 2; version-1 lines still read, with no mode) and read back by `lib.batch.in_force`, matched
on the live selection's hash like the export agreement — so a selection replaced outside the shell
does not inherit one. **There is no default.** A batch with no recorded mode has Next off on Data
and Publish blocked, because guessing `pages` for a batch meant as `links` is a guess with a
permanent other half. Gate 0 still exists and still must be confirmed: it shows the batch's mode
with Confirm / *Change on the Data screen* / Cancel. A mode remembered with a batch is not a run
approved, so `PublishSession` stays unpersisted. A client with no `process_list` has nowhere to save
a mode, and keeps the chooser on gate 0.

It replaced three booleans held
for the life of the process, which gated whether the Data screen showed the selection at all: before
this, restarting the shell hid the grid while a run went on consuming the file, as the band that
replaced it said in so many words. A batch that is invisible and live at once is worse than either.
The risk that rule was reaching for — ticks chosen against an export since replaced — is now
detected and stated instead of hidden. Cached on every input's `(mtime, size)`, because it costs two
workbook parses and three digests, which is also why `rail_facts` still carries none of it.

**The grid is the scope list joined against the export**, and that join is the reason this screen
was rebuilt. A barcode on the list that the export carries no row for produces no error, no plan
row and no count anywhere in the tool; the operator's only evidence is a total one smaller than
they expected. It gets its own table, above the rest.

That table is **read-only, and the rows in it are always kept**. It had checkboxes for one round
and they were wrong twice over: a tick there would have meant "keep this row in the file" while
the identical tick a few pixels below means "keep it *and* run it" — one control answering two
questions — and it made the count read "38 of 38 row(s) will be processed" when 37 was the most
any run could touch. Unticking one would not have stopped it being processed, since nothing was
going to process it; it would only have deleted the evidence that a barcode on the list has no
product behind it, which is the entire point of the table. Removing one is a spreadsheet edit and
a re-upload.

*Superseded 2026-10-09:* those rows now carry a tick box like every other, at the operator's
request, because the problems listed under the table are the ticked rows' problems. The two
questions are kept apart by the save, not by the control: a tick means *in this batch*, and Next
writes only the ticked rows that can run, naming the rest.

**When the export has not been parsed, the tables do not split.** Every row would land in "not in
the export", which is not a finding but the absence of one — and it would leave the screen with no
checkboxes at all, taking away the operator's long-standing ability to choose a batch before
parsing. So the whole list shows as one table under a warning band instead.

The join is `lib.process_list.rows_in_export`, in `lib` rather than on the screen, on
`product.gtin14` against the sheet's own normalisation — `lib.preflight.in_scope`'s exact pair.
`check_scope` deliberately emits `ProductRecord.gtin` and not `gtin14` "because a normalised
variant here would silently fail to match for any client whose feed carries 13-digit codes", and a
third normalisation invented on a screen would report every good product as missing.

**The checkbox inverted, and that is a hazard, not a detail.** It used to mean *remove this row*;
it means *keep this row*. An operator with the old habit ticks what they want gone and publishes
exactly those. Four mitigations, all cheap and all required: the *Remove selected rows* button is
deleted outright so no control carries the old wording; Save reports the **delta** ("Saved 36
row(s). 2 dropped") rather than the end state, which is the sentence that contradicts them;
`theme.action(danger=True)` kept it red until Save became the Next button; and the worst case is
undone by uploading the list again, which the screen requires in any case.

`pagination=0` on the table is **mandatory, not cosmetic**. With pagination on, Quasar's header
checkbox selects *this page*, and a save would quietly drop every row the operator never scrolled
to. Selection is independent of the filter in Quasar 2.18 — verified in a browser: deselect two,
type in the box, clear it, and the count holds — but the header checkbox's tri-state describes the
rows the *filter* is showing, not the file. That is what the count label beside the table is for,
and why it names the file's numbers first and the filtered view second.

Saving keeps the previous version, freezes and filters the header row so the file is still workable
in Excel, and refuses to write a list with no GTINs at all, because that would produce an empty
plan and a run that reports success having published nothing.

Two staleness facts the screen was not stating. The product count comes from `products.json` and
the "export modified" date from the workbook: **two files, two mtimes, shown as one fact**. Upload
without pressing Parse and last quarter's count sits under today's date, so the screen compares
them and warns. The data-quality report is dated for the same reason — a rebuild that wrote nothing
new leaves last week's worklist on screen looking exactly like this week's.

Every in-scope SKU held for want of a confirmed video carries a per-row mark saying what it waits
on — `no confirmed video in fr`, `no confirmed video in nl, fr`, `no confirmed video in fr; two videos in nl` — from `lib.video_status`, loaded by
`lib.preflight.load_video_status` (the report reads the same, so the two say the same words about
the same product). It used to say "no video yet" for all of them, which on the pilot covered three
different jobs. Data is the only per-SKU grid in the shell, so it is the only place that fact can
live per row; the screen used to show none of it.

**Only a held product gets a mark**, because the line under the table counts marks as holds. Two
videos confirmed for one product in one language is a hold like none: the page could not get
either, so the gate (`fully_mapped_gtins`) requires **exactly one** per language. It used to count
any confirmed row, and a two-video product published with no video in that language, reporting
success.

**Under `media.publish_without_video` a missing video is a mark, not a hold.** The product is
eligible, sits in *Missing video(s)* and in the eligible table with its Video cell filled, and the
run publishes it without a video there. Two confirmed videos in one language still hold it,
so it is in *Not eligible* with "two videos in nl — the client must keep one".

**The Data screen redraws from three entry points** — a list or export arrived (step 4, the
funnel, the report); a sign-off sheet was applied (step 4 — rebuilt, with `Ticks` keeping the
choices — and the report); a sign-off sheet only arrived (the report, since nothing was applied).


### The videos, on the Data screen

This was a screen of its own, `/videos`, under *This machine*. It exists because the mapping decides
whether a product can be published at all — with `media.restrict_to_mapped_gtins` on, a product
without a confirmed video in **every** language never reaches the plan, so an operator could
complete every screen and still produce an empty plan with the fix available only in a text editor.
That is also why it moved: an input that decides a batch's size belongs where the batch is chosen.

**`/videos` was deleted, not kept as an unlisted route.** With no screen linking to it, a surviving
route would have been a second live editor of one client-sign-off file, holding its own copy of the
text and knowing nothing of the session below. What is left lives in two components:
`ui/video_signoff_panel.py` (step 3) and `ui/video_map_panel.py` (the session it
writes through). Both are import-checked as `PANEL_MODULES`, and the AST contracts that
used to stop at `ui/pages/` read `ui/*.py` too — a rule an extraction could escape by moving a
handler one folder up is not a rule.

**The mapping is edited in its file, not in the shell (operator decision, 2026-10-06).** The
row-by-row editor — *The mapping, file by file*, a fold under the uploads with its own coverage
figures, fuzzy hints and a Save — was removed: what a batch needs to know about videos is already in
step 4's *Missing video(s)* and report §1, and a second editor of one client-sign-off file invites
edits nobody signed. The operator adjusts `videos/mapping.yml` by hand (the guide shows the line
shape) and reloads; `build_video_map --check` and the doctor still report its gaps.
`MappingSession` survives for the import alone: every write re-reads the file, and a file changed on
disk since it was read — now the normal case — is re-read before the import is re-planned. Its
staged-edit machinery and the "save your row edits first" refusals went with the editor.

**The import's write is not red.** Red is for a write that is hard or impossible to undo — Publish's
run and production confirmation, Setup's two saves. The import keeps a `.bak` and is refused if the
candidate lost a row. `tests/ui/test_shell_chrome_contract.py` holds red to two screens. And it will
not:

- **Re-draft the file.** Confirmed rows are client sign-off. Drafting stays a terminal job, where
  redirecting the output over the mapping is a deliberate act rather than a click.
- **Round-trip the YAML.** Each row's trailing comment records which fuzzy hint its GTIN came from
  — the evidence behind the sign-off — so `ui/video_map_edit.py` rewrites one line at a time, in the
  spirit of `ui/config_edit.py` on `clients.yml`.
- **Write a file that lost a video.** A shared video may lose a spare row — one barcode fewer —
  but nothing here takes a video out of the file, so one that has disappeared is a fault in the
  tool, and the file is left alone.

It also **imports the client's filled-in sign-off sheet**, which is the one input this pilot has
been waiting on and which used to be re-typed into the rows by hand — one row per video, where one
transposed digit maps a video to the wrong product with nothing downstream to catch it.
`scripts/report_video_candidates` writes the sheet to send; this reads it back. Four decisions in
it, each of them a refusal:

- **The sheet leads, per barcode** (operator decision, 2026-10-24 — it replaced "a confirmed row
  is never overwritten", under which a two-video product could not be fixed from the sheet at
  all). Per language, a barcode the sheet names gets exactly the videos the sheet gives it, moved
  off any other video, which becomes `skip` when none is left; a barcode the sheet does not name
  keeps its video, so a row naming a new barcode for a video *adds* it (the video is then shared)
  rather than replacing what was signed off. The operator chose that over "the row leads" knowing
  the cost: a sheet cannot take a barcode off a video except by `skip`. One video may carry several
  barcodes — comma-separated in the cell, one row per barcode in `mapping.yml`, which is the shape
  every consumer that decides publishing already reads per barcode, so the gate, `resolve` and the
  video-arrival reclassification needed no change. A sheet that would leave one barcode on two
  videos in a language is refused **whole**: its rows move barcodes between videos, and half a move
  is a mapping nobody asked for. A live page that loses its video keeps it, and the screen says so
  — the write path sets a video, never removes one.
- **The upload writes nothing.** It produces a plan, the plan is shown per row, and a second press
  applies it — the shape the row editor beside it already has. The apply then re-reads the file, and
  everything that counts from it redraws — coverage, the fold, the Video column, the report.
- **A barcode is validated against the export, not against a check digit.** A transposed digit
  usually yields a barcode no product has, which is catchable; a check digit stays silent on the
  case that matters, a typo that happens to be another real product. Scientific notation
  (`8.7132E+12`) gets a message of its own, because it is the commonest way a barcode arrives
  broken and the least obvious to whoever sent it.
- **Every sheet is kept, and the column choice is noted beside it.** It used to be read and not
  kept, which left the data-quality report nothing to re-read — and re-reading it on every render is
  what makes "what would the sheet still change?" (§1d) correct after a fill, a hand edit or a new
  sheet. `lib/video_signoff_archive.py` keeps each upload as `videos/signoff/signoff-{stamp}.xlsx`
  (through `input_layout.unique`, read back by mtime, never by name) and records the chosen columns
  **with the headings they were chosen against** in one `signoff/signoff.json` — so a sheet edited
  in place since is refused by the report, in words, instead of being read with a column moved.
  Beside `mapping.yml`, not under `process/`: the folder README states that a run reads exactly two
  files. Four alternatives were rejected: the filename (cannot carry heading text); a `.json` beside
  every sheet (doubles a folder people open in Finder); a `history.jsonl` line (its `What` is a
  closed set that four screens read, and that ledger belongs to the export's tree); not keeping it
  at all (the report would be describing a sheet nobody can find). A sheet is kept only once it
  reads as a spreadsheet and is not an export, so the report never describes something nobody sent.
- **The operator says which column is which; the recognised names only pre-fill the pickers.** The
  list of accepted spellings is a guess about somebody else's spreadsheet, and it was wrong about
  the only real sign-off sheet there is — it calls the barcode `current_gtin`, so the import refused
  the file it was built for. Adding that name fixes today and not the next sheet. The pickers show
  even when the guess is right, because that is what makes the guess auditable: a column silently
  read as the barcode is the one mistake on this screen that would publish the wrong video. It is
  also why `read_sheet` has a second pass that finds a header row by *shape* — a sheet we cannot
  recognise must still arrive with its headings listed, since "rename your columns to match a list
  we never show you" is not a fix.

The workbook reader behind it is `lib/xlsx.py` — `lib.process_list`'s, lifted out when this became
its second caller. Both read files whose table starts below a title row, on a sheet that is not the
first, in Strict Open XML that `openpyxl` reads **zero sheets** from. Normalising the barcode is
deliberately *not* shared: `process_list` zero-pads without stripping punctuation and
`lib.media_video.canon_gtin` strips then pads, so the import normalises on the mapping's own rule,
because that is what will compare its value against products forever.

### Content

Import the copy, then look at the **coverage** figures before reading it. The copy is written fresh
for each run and never stored, so the question is not how much has piled up but whether *this* file
answers every unit the run will publish. Not every in-scope unit: copy is written for the rows a run
creates or changes, so an already-live, unchanged unit needs none, and the figures say how many were
set aside for that reason. Its fingerprint covers `{inputs, language, prompt_version}`, so editing
one product in the feed leaves that unit uncovered — and an uncovered unit with no producer on this
machine is dropped from the plan (E21). The screen lists those units by GTIN and language, so
"request fresh copy" is an instruction rather than a hunch.

**The copy review shows this run's batch, not the whole file.** It reads `in_scope_gtins` from the
doctor's `scope` check and splits: this run's entries, then the in-scope GTINs with **no** copy (the
work still to do), then anything outside this run's scope. That last group used to be ordinary —
the cache accumulated every unit ever generated on this machine — and it is not any more. A per-run
file holding GTINs this run will not touch means it was written against a *different scope list*,
so the screen says so as a warning and keeps the list behind a fold rather than presenting it as
background.

Scope is not recomputed here. `lib.preflight.in_scope` stays the single implementation and the
doctor carries the answer across. If it cannot be read the screen shows the whole file and *says*
so, because filtering to nothing would read as "there is no copy" — wrong in the direction that
stops an operator looking.

Coverage and the review come from **one** preflight run, and the import button refreshes both: they
describe the file it just replaced.

**There are two producers, and which one this screen offers depends on one config field.** With
the client's `generator.api_key_env` naming a variable that has a value, the screen leads with
**Generate the copy** and a button that runs `python -m scripts.run_generate {client} --backend
api` as a subprocess — the key is read by the child from `.env`, never by this process. With it
unset there is no button and no Anthropic egress at all: the copy is written on the maintainer's
machine, in a Claude Code session with the `content-generator` skill, and arrives here as a file
to import. Both write the same `generation_results.json` against the same contract.

There is still no **export** button for `generation_requests.json`, and that is deliberate: the
skill reads the pending units itself from the same export, so what the operator sends is the list
this screen already shows, and a file written here for someone else to run `--emit` against would
add a hand-off without removing one.

> This paragraph said the opposite for a while — "asking for that copy is a conversation, not a
> button… this machine never runs `run_generate`" — which stopped being true when the key became
> optional, and stayed on the page for a release. The intro of this document was updated in the
> same change and this section was not, so the doc contradicted itself in two places about the
> most consequential thing on the screen.

### Preflight

Runs the doctor in a subprocess, with the batch's `--mode`, and renders each check with its remedy. Two buttons: offline (no
credentials, no sockets) and everything. The full run authenticates against WordPress and mints a
GS1 token; both are read-only.

It runs the offline checks on arrival, so the screen is never blank — which is also why the
buttons need to *look* like they did something. The subprocess runs off the event loop and the
buttons disable while it does; without that, a blocking `subprocess.run` in a click handler held
the loop until it had already finished, so "running…" never reached the browser and the screen
looked identical from click to result. Each result carries the time it finished, because on a
healthy machine an identical list is exactly what a working re-run produces.

The first line to read is **"What a run would touch"** — how many products survive the scope list
and the video allowlist. Every check below it reports on that scope rather than the whole
catalogue.

### Publish

The gates come from `lib/gates.py`, which `flow-orchestrator/SKILL.md` is checked against by a test
in both directions. Each is shown with **why it exists**, not only what it asks — a form that asks
without saying why teaches you to answer without reading, and this flow's whole cost is
concentrated in one unreviewed click.

**Some options exist only in the chat flow, and the data says which.** `post_run`'s *Explain each
error* needs a model to read the run log, so it is marked `chat_only` in `lib/gates.py` rather
than deleted: the shell not being able to do something is no reason for the surface that can to
lose it. The screen renders `shell_options`, so such an option cannot become a button that does
not do what it says, and the contract test derives what must be rendered from the gates instead of
from a hand-maintained list of exceptions.

`row_diff`'s per-row *apply*/*skip* used to be on that list and are not any more — the screen
walks the rows now, so the flag would have been describing the surface rather than the option. The
flag moves when the surface does; that is what keeps it meaningful.

**And the data says what each answer *does*, in three states rather than two.** `GateOutcome` is
`ADVANCES`, `STOPS`, or `REDISPLAYS`, because one boolean was answering two questions — *does this
carry the flow on* and *does this stop the run* — which coincide everywhere except on a detour.
Gate 6's *Show full diff* is the detour: in the chat flow it prints the rest and re-prompts, so it
does not advance; on a form it is the terminal answer to its gate. It was once the only option that
gate could render here, and read as a refusal that one button ended the run with nothing on the
screen to undo it — reached by answering *Review changed*, the most careful answer on offer. It now
lifts the row cap and means nothing else. *Change mode* and *Regenerate* are detours too, at gates
that are required, so the run is still held — but held as **unanswered**, which is what the screen
says, instead of reporting a cancellation nobody made.

`ui/session.py` **refuses to build the command** while any required gate is outstanding. Not a
warning: a function that raises. That is the improvement over prose, which can be paraphrased,
compressed, or skipped.

- Choosing `links` or `both` turns the banner red and inserts the production gate, which needs the
  client id typed in full.
- The dry run is mandatory and runs the same command with `--dry-run` and every other flag
  identical.
- `--i-understand-production` is appended **only** after the production gate is answered, and never
  on a dry run.
- An empty plan is refused rather than run. Publishing nothing successfully is the one outcome
  indistinguishable from success.
- **Gate 6 walks every CHANGED row, and confirms only the ones you applied.** It used to do
  neither. It listed `[row for row in plan.rows if row.diff]` — and `state.json` records the prior
  `title` and `wp_url` and nothing else, so a row changed in the product body carries no diff at
  all. On a live plan that displayed **one** row of the twenty it was confirming.
  Meanwhile *Review changed* returned exactly what *All* returned, because the selection switched
  on classification alone. So the most careful answer on the menu was the same click as the most
  sweeping one, and fixing one product's French title meant rewriting twenty live rows.

  Each row now carries *Apply* and *Skip*, and a row left undecided is **not** published — the
  safe default, and the one that makes narrowing possible at all. NEW rows are confirmed
  regardless; the rows are narrowed to the languages chosen at gate 2, since a decision about a
  row the language subset drops is a decision with no effect. Rebuilding the plan forgets every
  decision: these are not the rows those answers were about.

  The 50-row display cap stays, and now says what it costs. With a control on every row a capped
  list drops rows out of the *decision*, not merely out of the display, so the rows past it are
  named as undecided and therefore unpublished, with *Show full diff* offered to bring them on
  screen. Which rows a run confirms is `PublishSession.confirmed_pairs` — in the module that is
  tested without a browser, rather than half on the screen as it was.
- **Gate 0 leads with what this run could touch, not the size of the catalogue.** It used to
  render the length of `products.json` under the label "products in the catalogue" — honest, and
  the wrong number: the whole catalogue on a run scoped to one product, at the gate where the operator forms
  their picture of what they are about to do. It now shows the doctor's `scope` check — *N in scope*, *M in the catalogue* one size down, and the doctor's own sentence naming what removed
  the rest. The shell does **not** compute scope itself: `lib.preflight.in_scope` already composes
  the scope list and the video allowlist, and a second implementation of "what will this run
  touch" is the same class of mistake as a second implementation of the gates.

  Neither figure is the row count. Scope deliberately cannot subtract the units already published
  — that needs `state.json`, and an idle read of a corrupt one quarantines it (E19) — so it is a
  ceiling, and the real number arrives at step 5.

  If the payload cannot be read the gate shows a dash and says so; it never falls back to the
  catalogue total, because a wrong number under the right label is worse than no number. An empty
  scope gets a danger band: that run would write nothing and report success.

  **One `doctor --json --offline` per redraw**, in `_redraw` and shared by gates 0 and 3. Gate 3
  already ran one; a second would have been ~500 ms of blocking subprocess on every answer, and
  two gates could have disagreed about the same run. A contract test fails if any gate renderer
  runs its own.
- **The missing-field gate (step 4) appears only when the plan actually dropped a unit for a
  missing `product_name` (E18), and it names each one.** It used to render on every run, offering
  *Skip this unit* beside no unit — and of its three answers only *Stop the run* had any effect,
  so the one live control on a question about nothing was the destructive one. Gate applicability
  now consults the plan (`needs_missing_product_name`), refreshed on **every redraw** rather than
  once per run, because the plan is built at step 5 — in the middle of the walk — and a fact read
  before there is a plan decides a gate that then never appears. Building a plan that drops units
  says so in a toast as well as by the gate appearing above.

The Gate index's **Modes column is checked against the code** in both directions, not only the
ids and step numbers. It is prose, it said `all` for a gate that was never meant to fire
unconditionally, and nothing compared the two — which is how that defect shipped.

### Setup

The client, the site, the environment, the credentials, and every configured file with **how long
ago it was modified**. The export path is authoritative and has no command-line override, so a
workbook saved somewhere new is invisible to the tool — the date beside it is the fastest way to
notice.

The two most expensive mistakes in this pipeline are both *config* mistakes that nothing downstream
notices: pointing at the wrong export, and pointing at production. Both were previously made in a
text editor, in a file whose rules are not visible from inside it. Hence the form, and five things
about it:

- **Only changed fields are written.** The screen shows the *resolved* config, with the `defaults`
  block merged in. Saving all of it would freeze every inherited default into this client's own
  block, so an untouched form writes nothing at all.
- **Everything else in `clients.yml` survives byte for byte** — comments, alignment, quoting style,
  and every block the form does not show. The file is a document, and several of its comments are
  the only record of why a value is what it is.
- **The result is validated before it replaces the file**, by the same `check_config` the doctor
  runs, which reports every offending field rather than the first. A candidate that would not load
  is refused and the file is left alone. The previous version is kept as `clients.yml.bak`.
- **Switching to production asks for the client id, typed in full** — the same decision the
  production gate asks about, made once here instead of once per run. Two further inconsistencies
  the schema cannot express are refused too: a default language that is not in the language list,
  and `production` with no production account or credential names.
- **The client id is not editable.** It is the path to `output/{client}/state.json`, which records
  every GTIN already published. Renaming it orphans that file rather than moving it, and every
  published GTIN would classify as new on the next run.

**Credentials are write-only.** The fields set values in `.env` and never show one back; an empty
box means *leave this one alone*. Values are always quoted, because the commonest credential
failure here is an application password that lost its quotes and was truncated at the first space —
which the screen also reports, as a group count, without disclosing anything. There is **no
Anthropic key field**, and there will not be one.

`gdsn_map`, `acf_map`, `brick_category_map` and `generator` stay read-only, each with the reason
beside it. The first three were settled by a field walk against the live site; `generator` is the
E21 switch, not a preference.

The Test buttons run `python -m scripts.doctor` and show the checks that answer for that part of
the form. They are the preflight's own checks rather than a second opinion — and when the run as a
whole fails on a check the button did not ask about, it says so instead of showing green.

### Runs

Reads `output/{client}/runs/*.jsonl`, newest first, and distinguishes a **partial** log — a run
that stopped mid-way — from a finished one. That is the case that matters most: live pages and
permanent GS1 records may already exist for the rows that landed.

**Build the result sheet** sits on each run's card, not on Data. The artefact is per-run and
lands beside `{ts}.jsonl` as `{ts}-scope.xlsx`; a screen showing no run would have to guess which
run it was about, and `--run` is passed explicitly for the same reason — two runs a second apart
are `{ts}.jsonl` and `{ts}-1.jsonl`, and the wrong one of the pair is indistinguishable from the
right one until somebody opens a page URL that was never visited.

The sheet is the operator's own scope list handed back with what the run did appended: one row per
SKU, their columns verbatim, then `in_scope`, `result`, and status/page/detail per language. A
`units` tab carries one row per `(gtin, language)` uninterpreted, which is where "nl published, fr
failed" survives the worst-of reduction; a `legend` tab gives every value a sentence so the file
can be forwarded without a covering email.

**It is a report, written after — nothing reads it back.** That is the whole difference from the
design where the scope list grows a run-status column and a later run filters on it. `lib/
process_list.py` records what that cost the last time: a status column silently meant its opposite
for a client whose file said `no`, in both directions, and neither direction raised anything. Two
status columns rather than one, for the same reason: `in_scope` is the decision and `status_{lang}`
is what happened, and one cell answering both has a meaning that depends on when you read it.

`plan.json` is overwritten by every `run_plan`, so for anything but the newest run it is somebody
else's document. A plan generated *after* the run is refused with a line on stderr rather than
quietly contributing its holds to a run that never saw them.

Above the logs, **"Does the site match the ledger?"** asks the site instead. Everything else on
this screen is what *this machine* recorded, which cannot show a page created by anything else —
another machine whose `state.json` has not come back, a hand edit in wp-admin, or a run that
failed part-way.

That last one is why it exists. The first real publish through this shell published a product in
Dutch and failed on French; sibling-blocking correctly held the product, so the row was logged as
an **error** and nothing was written to state — while the Dutch page was live, correct and
publicly reachable. Ten entries in the ledger, eleven pages on the site, and nothing in the tool
could say so. A later run classifies that product NEW, and only the slug lookup inside the
WordPress client stops it creating a duplicate.

It lists every page carrying a `meta.gtin`, **per language explicitly** (an unscoped query on a
WPML site answers with the default language only, so skipping that would report every translated
page as missing), and diffs both directions. It reports and never repairs: each divergence has
more than one correct resolution, and choosing needs someone who knows which machine published
last. `python -m scripts.reconcile` is the same check in a terminal.

---

## Where the safety actually lives

| Guard | Where |
|---|---|
| Refuses a real production run without `--i-understand-production` | `scripts/run_execute.py` |
| Refuses a `--only links` GTIN whose target does not serve | `scripts/run_execute.py` |
| Refuses to build a command past an unanswered required gate | `ui/session.py` |
| Refuses to replace `clients.yml` with a file that would not load | `ui/config_edit.py`, via `lib/preflight.check_config` |
| Which gates exist, and which are non-negotiable | `lib/gates.py`, checked against `SKILL.md` |
| The prompt text a model reads | `.claude/skills/flow-orchestrator/SKILL.md` |

The first two are inherited unchanged, because the shell subprocesses the scripts rather than
importing them. That is also why it cannot import `main()`: `load_env()` lives in each script's
`__main__` block on purpose, so an in-process call would have **no credentials** — and calling
`load_env()` in the shell would put production secrets into a long-lived desktop process and arm
the staging-guard variables inside it. A test asserts no module under `ui/` does either.

---

## Regenerating the screenshots

[`operator-guide.md`](operator-guide.md) embeds one PNG per screen from `docs/images/`. They go
stale whenever the chrome changes, and they are **captured against a throwaway client, never
against a real one** — `clients.yml` is gitignored because it is client configuration, and a
screenshot bakes the client name, real GTINs, product names and the site URL into a committed
binary that no `.gitignore` protects.

The recipe, all of it outside the repository:

1. Copy the repo to a scratch directory. Use `clients.example.yml` as its `clients.yml` —
   `democlient` is already defined in it. Copy the repo rather than only moving the working
   directory: `lib.config.DEFAULT_CLIENTS_PATH` is anchored to the **repository** root, so a
   scratch cwd beside the real checkout would still read the real `clients.yml`.
2. Write the operator files with `python -m scripts.make_demo_export`. It lands a 24-sheet GDSN
   export and a scope list at the configured paths, and already puts **one barcode on the scope list
   that no export row carries** — the Data screen has a table whose only job is to show those, and
   without one it is not in the picture. It also writes the videos: a placeholder file per mapping
   row, `mapping.yml` in the row-per-line shape the editor accepts, and
   `test-uploads/video-signoff.xlsx` with one row per import outcome and the real sheet's
   `current_gtin` column. Of the twelve products, three are held by a mandatory rule; before the
   sheet is applied seven pass the video gate and five are held, after it eight — so the figures are
   neither all zero nor uniformly green. To photograph the uploads, move the export and the list
   into `test-uploads/` and upload them through the pickers.
3. Parse it: `python -m scripts.parse_export democlient`. Then **backdate `products.xlsx` behind
   `products.json`**, or the staleness band fires on files written seconds apart and the screenshot
   tells the operator to re-parse for no reason.
4. Synthesise what no command produces — `generation_results.json`, and `runs/*.jsonl` — through
   the models in `lib.generator`, so the shapes are right by construction rather than by hand.
   Leave `input_fingerprint` **null** on each result item: it is optional, and any other value
   fails the doctor's staleness check.
5. The video mapping is already there from step 2. Do not write one with `yaml.safe_dump`: its
   block style is refused by the mapping editor.
6. Run `python -m scripts.run_plan {client}` there so the plan and the rail facts agree, then
   serve it with `ui.run(..., native=False, show=False)` on a spare port and drive Playwright
   at 1280x860.

The scratch directory's absolute path shows up in the Preflight screenshot's first check, so put
it somewhere that is not a private path.

## For IT

- **Loopback only.** Port 8477, `127.0.0.1`, native window — no shareable URL.
- **The install is user-scope and version-pinned** — `uv` at a pinned version, CPython 3.11, and
  86 packages resolved in the committed `uv.lock` with hashes. No administrator rights, no service.
  [`operator-install.md`](operator-install.md#for-it) has the detail.
- **No Anthropic egress and no LLM credential** on this machine.
- **Outbound**: the client's WordPress site, `gs1nl-api.gs1.nl` (or its acceptance host), and the
  image hosts named in the product feed. That last one is currently unconstrained by an allowlist,
  which is a fair question to ask about.
- **Credentials** are the pre-existing `.env` at `chmod 600` — a WordPress application password
  with editor rights and GS1 production OAuth credentials. The shell never *loads* them: it does
  not call `load_env()` or any dotenv reader, so no secret enters this long-lived process's
  environment and the staging-guard variables are never armed inside it. The subprocesses load the
  file themselves. The Setup screen writes to it and re-applies mode 600, and reads it only far
  enough to say whether a name has a value.
- **No auto-update, no telemetry, no network listener beyond the loopback socket.**
