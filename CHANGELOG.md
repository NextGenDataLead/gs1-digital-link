# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.1] - 2026-11-11

### Fixed
- **A dropped connection no longer stops *Check the live site*.** A server that closes a kept-alive
  connection answers `Server disconnected without sending a response`; a read is now retried like
  any other network error. A write is not repeated: the page may already have been created.
- **The Content review no longer shows a live Dutch page as "no copy" under `translate_copy`.**
  That page is translated, not rewritten; it now says *keeps the live text*.

## [0.6.0] - 2026-11-10

### Added
- **French copy can be the Dutch copy, translated: `generator.translate_copy`.** On, the copy is
  written in the default language only, and every other language's tagline and Eigenschappen are
  its translation, line for line — so the pages say the same thing. Another language's GS1 text
  then rewrites nothing; it is still compared in §4e, and §4c says why a short one no longer
  shortens a page. Each page records what its copy was written from (`copy_basis` in the ledger):
  a change to the default language's text rewrites every language, nothing else does. Turning it
  on makes each live page in another language due one translation of the default-language page
  **as it is live** (read with GETs), and leaves the default-language pages as they are. A
  translation that fails, or comes back with a different number of lines, is named
  (`NOT translated …`), leaves that page alone and is asked for again next run. API backend only:
  `--emit`/`--validate` refuse. Off by default — nothing changes until a client sets it.
  A live default-language page from before the setting is never rewritten by it, not even when
  ticked under *Include already-published pages* — that is how the shell asks for the switch —
  and the Content screen lists the pages it did not translate. **Every machine that publishes
  needs this release before the setting is used:** an older one drops `copy_basis` from the
  ledger, and every translated page is then due again.
