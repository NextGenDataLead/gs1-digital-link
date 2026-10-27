# Publishing a batch: the operator's guide

This is the walkthrough for the person who runs the tool. It assumes no Python, no terminal and no
knowledge of how any of it works — only that you have the folder, you have double-clicked
`start.command` (or `start.bat`), and a window is open in front of you.

If that has not happened yet, [`operator-install.md`](operator-install.md) is the page for it.
Come back here once the window opens.

▶ **[Watch a batch first](videos/publishing-a-batch.mp4)**: 6½ minutes, one `both` batch on a demo
client, from the uploads to the dry run, with what to read on each screen.

> **The one thing to know before you start.** Some of what this tool writes cannot be undone. A
> WordPress page can be edited or deleted afterwards; a **GS1 Digital Link record cannot**. Once a
> barcode has a record, it exists permanently — it can be emptied and switched off, never removed.
> The tool asks you to confirm before it writes one, every time, and this guide points out each
> place it does.

---

## What you are doing

You have a spreadsheet of products from GS1. For each product you want:

1. a page on the website, in each language, and
2. a barcode that leads a phone to that page.

The tool does both. You work through four screens in order, and each one has a single job:

| | Screen | Its job |
|---|---|---|
| 1 | **Data** | Bring the batch's files — product list, GS1 export, the client's video sign-off — and choose which products it covers |
| 2 | **Content** | Get the marketing text for those products, and read it |
| 3 | **Preflight** | Check everything that can be checked before anything is written |
| 4 | **Publish** | Do it, one confirmation at a time |

A **batch** is one pass through those four. You will do it again next time there are new products.

---

## The window

<img src="images/rail.png" alt="The navigation rail: four numbered steps — Data, Content, Preflight, Publish — then a rule and, under the heading This machine, Setup and Runs." width="240">

The strip down the left is how you move around. It has two halves, and the difference matters:

- **The numbered four are the batch, and they open in order.** Each time you start the tool only
  **Data** is open; the next step opens when you press **Next** at the bottom of the one before
  it, and the ones not reached yet are greyed out. Going back to an earlier step greys out the
  steps after it again, until you press Next there once more — so a later screen never works on
  something you might have changed. Under each one the tool shows you a small fact about where you
  stand — how old the spreadsheet is, how many rows are planned. They are facts, not ticks:
  "today" tells you the file is recent, not that it is the right file.
- **Below the line is everything else.** *Setup* is the site settings, set up once and rarely
  touched. *Runs* is the record of what happened. Neither is a step; you go there when you need
  them. (The video mapping used to live here too. It is part of **Data** now, because it decides
  whether a product can be published at all.)

At the top it always says which client you are working on and, underneath, the environment:
**TEST** in grey, or **PRODUCTION** in red. That tag is the single most important thing in the
window — PRODUCTION means the GS1 records this run writes are real and permanent.

If the window is narrow, the strip folds up behind a **☰** button:

<img src="images/rail-narrow.png" alt="The same navigation in a narrow window, opened from a hamburger button." width="420">

Every screen also prints the exact command it is about to run, in a grey monospaced line. You never
need to type it. It is there so that if something goes wrong, you can copy that line into an email
and whoever helps you knows exactly what happened.

---

## Step 1 — Data

<img src="images/data.png" alt="The Data screen as it opens: jump links, then three uploads side by side — the product selection list, the GS1 export and the video sign-off sheet — a Clear all — start fresh button, step 4 with the three choices both, links and pages and a note that Next waits for one, and a band asking for the list and the export." width="900">

*This is the screen as it opens.* Coverage, the product tables and the issue report appear once
the list and the export are both there — see below.

**What this screen is for:** everything a batch is made of, measured against the one thing you
chose — your list of products. Three files, then **choosing** which products this batch covers. The
links under the title jump to each part.

Every heading has a small **ⓘ** beside it. Press it to read what that file is and where it goes.
Nothing you need to act on is hidden behind one — a warning always sits on the page in a coloured
band.

**Everything is remembered.** Close the app, open it tomorrow, and your files and your saved choice
are still here. To begin a new batch from nothing, press **Clear all — start fresh** under the
uploads. It asks first, then moves the list, your saved choice and the export out of the way —
nothing is deleted, and the client's video sign-offs are not touched.

**What you do:**

