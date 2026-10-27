---
name: gs1-links
description: "Write a client's GS1 Digital Link resolver records only, pointing them at product pages that already exist, and render the QR. Invoked as /gs1-links. This is the links-only mode of flow-orchestrator, with the mode already fixed — it writes PERMANENT GS1 records and touches no WordPress page."
---

# /gs1-links — Digital Links only

This is [`flow-orchestrator`](../flow-orchestrator/SKILL.md) with the mode pinned to **`links`**.

**Load `.claude/skills/flow-orchestrator/SKILL.md` and follow every step in it**, with two things
already settled by the command:

- The **mode is `links`** — build one Digital Link record per GTIN pointing at pages that already
  exist, and render the QR. No page is created, updated, or linked as a translation. State it at
  gate 0 as fixed by the command rather than derived from the phrasing; the operator still confirms
  the gate.
- Step 9 appends **`--only links`**.

Everything else — the export cross-check, the plan gate (built with `run_plan --links-only`), the
production environment confirmation, the post-run summary — happens exactly as written there. The
copy review (step 3) does not apply in this mode. Nothing about
the gates is restated here on purpose: one copy, one place to change.

## The risk this mode carries, and what handles it

The targets do not come from a page this run just created and verified. When the plan row carries a
`listed_url` (the process list's `target_url_column`, e.g. `Link naar site`), that page is the
default language's target and the record's default link, so every scan resolves to it. Every other
confirmed language links the translation the page itself names in its hreflang — and gets no link
when it names none (the GTIN is not refused for that). Refused if this
tool already has its own, different page for the GTIN. Otherwise `run_execute` resolves each target from `state.json`, else a slug lookup on the
site, else the plan row's `target_url` (built from `wordpress.target_url_pattern`). Either way it
**HEADs every one of them before writing anything**. A GTIN with any target that does not serve
gets no GS1 write at all.

That check is in the script, not in this file, precisely because instructions can be skipped and a
GS1 record can never be deleted: a permanent QR aimed at a 404 is not recoverable.

So when step 11 reports `refusing to point a permanent GS1 record at it`, do not work around it.
The page is not where the plan says it is — the slug may not match `slug_pattern`, or the page may
be drafted or gone. For an older page with its own slug, put its address in the selection list's
`Link naar site` column; otherwise fix `wordpress.target_url_pattern` or publish the page, then
re-run. The rest of
the batch already went through.

**A dry run cannot verify.** It builds no clients, so it lists the targets it would use and says the
real run checks them. Do not read a clean dry run as proof the targets serve.

Reversible alternative: `/gs1-pages` writes the pages and stops. `/gs1-publish` does both legs.
