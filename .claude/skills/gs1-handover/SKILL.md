---
name: gs1-handover
description: "Hand the work for a client — settings, batch files and the ledger (state.json) — from one machine to another, in either direction: make a handover pack, or receive one. Invoked as /gs1-handover. Use it for any phrasing of moving the setup or the publishing to or from someone else: \"make a handover pack\", \"handover\", \"hand it over\", \"package everything for them\", \"transfer the setup\", \"send them the ledger\", \"give the work back\", \"I got a pack/zip from …\", \"receive/unpack/import the pack\", \"take over from …\", \"overdracht\", \"overdrachtspakket\", \"geef het door\", \"neem het over\". Never copy state.json by hand instead."
---

# /gs1-handover — moving the work, and the ledger, between machines

When more than one person publishes for a client, they publish, **never at the same time**. Whichever machine publishes owns
`output/{client}/state.json`, the ledger of permanent GS1 writes. The work moves with a **handover
pack** (`lib/handover.py`), and with nothing else: never by copying, editing or merging
`state.json`. A machine that does not hold the ledger does not publish.

The same two actions are buttons for the operator: **Setup → Hand over to another machine →
Make a handover pack / Receive a handover pack**. If the person would rather click, say so and
stop there. This file is for doing it from Claude Code, and for the checks around it, which the
buttons do not ask.

**Run everything in the data folder.** That is `$GS1_DATA_DIR` when it is set, otherwise the
repository itself (the maintainer's clone). If `GS1_DATA_DIR` is unset and the repository has no
`clients.yml`, there is no data folder here: stop and say so. Do not create one. The interpreter is
`.venv/bin/python`, or `.venv/Scripts/python` on Windows (Git Bash).

## Step 0 — which direction

Ask if it is not clear from what was said:

- **Making a pack**: this machine has been working and the other one takes over (a first handover
  to someone new, or handing the publishing back).
- **Receiving a pack**: a `handover-{client}-….zip` arrived here.

## Making a pack

1. **The ledger has to be current.** Ask whether a run is in progress anywhere. If one is, wait for
   it. Then compare the ledger with the live site:
   `python -m scripts.reconcile` (read-only, GET only). Exit 0 means they agree. Exit 1 lists
   divergences: **stop** and show them. A stale or divergent ledger is the most expensive thing
   that can travel. Settle it first (see `docs/verifying-live.md`), never by editing the file.
2. **Passwords: ask, never assume.** Include `.env` (`--with-credentials`) **only** for a first
   handover to someone setting up a new machine. After that, each person keeps their own: a pack
   with passwords would overwrite the receiver's. If the answer is yes, say plainly that the zip
   will hold production passwords in plain text.
3. **Make it, outside the data folder**, so a zip with passwords is not left lying in it:
   `python -m scripts.handover pack [--with-credentials] --out ~/Downloads/handover-{client}-{UTC stamp}.zip`
   (Windows: `%USERPROFILE%\Downloads\…`). The last stdout line is the path.
4. **Report** what it printed: the path, the file count, the size, the ledger's last write, and
   whether `.env` is in it. Then say these four things, every time:
   - **Send it privately**, never by email when it holds passwords, and delete it once received.
   - **Video files are not in it.** Copy the `NL`/`FR` video folders separately: all of them on a
     first handover, only new files after that.
   - **Stop publishing from this machine** until a pack comes back.
   - **Write down who holds the ledger now**, wherever the engagement is tracked.

## Receiving a pack

1. **Find the zip** (usually in Downloads) and **dry-run first**:
   `python -m scripts.handover unpack <zip> --dry-run`
   Show what it reports: where it came from, when, the ledger's last write, whether it holds
   passwords, and the files it would write. **Wait for an explicit go-ahead** before going on.
2. **Apply it**: the same command without `--dry-run`. It keeps every replaced file in
   `input/{client}/superseded/handover-{stamp}/` and restricts `.env` to its owner (on Windows
   too).
3. **If it refuses**, the ledger here records a publish the pack does not know about, or a newer
   version of one. Both machines published since the last handover. **Stop:**
   - no forcing, no copying `state.json` across, no merging;
   - tell both people to stop publishing;
   - the way out is a pack made on *this* machine sent the other way, or reconciling both ledgers
     against the live site (`python -m scripts.reconcile` on each).
4. **After it is applied**, say:
   - restart the app if it is open (it shows the client and environment it now has);
   - copy any new video files into `input/{client}/videos/NL` and `/FR` in the data folder;
   - run `python -m scripts.reconcile`, or Runs → *Does the site match the ledger?*, before the
     first publish from here;
   - delete the zip, and empty the bin, if it held passwords.

## Never

- Never copy, edit or merge `state.json` by hand, in either direction. That bypasses the one check
  that stops a permanent GS1 record being written twice.
- Never pack or unpack while a run is going on, on either machine.
- Never include `.env` by default, and never send a pack holding passwords by email.
- Never publish from the machine that just packed until a pack comes back to it.