1. **Upload the product selection list** — which barcodes this batch may touch. Press the picker
   and choose the spreadsheet. It is read before it replaces anything, so a file that will not open
   is refused and the list you were using stays put.

   Under the uploads, a line **Product list: barcode column ‘Barcode’ (…)** names the column it
   reads as the barcode, with the first few values from it. A list headed `Barcode` is installed
   straight away; check the column anyway, and open the line to change it (or the column holding
   the link to the page, where the client's settings name one). A list that calls it something else (`EAN`, `Artikelnummer`, a title
   row above the table) waits: choose the column, then **Install the list with these columns**. A
   column with no barcodes in it is refused, and the message quotes what it holds. Your file is
   kept exactly as you sent it; the installed list carries the headings the tool reads.
2. **Upload the GS1 export** — the product *data*. It is read as soon as it arrives: a spinner runs
   while that happens, then a line says how many products it found. **If it is not a GS1 export,
   or something the tool needs is missing, it is put back and nothing changes** — the file you had
   is still there, and the reason appears on screen. Uploading is the check; there is no other
   button.
3. **Upload the video sign-off sheet** — when the client sends it back. See *Videos* below. If
   there is no new sheet, skip this step.

   The three sit side by side because none of them waits on another: bring whichever you have.

4. **What this batch publishes** — **both**, **links** or **pages** (the table under *Step 4 —
   Publish* says what each writes). Choose it here, before you choose products, because it changes
   what a product needs: a **links** batch writes no page, so it needs no text and the **Content**
   step is passed over. **Next stays off until you choose.** Your choice is saved with your
   products when you press Next, and the Publish screen shows it again and asks you to confirm it
   before anything runs.

<img src="images/data-choose.png" alt="Steps 4 and 5 for a pages batch: pages chosen; Coverage reading 13 selected, 9 can run, 1 not in the export, 3 not eligible, 2 missing videos; then every product of the list with a tick box, a Status and a Detail column; and under it three folded tables — Not in the GS1 export, Not eligible, Missing video(s) — listing only the ticked products." width="900">

**Coverage** counts **the products you ticked** in step 5 — not your whole list. **Selected** is
how many you ticked (and, under it, how many are on your list), **can run** is how many of those a
run will act on — what Next saves — and the figures after it say why the rest cannot: **not in the
export**, and then for a batch that writes pages **not eligible** and **missing video(s)**, or for
a links batch **link problems**. Tick or untick, and the figures follow.

5. **Choose the products and save.** First, **your products**: every row of your list, each with
   a tick box, a **Status** — what a run would do with it — and a **Detail** saying why:

   | Status | Means |
   |---|---|
   | **can run** | It is in the export and nothing stops it. |
   | **can run · no video** | It runs, but goes live without a video in the language named. |
   | **not in the export** | The GS1 export has no row for this barcode. Fix it in MyGS1, or the barcode is wrong. |
   | **not eligible** | Pages only. A mandatory value is blank (fixed in MyGS1, never here), or the client confirmed two videos for one language. |
   | **link problem** | Links only. No page to point the GS1 record at, an address not on the client's site, or a page that does not load. |
   | **checking link** | Links only. The page is still being looked at — a few seconds. |

   Your saved batch arrives ticked — every row, for a list just uploaded. Untick a product to
   leave it out. **Under the list, one folded table per problem, listing only the ticked
   products that have it** — *Not in the GS1 export*, and then *Not eligible* and *Missing
   video(s)* for a batch that writes pages, or *Link doesn't work* for a links batch. Press a title
   to open it; the count is in the title either way.

   **A links batch is judged on its link and nothing else.** It writes the GS1 record and no page,
   so a missing video or a blank page field does not stop it. Its link is the address in the
   **Link naar site** column of your list, or — when that is empty — the page this tool published
   for it. If neither exists, or the page does not load, the product cannot run until the address
   is fixed in your list and the list is uploaded again.

   > ⚠️ **A tick means keep.** Untick a product to leave it out. If you used an earlier version of
   > this app, the button under the table said *Remove selected rows* and meant the opposite — it
   > is gone.

   **Next saves the ticked products that can run, and only those.** A ticked product that cannot
   run is left out and named in the line above Next. Use the filters to find products — the
   **Status** filter shows, say, only the ones with a link problem; filtering changes only what you
   can see, never what is ticked. Your ticks also survive changing what the batch publishes, and
   the client's sign-off sheet being applied.

