# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/NextGenDataLead/gs1-digital-link/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/NextGenDataLead/gs1-digital-link/releases/tag/v0.5.0
