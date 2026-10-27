/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * The operator gates, rendered from `lib/gates.py` by `python -m scripts.export_gates`.
 * `tests/lib/test_gates_export.py` fails when this file is stale, so the safety contract has one
 * source rather than one per language.
 *
 * Consumers: `src/gate.ts` in this package, which puts a write tool behind the named gate.
 */

export interface GateOption {
  value: string;
  label: string;
  consequence: string;
  outcome: "advances" | "stops" | "redisplays";
}

export interface Gate {
  id: string;
  step: string;
  title: string;
  purpose: string;
  required: boolean;
  modes: string[];
  needsProduction: boolean;
  options: GateOption[];
}

export const GATES: readonly Gate[] = [
  {
    "id": "intent",
    "step": "0",
    "title": "Intent confirmation",
    "purpose": "States the mode, cross-checks the configured export file against the one the operator has in mind, gives **how many products this run could touch** and the environment, and — for anything that writes to GS1 — warns that the records are permanent. The export cross-check catches the likeliest real error: a fresh export dropped somewhere new while `export.path` still points at the old one, which nothing downstream notices. The scope figure leads and the catalogue total follows it: this gate used to give only the catalogue size, which read 127 on a run scoped to one product — and gate 0 is where the operator forms their picture of what they are about to do. Neither number is the row count; that arrives at step 5.",
    "required": true,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "confirm",
        "label": "Confirm",
        "consequence": "Proceed with this mode",
        "outcome": "advances"
      },
      {
        "value": "change-mode",
        "label": "Change mode",
        "consequence": "Re-present with a different mode",
        "outcome": "redisplays"
      },
      {
        "value": "cancel",
        "label": "Cancel",
        "consequence": "Abort; nothing runs",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "languages",
    "step": "2",
    "title": "Language selection",
    "purpose": "Which of the client's configured languages this run covers. Intersected with the confirmed rows at step 6.",
    "required": false,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": []
  },
  {
    "id": "content_review",
    "step": "3",
    "title": "Generated copy review (gate 1 of 2)",
    "purpose": "The tagline and Eigenschappen are LLM-written, so they are read before they can reach a page. Review the copy against the real product, not the 'validated N' count — this pipeline fails silently. Show the coverage counts here too: a NEW or CHANGED unit with no copy for this version of the export is dropped from the plan entirely (E21), so a missing or stale results file yields an empty plan and a run that reports success having published nothing. The batch is this run's rows, not every unit in scope — an already-live, unchanged unit is not generated for.",
    "required": true,
    "modes": [
      "both",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "confirm",
        "label": "Copy is good",
        "consequence": "Proceed to planning",
        "outcome": "advances"
      },
      {
        "value": "regenerate",
        "label": "Regenerate",
        "consequence": "Write the copy again before planning",
        "outcome": "redisplays"
      },
      {
        "value": "cancel",
        "label": "Cancel",
        "consequence": "Abort; nothing runs",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "missing_field",
    "step": "4",
    "title": "Missing-field prompt",
    "purpose": "The units `run_plan` dropped because the product carries no `product_name` in that language (E18), named one by one. **This gate appears only when a unit was actually dropped.** It used to appear on every run, asking whether to skip a unit it could not name, on runs where nothing had been skipped — and of its answers only *stop the run* did anything, so the one live control on a question about nothing was the destructive one. A gate that asks about nothing teaches answering without reading, which is the habit this flow cannot afford. Nothing here can supply the missing name: it is filled in MyGS1 and re-exported.",
    "required": false,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "skip-row",
        "label": "Skip this unit",
        "consequence": "Other languages proceed",
        "outcome": "advances"
      },
      {
        "value": "ask-me-later",
        "label": "Ask me later",
        "consequence": "Batch the prompts, present at the end",
        "outcome": "advances"
      },
      {
        "value": "fail-run",
        "label": "Stop the run",
        "consequence": "Abort before execute",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "plan_review",
    "step": "5",
    "title": "Plan review (gate 2 of 2)",
    "purpose": "The last look before anything is written. Show the counts, the gate exclusions, and the units dropped before classification — an operator reading 'New: 0' alone concludes there is nothing to do, when in fact there is copy to generate. When prior state was reset from a corrupt file (E19), that warning goes **above** the counts: the counts alone read as a routine first run, and confirming would rewrite every live page.",
    "required": true,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "all",
        "label": "All",
        "consequence": "Confirm every NEW and CHANGED row",
        "outcome": "advances"
      },
      {
        "value": "new-only",
        "label": "New only",
        "consequence": "Confirm NEW rows; skip CHANGED",
        "outcome": "advances"
      },
      {
        "value": "changed-review",
        "label": "Review changed",
        "consequence": "Walk each CHANGED row's diff",
        "outcome": "advances"
      },
      {
        "value": "cancel",
        "label": "Cancel",
        "consequence": "Abort; nothing is written",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "row_diff",
    "step": "6",
    "title": "Per-row diff",
    "purpose": "Only on `changed-review`. **Every CHANGED row is walked, not only the ones carrying a diff.** State records the prior `title` and `wp_url` and nothing else, so a row whose change is in the product body has no diff at all — on the pilot plan that was 19 of 20 CHANGED rows, and keying the walk on the diff showed one row while confirming twenty. Show the fields the diff actually has and never invent an old value; an empty diff reads `Changes: product content (no title or URL change)`, and a `gs1_link` key reads `Changes: resolver link not written yet` — the page is published, its resolver link was never written, and nothing about the page is changing. `apply`/`skip` are per-row on **both** surfaces: the chat flow prompts row by row, the shell puts the pair on every row and holds the answers for the run. A row left undecided is not confirmed.",
    "required": false,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "apply",
        "label": "Apply",
        "consequence": "Include this row in the run",
        "outcome": "advances"
      },
      {
        "value": "skip",
        "label": "Skip",
        "consequence": "Leave this row unchanged",
        "outcome": "advances"
      },
      {
        "value": "show-full-diff",
        "label": "Show full diff",
        "consequence": "Show every changed row",
        "outcome": "redisplays"
      }
    ]
  },
  {
    "id": "production",
    "step": "8",
    "title": "Production environment confirmation",
    "purpose": "Mandatory, non-overridable, and enforced per run rather than per session. Skipped in `pages` mode only because gate 0 has already named the environment and nothing irreversible follows — a second production prompt for a page you can delete trains the operator to click through them.",
    "required": true,
    "modes": [
      "both",
      "links"
    ],
    "needsProduction": true,
    "options": [
      {
        "value": "confirm",
        "label": "Confirm",
        "consequence": "Execute against production",
        "outcome": "advances"
      },
      {
        "value": "switch-to-test",
        "label": "Switch to test",
        "consequence": "Re-resolve to the test environment",
        "outcome": "stops"
      },
      {
        "value": "cancel",
        "label": "Cancel",
        "consequence": "Abort; nothing is written",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "dry_run",
    "step": "8.5",
    "title": "Dry run",
    "purpose": "The same command with `--dry-run` and every other flag identical. Catches a plan pointing at the wrong rows, the wrong leg or the wrong URLs while it still costs nothing. Two things it cannot catch, so do not read a clean dry run as more than it is: in `links` mode it does not verify that targets serve, and it never proves the ACF fields will land.",
    "required": true,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "proceed",
        "label": "Proceed",
        "consequence": "Run it for real",
        "outcome": "advances"
      },
      {
        "value": "cancel",
        "label": "Cancel",
        "consequence": "Abort; nothing is written",
        "outcome": "stops"
      }
    ]
  },
  {
    "id": "post_run",
    "step": "11",
    "title": "Post-run summary",
    "purpose": "What actually ran, per row, with each error named. In `links` mode a refused GTIN means its target URL did not serve — read that as 'the page is not where the plan says it is', not as a GS1 fault. `detail` needs a model to read the log and explain it, so it exists in the chat flow only; the shell links to the Runs screen, where the same rows are rendered and the site can be reconciled against the ledger.",
    "required": false,
    "modes": [
      "both",
      "links",
      "pages"
    ],
    "needsProduction": false,
    "options": [
      {
        "value": "yes",
        "label": "Retry the failures",
        "consequence": "Re-run execute filtered to the failed GTINs",
        "outcome": "advances"
      },
      {
        "value": "no",
        "label": "Done",
        "consequence": "Finish",
        "outcome": "advances"
      }
    ]
  }
];

/** The gate with this id, or throw — an unknown id is a bug, not input. */
export function gateById(id: string): Gate {
  const gate = GATES.find((g) => g.id === id);
  if (gate === undefined) {
    throw new Error(`no such gate: ${id}`);
  }
  return gate;
}