**Issue report for the client**, at the bottom, is the report you send to the client, shown as
they will read it — about the products you ticked, and it follows your ticks: (1) how many of
them failed, (2) the issues by category, each with its products and the reason, and (3) the failed
products, each with its reasons. A missing video is listed but does not count as failed — the
product runs without it. **Download PDF** and **Download Excel** under it save the same report in
your **Downloads** folder and open it in your own PDF reader or spreadsheet program, ready to send.
A dated copy is also kept in the client's `reports` folder. **Every run
writes the same report afterwards, about the same ticked products** — the ones that could not run
keep the reason given here, and the ones that ran are reported on what the run did. Your ticks,
including on products that could not run, are still there when you come back to this screen.

When you are done, press **Next** at the foot of the screen. It does two things: it saves your
choice of products and what the batch publishes, and it takes you on to step 2 — or, for a
links batch, straight to step 3. There is no separate save button.

**The line above the button says what it will do** — *"Next saves 8 of 9 ticked product(s) — 1
cannot run and is left out, see below — and goes on to the copy"* — and it updates as you tick. Read that before you press, not
after: it is the one thing that will tell you if you ticked the wrong way round. With nothing ticked
the button stays off. Pressing it takes you straight to Content.

**A yellow band above the table names any barcode that is on two rows of your list with different
products** — usually a typo. It says which rows, and which product the export says the barcode
belongs to. Correct the other row in your spreadsheet and upload the list again.

**Done looks like:** both files uploaded without a refusal, nothing unexpected in the first two
tables, and *selected* in Coverage showing exactly the number of products you mean.

**Stop if:** an upload is refused, or a red band says a file cannot be read. Upload it again,
or ask the maintainer for a fresh copy — do not go looking for the file in a folder to replace by
hand.

### Videos — and the sheet the client sends back

A product without a confirmed video in a language is **still published** — just without a video
there. The *Missing video(s)* table and the Video column say which, and after every live run the
tool writes a short data-quality note listing the pages that went live without one. When the client
confirms the video, the next run adds it to the page that is already live.

One case *does* hold a product back: **two videos confirmed for the same language**. The tool will
not pick one for the client, so the product sits in *Not eligible* until one is marked *skip*.

Two kinds of waiting look the same in a count and are different jobs. *no confirmed video in fr*
can mean a French video exists and nobody has said which product it is — or that no French video
has been made yet.
The complete data-quality report on the Runs screen lists both sides, so you and the client can
tell them apart.

**Importing the client's sheet.** You do not have to type barcodes in. When the client sends back
the spreadsheet with the barcodes filled in, upload it in **step 3**.

<img src="images/data-videos.png" alt="After a sign-off sheet was uploaded in step 3: under the three uploads, three boxes saying which column is the language, the filename and the barcode; figures for how many videos change, two-video clashes, rejected, already set and left blank; a table of what the mapping would become; the two-video clash already in the mapping; the rejected rows; and an Apply button." width="760">

- It needs three columns: the language, the video filename, and the barcode. **You say which is
  which** — the tool reads the sheet's headings, fills the three boxes in with its best guess, and
  you change any it got wrong. So the columns can be called anything at all, and any other columns
  are ignored. The table can sit below a title row, on any sheet of the workbook.
- **Check those three boxes every time, even when they look right.** Reading the wrong column as
  the barcode is the one mistake here that would put the wrong video on a product.
- **Uploading changes nothing in the mapping.** It shows you what the sheet would do, row by row,
  and only then offers a button that does it. The sheet itself is kept, dated, so the data-quality
  report can always say what it still changes.
- **The sheet leads, barcode by barcode.** Every barcode the sheet names gets exactly the video
  the sheet gives it — even when the mapping said something else. A video that loses its last
  barcode that way becomes *skip*. A barcode the sheet does not name at all keeps its video, and a
  blank cell on its own changes nothing. Before you apply, *What the mapping would become* lists
  every video that changes, with what it says now and after.
- **One video can belong to several products.** Put their barcodes in one cell, separated by
  commas (`2987653005829, 2987653005830`). What a product can never have is two videos in one
  language: a sheet that would do that is refused as a whole, and the rows that cause it are
  listed. Give the product one video in the sheet and upload it again. (A product that *already*
  has two videos in the mapping, and still has both in the sheet, does not block the rest of the
  sheet — it simply stays held until the sheet names one of them.)
