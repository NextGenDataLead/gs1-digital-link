---
name: gs1-publish
description: "Run the full GS1 publish flow for a client: WordPress product pages first, then Digital Link resolver records pointing at them, and the QR. Invoked as /gs1-publish. This is the both-legs mode of flow-orchestrator, with the mode already fixed — it writes PERMANENT GS1 records."
---

# /gs1-publish — pages and Digital Links

This is [`flow-orchestrator`](../flow-orchestrator/SKILL.md) with the mode pinned to **`both`**.

**Load `.claude/skills/flow-orchestrator/SKILL.md` and follow every step in it**, with two things
already settled by the command:

- The **mode is `both`** — WordPress pages first, then one Digital Link record per GTIN pointing at
  those pages, then the QR. State it at gate 0 as fixed by the command rather than derived from the
  phrasing; the operator still confirms the gate.
- Step 9 invokes `run_execute` with **no `--only` flag**, which is what `both` means.

Everything else — the export cross-check, the copy review, the plan gate, the production
environment confirmation, the post-run summary — happens exactly as written there. Nothing about
the gates is restated here on purpose: one copy, one place to change.

**This mode writes permanent records.** A GS1 Digital Link can never be deleted; retraction only
disables it. Gate 0 carries that warning and step 8's production confirmation is mandatory.

Reversible alternative: `/gs1-pages` writes the pages and stops. `/gs1-links` writes only the
resolver records, for pages that already exist.