- **The data-quality report compares the languages (§4e).** Every attribute the client fills in per
  language in MyGS1 — the title (3309/3297), the product variation (3332), the marketing text
  (1083), the feature/benefit lines (1067), the functional name (3301) and the description (3318) —
  is laid side by side, one column per language. A value **missing** in one language is read from
  the export. **Says something else** and **another language** are judged by Claude through the API
  backend and kept in `language_check.json`; a product is judged again only when its values
  change, and a judgment about values the export no longer holds is never shown. Generating copy
  with `--backend api` (the shell's Generate button) brings it up to date; `python -m
  scripts.check_languages` runs it alone, and `--all` also judges live products that are no longer
  on the product list. A failed comparison never fails the copy already written, and judgments
  already made are kept when a run stops partway. On the pilot batch it found a French 1083 about human
  hair on a pet brush, "désherbant" (weed killer) as the French name of three weed brushes, and
  English or Dutch in eight French slots.

## [0.5.5] - 2026-11-05

### Added
- **Every scan can go to the Dutch page: `gs1.link_languages: default_only`.** With a Dutch and a
  French link in a record, GS1 makes both a per-language default, and id.gs1.org then shows a
  phone set to any third language — English in Belgium or the Philippines — a "GS1 Digital Link
  linkset" page to choose from instead of a product page. `default_only` writes the default
  language's link alone, so every phone opens that page, French ones included. It is the
  **Resolver links** dropdown in the GS1 section of Setup; `all`, the default, keeps one link per
  language as before. Switching it marks every live product's record *GS1 record out of date*
  until a `links` run rewrites it, and that run checks only the pages it links. The QR codes do
  not change. Removing the French link by hand from one record (08713195001593) is what showed it
  works.

## [0.5.4] - 2026-11-04

### Added
- **A field can fall back to a second attribute: `fallback_attribute`.** A source in `gdsn_map`
  or `gdsn_extras` may name a second attribute on the same sheet, read only where the first is
  empty in every target market — per language, so the preferred attribute wins wherever it is
  filled. Made for the page title: read it from 3309 *Commerciële benaming* (the web name) and
  keep 3297 for the products whose 3309 is not filled yet, instead of failing the parse for them.
  Set it in `clients.yml` as `attribute: "3309", fallback_attribute: "3297"`.
- **The data-quality report lists every value taken from a fallback** (new §4d, plus a summary
  row), with the value the page shows now and the attribute to fill in MyGS1. Nothing there holds
  a product. The coverage matrix heads such a column with both numbers (`product·3309/3297`), and
  a held product's missing-data line names both attributes. The complete report's
  *What to do, by who* counts them as a job for the client in MyGS1.
- **The parse refuses a value that is spreadsheet formula text.** An upload into MyGS1 had stored
  `INDEX([1]TradeItemDescription!$F:$F, MATCH(…))` in front of every new 3309 name, and the export
  carried it as plain text that would have headed 49 live pages. Any value the tool would publish
  that starts with `=`, carries a sheet reference, is an Excel error value such as `#VALUE!`, or
  calls a lookup function such as `INDEX(` now stops the parse, naming the product, the field and
  the attribute, with the remedy: re-upload the values only in MyGS1. Checked against the full
  2026-10-26 catalogue (no false alarm) and the faulty export (all 98 values refused).

## [0.5.3] - 2026-10-28

### Fixed
- **A product selection list without the *Link naar site* column is accepted.** The Data screen
  offers that column as optional, but when `clients.yml` names it, the run refused a list that
  did not have it ("has no 'Link naar site' column"). A list without it now reads like one where
  every link is blank: in a links run, each product points at the page this tool published.

## [0.5.2] - 2026-10-28

### Added
- **New videos are added to the mapping from the Data screen.** A video copied into its folder
  after the mapping was made had no row in `mapping.yml`, so the sign-off sheet's barcode for it
  was rejected and the only fix was editing that file by hand. A button now adds
  a blank row for each rejected video that is really in its folder (a filename the folder does
  not have is left rejected: it is a typo, or a video not copied in yet). It sits at the top of the
  sheet's review, under the column choice, because every figure below it changes once those rows
  exist. The sheet is then read again, and *Apply this sheet* writes its barcodes as before.

### Fixed
- **A barcode typed with a mark in front to keep Excel from reading it as a number** (a backtick,
  an apostrophe another program kept, a curly quote) is read as the barcode in the sign-off sheet.
  It was rejected as "not a barcode". Marks inside the digits are still refused.
- Rows added to the mapping take the indent of the rows above them, so a mapping written flush
  left stays valid.
- The warning that a sheet would leave products with no video now shows in its warning style.

## [0.5.1] - 2026-10-28

### Changed
- **Preflight fails when a client with published pages has no page templates of its own.** The
  *Prompt and templates* line used to say "the built-in page templates apply" and move on, so a
  release installed before the client's templates reached the data folder would rebuild its live
  pages in the default layout without a word. It now stops the run and says where the templates
  belong. A client that really uses the built-in layout copies `templates/_default` there.

## [0.5.0] - 2026-10-26

### Changed
- **A client's prompt and page templates live in the data folder, and travel in the handover
  pack.** The voice prompt is read from `input/{client}/prompts/generation.{prompt_version}.md`
  and templates from `input/{client}/templates/`, before the program folder's
  `prompts/{client}/` and `templates/{client}/`, which stay as the fallback, so nothing changes
  for an installation that has not moved them. The installer copies them into the data folder
  where it has none yet (never overwriting), and Preflight's new *Prompt and templates* line says
  which copy is in use, warning while it is only the program folder's. A release that ships no
  client files, such as a public one, can then run with a client's own setup received in a pack.

### Added
- **An explainer video** in `docs/videos/`: publishing a batch up to the dry run (6½ min).
  Captioned, with English subtitles beside it, and linked from the docs index and the operator
  guide.
- **A voice prompt for the demo client** (`prompts/democlient/generation.v1.md`), so the example
  config's generator works out of the box and a new client's prompt has a model to start from.

## Before 0.5.0

Versions 0.0.1 to 0.4.3 were built in a private repository, against a live pilot: a Dutch
manufacturer's WordPress site in Dutch and French, and GS1 Netherlands' production resolver. This
repository starts at 0.5.0, so those releases and their history are not here. By 0.5.0 the tool
had:

- **Reading the data:** a GS1 Data Source export, either the GDSN datapool workbook or a flat
  article list, mapped to page fields in `clients.yml`; and a data-quality report (PDF and Excel)
  written for the client, listing what to fix at the source.
- **A plan before every write:** each product and language is classified (new, changed,
  unchanged, held) against a ledger of what has been published, and every run starts as a dry
  run. The ledger can be reconciled against the live site.
- **WordPress pages** per language through the REST API, from ACF fields or HTML templates, with
  WPML translations, media upload and categories.
- **GS1 Digital Link records** through GS1 Netherlands' API (OAuth2), with a link per language,
  so a phone opens the page in its own language, and the QR codes to print (SVG and PNG).
- **Generated selling points**, optional: Claude writes them from the export with a client's
  frozen voice prompt, and the operator reviews them before anything publishes.
- **Three publish modes:** pages only (reversible), Digital Links only (permanent), or both. All
  three run behind the same confirmation gates, in Claude Code or in a desktop operator shell.
- **Product videos** matched to products per language, with a client sign-off sheet.
- **A handover pack** that moves a setup and its ledger to another machine, and refuses to
  replace a ledger that knows a write the pack does not.
- **Installers** for macOS and Windows that need no Python (double-click, from the committed
  `uv.lock`), and a container image.

[Unreleased]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.5.5...v0.6.0
[0.5.3]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/NextGenDataLead/gs1-digital-link/releases/tag/v0.5.0