- **To take a video off every product, write *skip* in its barcode cell.**
- **A page that is already live keeps its video** if the sheet leaves its product with none — the
  tool can put a video on a page, not take one off. The screen names those products; remove the
  video in WordPress if it should go. A product whose video *changes* gets the new one on the next
  pages run.
- **Rejected** rows say why, and name the row number in the spreadsheet so you can point the
  client at it. The commonest one: a barcode that arrived as **8.7132E+12** because the column was
  formatted as a number. Ask for that column to be set to text and the file sent again — the digits
  are genuinely gone from the file, so there is nothing to recover at this end.
- **The figures match the rest of the app.** *Two-video clashes* counts products as they would be
  once the sheet is applied — the ones already in the mapping, minus those the sheet solves, plus
  any it would add — the same kind of number as *Not eligible* and the report, and lists the
  existing ones. *Left blank* says how many mapping rows still have no
  barcode, the figure the preflight reports.
- Dropped a GS1 export in this slot by mistake? It is refused, by name. Nothing is kept.

**Changing the mapping yourself.** The app does not edit the mapping row by row. When something
needs changing by hand — a conflict, a product with two videos in one language, a new video file —
open the mapping file, `videos/mapping.yml` in the client's input folder, in a plain text editor
(Notepad on Windows, TextEdit in plain-text mode on macOS, or any code editor — never Word). Each line is one video file and the barcode it
belongs to:

```
  - {file: "Damp Mat v2.mp4", gtin: "02987653006864"}
  - {file: "Damp Power.mpg", gtin: ""}
```

Fill in the barcode between the quotes, or write `skip` for a video that belongs to no product.
Leave the rest of the line as it is. Save, then reload the Data screen: the *Missing video(s)* table,
the Video column and the report all count from the file as it is now. Your ticks are untouched.

---

## Step 2 — Content

<img src="images/content.png" alt="The Content screen (an earlier version — it now has three numbered steps: check the live site, generate content, review the text)." width="900">

**What this screen is for:** the tagline and the Eigenschappen text that go on each page. They are
written by Claude, not taken from the spreadsheet. Everything else on the page — brand, size,
material, barcode — comes from the GS1 export.

The screen is three numbered steps, and each one appears only when the one before it has worked:
step 2 after the site has been checked, step 3 after the text is written.

**Writing text is optional — Next is always on.** If you go on without writing any, a page that is
already live keeps the tagline and Eigenschappen it has: only what changed (a title, a category, a
video) is updated. A product that is not live yet and has no text is left out of the run, and
Preflight names it.

1. **Check the live site.** Press **Check the live site**. It asks the website which products in
   your batch are missing the two texts, and answers with three numbers: *no live text · generate*
   (these get written in step 2), *no live text · skip* (the export has nothing to write from —
   that is fixed in MyGS1, not here) and *live text already · skip*. Nothing is written.
2. **Generate content.** Press **Generate content for N product(s)**. N should be the first number
   from step 1. The text is written in every language and replaces any earlier text for those
   products. Pages already live with text are left alone unless you tick **Include
   already-published pages** (off by default), which writes new text for all of them. To pick
   just a few instead, open **Override** and tick those; nothing there is ticked unless you tick
   it. No button, but a
   yellow note about a key? This machine cannot write text — ask your maintainer.
3. **Review the text.** The text for this batch, per product, per language. **Read it.** This is
   the last comfortable moment to catch a sentence that is wrong; after Publish it is on the live
   site. Text from an earlier batch is never shown here. If you come back to this screen after
   generating, step 3 opens straight after the check — you do not have to generate again.

**A links batch skips this screen.** Next on Data takes it straight to Preflight; if you open
Content from the rail anyway, it says there is nothing to write and lets you go on.

**Done looks like:** every product in step 3 has text in every language, and no product is named
as "no text yet". Then press **Next**. Or, when only something other than the text changed — a
new title, say — press **Next** without generating: the live pages keep their text.

**Stop if:** step 3 names products with no text yet. They would be dropped from the batch silently.

---

## Step 3 — Preflight

<img src="images/preflight.png" alt="The Preflight screen: a summary of 7 passed and 1 warning, a Ready band, and a list of individual checks each marked ok, warn or FAIL." width="900">

