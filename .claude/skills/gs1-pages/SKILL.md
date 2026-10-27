---
name: gs1-pages
description: "Publish a client's WordPress product pages only, writing no GS1 Digital Link records. Invoked as /gs1-pages. This is the pages-only mode of flow-orchestrator, with the mode already fixed — it is the reversible flow: nothing permanent is created."
---

# /gs1-pages — WordPress pages only

This is [`flow-orchestrator`](../flow-orchestrator/SKILL.md) with the mode pinned to **`pages`**.

**Load `.claude/skills/flow-orchestrator/SKILL.md` and follow every step in it**, with three things
already settled by the command:

- The **mode is `pages`** — render and upsert the WordPress pages, link them as translations, and
  stop. No Digital Link record is written and no QR is rendered. State it at gate 0 as fixed by the
  command rather than derived from the phrasing; the operator still confirms the gate.
- **Gate 0 stands in for step 8.** Nothing irreversible happens, so the separate production
  environment confirmation is skipped — gate 0 has already named the environment.
- Step 9 appends **`--only pages`**.

Everything else — the export cross-check, the copy review, the plan gate, the post-run summary —
happens exactly as written there. Nothing about the gates is restated here on purpose: one copy, one
place to change.

## Two things to tell the operator afterwards

- **The pages are live but not resolvable.** No QR points at them yet. Run `/gs1-links` when the
  pages are verified to finish the publish.
- **The rows will keep planning as CHANGED** until that happens. That is deliberate — a page
  published without its resolver link is not finished, and the plan says so. The row's diff carries
  `gs1_link`, not a content change.

**Still gated against production.** `--only pages` writes to a live WordPress site, so
`run_execute` still refuses a production run without `--i-understand-production`, which you append
on the strength of gate 0.
