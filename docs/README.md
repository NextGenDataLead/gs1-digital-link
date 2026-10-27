# Documentation

Three different people read this folder, and the right first page is different for each.

## If you are the person publishing products

You have been sent a folder and you need to get products onto the website and their barcodes
registered. You do not need a terminal.

1. **[`operator-install.md`](operator-install.md)** — two double-clicks, plus the five files that
   are not in the download. Normally all five arrive together in one handover pack; read the
   `state.json` warning in it.
2. **[`operator-guide.md`](operator-guide.md)** — the walkthrough of one batch, screen by screen,
   with screenshots, a *when something goes wrong* section, and a glossary.

Those two are the whole set. Everything else here is written for someone else.

Rather watch first? A short video, with captions,
[publishes a batch](videos/publishing-a-batch.mp4) (6½ min, stopping at the dry run).


## If you are setting the tool up for a new client

1. [`setup.md`](setup.md) — install, verify, configure, run, onboard a client.
2. [`gs1-nl-onboarding.md`](gs1-nl-onboarding.md) — GS1 contracts, OAuth2 credentials, resolution,
   retraction.
3. [`wordpress-onboarding.md`](wordpress-onboarding.md) — application passwords, post types, slugs,
   ACF versus templates, WPML, media.
4. [`data-source-export-schema.md`](data-source-export-schema.md) — reading a GDSN or flat export,
   and mapping its columns.
5. [`template-variables.md`](template-variables.md) — `acf_map` and the page template's variables.
6. [`costs.md`](costs.md) — what running this costs, and the one variable cost.

## If you are working on the code

- [`troubleshooting.md`](troubleshooting.md) — every exception type, the HTTP outcomes, the E1–E23
  edge-case inventory, rollback, and the traps already paid for on a live site.
- [`ui-operator-shell.md`](ui-operator-shell.md) — why each screen of the desktop shell is built the
  way it is, and where its safety lives. **Not** the operator's guide; that is above.
- [`verifying-live.md`](verifying-live.md) — proving the pipeline still writes to production
  without degrading a live product.
- [`IMPLEMENTATION_SPEC.md`](IMPLEMENTATION_SPEC.md) — the *how*: types, module contracts, error
  handling, idempotency, script contracts, and the Definition of Done per phase (§12).
- [`PROJECT_HANDOVER.md`](PROJECT_HANDOVER.md) — the *why*: scope, architecture decisions, phase
  plan, risks, reference artifacts.
- [`architecture.svg`](architecture.svg) — the end-to-end diagram.

## Per client

[`clients/`](clients/) — notes per client. `{client}-live-log.md` is gitignored; [`clients/live-log.template.md`](clients/live-log.template.md)
is what it is copied from.

---

`videos/` holds the explainer video, with its English subtitles as an `.srt` beside it.
Like the screenshots, it was recorded against the throwaway `democlient`; nothing real was
written. The captions are burned into the picture, so they show in any player.

`images/` holds the screenshots embedded in the operator guide. They are captured against a
throwaway client, never a real one — the recipe is in
[`ui-operator-shell.md`](ui-operator-shell.md#regenerating-the-screenshots).