**What this screen is for:** everything that can be checked before anything is written. It runs by
itself when you arrive — a turning wheel and a seconds counter show it working — and there are no
buttons to press. It checks the settings, this batch and its text, and then logs in to the website
and to GS1. All of it is read-only: nothing is written. Every visit runs it again from scratch.

Each line is one check, marked **ok**, **warn** or **FAIL**, with a sentence saying what it found
and — when it failed — what to do about it. The verdict at the top is the one to read first.

- **ok** — nothing to do.
- **warn** — read it. The run can proceed; something is less than ideal.
- **FAIL** — fix it before publishing. The remedy is printed under the check.
- **—** (not applicable) — the check does not concern this batch. For a **links** batch that is
  the text, the categories, the videos and ffmpeg: no page is written, so none of them matters.

**Next stays off until the checks pass with no FAIL**, because a wrong password found here costs you
nothing, and found at step 4 costs you a half-finished batch. Fixed something? Click **Preflight**
in the rail to run the checks again.

**Done looks like:** "Ready." — or "Ready, but read the warnings first", once you have read them.

**Stop if:** any check says FAIL.

---

## Step 4 — Publish

<img src="images/publish.png" alt="The Publish screen: a band saying this pages run is reversible, then the first gate — This batch publishes pages — with Confirm, Change on the Data screen and Cancel." width="900">

**What this screen is for:** doing it. Nothing is written until you have answered every required
question, and the screen will not let you skip one.

It works as a series of **gates** — one card each, in order, each stating what it is asking and
why. Answer one and the next slides into view. If you are unsure at any gate, **Cancel** is always
there and always safe: it stops the run without having written anything.

**The first gate shows what kind of run this is** — the choice you made on the Data screen — and
asks you to confirm it. To change it, press **Change on the Data screen**, choose again there and
press Next. The three are not equally reversible:

| Mode | What it writes | Undoable? |
|---|---|---|
| **pages** | WordPress pages only | Yes — a page can be edited or deleted |
| **links** | GS1 Digital Link records and QR codes, pointing at pages that already exist | **No** |
| **both** | pages first, then the records pointing at them | **The GS1 half: no** |

A band at the top of the screen tells you which of those you are in, all the way through. Grey
means reversible; **red means this run writes permanent records.**

**Products that already have an older page.** Many products have a page on the site that this tool
did not make, at an address of its own (`…/democlient/notenkraker-2/`). To point their GS1 record at
that page, put its address in the **Link naar site** column of the product selection list, and run
**links**. The QR code then leads to that page. If the page has a French version, the GS1
record stores that as its French link too, and a phone set to French opens it; if it has none, the
record gets the Dutch link only and every phone opens the Dutch page. The
tool refuses a product, and says why, rather than guess, when:

- the page you named does not load — check the address in the list;
- the tool already published its own page for that product at another address — clear one of
  the two;
- you run **pages** or **both** for a product whose cell is filled — that would make a second page.
  Clear the cell if the new page should replace the old one.

A **links** run needs no generated copy, so it skips the Content step. Leave the cell
empty for every product whose page this tool publishes.

**The gates you will meet, in order:**

1. **Intent confirmation** — the mode chosen on the Data screen, and nothing else. Read it before
   you press Confirm — it decides whether anything permanent is written.
2. **Language selection** — which languages this run covers.
3. **Generated copy review** — confirms you have read the text from step 2.
4. **Plan review** — the tool builds the plan and tells you how many rows it holds and what kind:
   *new* pages, *changed* pages, or unchanged. Choose **All**, **New only**, or **Review changed**
   to walk the changed ones and decide each individually — Apply or Skip on each row, or
   **Apply all** above the list and then Skip the few you want to leave out.
5. **Production environment confirmation** — only when the client is on production. You type the
   client id in full into a box before the Confirm button will accept it. Deliberate friction, in
   the one place it is worth it.
6. **Dry run** — runs the whole thing while writing nothing, and shows you the output, which stays
   on screen. **Read it**, then Proceed or Cancel.

Then **Write it**, and the run starts. A grey panel fills with the log as it goes, and the button
shows a spinner with the seconds counting up.

**When it finishes, a message stays on screen until you close it:** **Published** — how many pages,
with no errors, and for `links` or `both` how many GS1 records were written — or **Run failed**,
with the reason. The button is then gone: a walk runs for real once. To publish again, open
Publish from the rail and start a new walk.

**After a `links` or `both` run, the tool scans every QR code it just wrote** — once as a Dutch
phone, once as a French one — and checks each lands on its own page. GS1 takes about half a minute
to show a new record, so this can add a minute or two before the run finishes. The last lines of
the run's output say either *all scans land on their own page*, or which product and language
landed somewhere else. That is a warning, not a failure: the record was written. Scan that code
yourself in a few minutes; if it still opens the wrong page, ask for it to be looked into.

> **Your answers live on this screen only.** If you navigate to another screen part-way through the
> gates and come back, you start again from the first gate. Nothing is lost except your place, but
> it is worth knowing before you go and check something.

---

## After: Runs

<img src="images/runs.png" alt="The Runs screen: a Compare against the site button, and a card for one run showing 8 ok, 0 errors and 8 rows recorded." width="900">

**Runs** is the record of what actually happened — every row of every run, as it was recorded at
the time, newest first. Each card gives you how many rows succeeded, how many failed, and an
expandable list of every one.

A run that stopped half-way shows as a partial run, and says so. That is the case worth looking at:
some pages may be live and some records may already exist for the rows that landed.

**Does the site match the ledger?** at the top asks the website what is actually there and compares
it against this machine's record, in both directions. It only reads; nothing is written. Use it if
you are ever unsure whether a page exists.

**Complete data-quality report**, under it, is the follow-up list for **every product on your
product list and everything that is live** — not just the last batch. Press **Build the report**.
It opens with *What to do, by who* (the client in MyGS1, the client's videos, you, the site's
maintainer), then *What this tool has published* — how many products are live, and per language
how many pages and GS1 links, plus how many GS1 records and QR codes — then every product with where it stands — live (each page checked on the website for
its video and text), ready to publish, held and why, or not in the export — then what is wrong with
the product list itself, then the usual sections. It takes a while, writes nothing to the site, and
is saved as `complete-data-quality-report.md`, ready to forward to the client.

**Every run also writes the client's issue report** — the same one-to-two pager as on the Data
screen, about the same ticked products, with what happened in the run added: anything that failed
while publishing, or was held, counts as failed too. **Download PDF** and **Download Excel** are on
the run's card.

**To send the result back to whoever asked for the batch:** press **Build the result sheet** on
the run's card. It writes your scope list again, beside the run log, with what happened to each
row added on the right — one line per product, the page address where there is one, and a plain
reason where there is not. A third tab explains every word in it, so the file can be forwarded on
its own.

Two things it will say that are not failures. **`held`** means the run deliberately left a product
alone — usually no confirmed video, or a mandatory field left blank in MyGS1 — and the reason is in
the row. **`not in export`** means the barcode is on your list and the export has no product for
it. Both are work; neither is something that went wrong during the run.

**One housekeeping job after every batch:** send `output/{client}/state.json` back to the
maintainer. It is the record of everything published, it only exists on the machine that did the
publishing, and two out-of-step copies is how a product gets published twice.

---

## Setup — when you would go there

<img src="images/setup.png" alt="The Setup screen showing jump links and the Client, WordPress and GS1 sections of the configuration form." width="900">

Rarely. **Setup** is the site's settings — the website address, the login, which GS1 environment,
where the files live. It is configured once, by the maintainer.

Three reasons an operator opens it:

- **The work is moving to or from another machine.** *Hand over to another machine* has two
  buttons. **Make a handover pack** puts the settings, the batch files and the record of what is
  published into one zip in your Downloads folder; tick *Include the passwords* only for someone
  setting up for the first time, and send that zip privately. **Receive a handover pack** takes a
  zip someone sent you: it shows what it would change, and writes nothing until you press
  **Receive this pack**. Then close the app and start it again. Only the machine that last
  received the pack publishes — it refuses a pack if this machine has published something the
  pack does not know about. Video files are never in a pack; copy those folders across yourself.
- **A password changed.** The *Credentials* section takes the new one. It never shows you an
  existing value, only whether one is set.
- **Something is failing and you want to test one thing.** The *Test* section runs the same checks
  as Preflight, but only the two or three that concern the setting you just changed.

The row of links under the title jumps to a section. The block at the bottom, **Fixed settings —
not editable here**, is folded shut because nothing in it can be changed on this screen: how pages
are filled from the export, which website category each product group goes into, and the switch
that keeps a product without its description off the site. They are part of the settings, so a
handover pack carries them like everything else; each has the reason it is fixed written beside it.

**Everything on this screen has a consequence written under it.** Read that line before changing a
field. Switching the GS1 environment to production makes you type the client's name in full first.

---

## When something goes wrong

Work down this list. Anything not on it: take a screenshot, copy the grey command line from the
screen, and send both to the maintainer.

**A red band saying `clients.yml did not load`.**
The settings file is missing or broken. The band has a link to Setup. If Setup also shows red, the
file has not been installed — this is one for the maintainer.

**Preflight says FAIL.**
Read the indented sentence under the check. It is the instruction. Most failures are one of: the
spreadsheet has not been parsed yet (go to Data, press Parse), the copy is missing or stale (go to
Content), or a credential is wrong (Setup → Credentials).

**A check says a file is missing.**
Five files are not in the download and arrive from the maintainer separately. The list is in
[`operator-install.md`](operator-install.md#3-the-five-files-that-are-not-in-the-download). The
important one is `state.json`, the ledger — without it the tool believes nothing has ever been
published.

**The plan has fewer rows than you expected.**
Normal, and usually right. Products are dropped when they are not on the scope list, when they
are already published and unchanged, when they have no confirmed video in every language, or when
mandatory data is missing. The plan gate tells you how many were dropped and why.

**The run finished with errors.**
Go to **Runs**, open the newest run, expand *Every row*. Each failed row carries its own error.
Some rows will have succeeded — those pages are live. Re-running is safe for pages: they are
matched and updated in place, not duplicated.

**You think something was published twice.**
Stop, and do not run again. Use *Does the site match the ledger?* on the Runs screen, and tell the
maintainer what it says.

**It is slow and nothing seems to be happening.**
Look at the button you pressed: if it shows a spinner and a rising number of seconds, it is
working. A twenty-row run is quiet for about ninety seconds by design. Do not press it again.

For anything deeper, [`troubleshooting.md`](troubleshooting.md) has the technical detail — it is
written for a developer, but the error messages in it are the ones you will have seen.

---

## Glossary

Words this tool uses that mean something specific.

| Word | What it means here |
|---|---|
| **GTIN** | The number under a barcode. The tool's identifier for a product — 14 digits. |
| **GS1 Digital Link** | The standard that makes a barcode lead somewhere. A record at GS1 says "this GTIN → this page". |
| **Resolver** | GS1's service that does the leading. Writing to it is what "permanent" refers to. |
| **Batch** (or *wave*) | One pass through Data → Content → Preflight → Publish. |
| **Scope** | Which products a run may touch — after the scope list and the video rule have cut the spreadsheet down. |
| **GS1 Data Source export** | The product spreadsheet from GS1. Data, not scope. Uploaded on Data. |
| **Product scope list** | The separate spreadsheet listing which barcodes this batch covers. Scope, not data. Uploaded on Data, above the tables. Called `selections.xlsx` on disk, under `process/selection/`, and `process_list` in the settings file — same thing. |
| **Result sheet** | Your scope list handed back after a run, with what happened to each row. Built on **Runs**, beside the run it describes. |
| **Plan** | What the run worked out it would do, row by row, before doing any of it. One row per product per language. |
| **Row** | One product in one language. Two languages means two rows for the same product. |
| **New / Changed / Unchanged** | How the plan classifies a row against what is already published. Unchanged rows are not touched. |
| **Held** | A product the plan refuses to publish because something mandatory is missing. It is not an error; it is a decision. |
| **Ledger** (`state.json`) | The record of everything this tool has published. Lives on the machine that did the publishing. |
| **Dry run** | Doing the whole run with the writing switched off, to see what it would do. |
| **Gate** | One question on the Publish screen that must be answered before the run continues. |
| **Preflight** | The list of checks, and the screen that shows them. |
| **ACF / WPML** | WordPress plugins — the one that holds the custom fields, and the one that handles languages. You will only see these named in error messages. |
| **Eigenschappen** | The bulleted feature text on a product page. Dutch for "properties"; it is what the field is called on the site. |

---

*Next: [`operator-install.md`](operator-install.md) for installing and for the files that arrive by
hand · [`troubleshooting.md`](troubleshooting.md) when an error needs chasing down.*
