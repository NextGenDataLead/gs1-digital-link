"""Execute a confirmed run plan against WordPress and GS1 (IMPLEMENTATION_SPEC §8.3).

Usage:
    python -m scripts.run_execute CLIENT_ID (--plan PATH | --confirmed PATH)
                                 [--only {pages,links}] [--dry-run] [--revive]
                                 [--i-understand-production]

Work is grouped by GTIN and runs in two phases, because some of it is per language and
some of it is per *product*:

1. **Per confirmed ``(GTIN, language)`` row:** render the product template → upsert the
   WordPress page → verify it serves 200.
2. **Per GTIN, once every one of its rows has survived phase 1:** link the pages as
   translations of one another (§4.5) → set **one** GS1 resolver target carrying a link
   for *every* language (GET-before-write via ``safe_upsert``, §5.4) → render the QR.

The split is not tidiness. GS1's CreateOrUpdate **replaces** the whole ``links`` array,
so a write per language would leave only the last language's link — silently destroying
the others. And a translation group cannot be linked until every page in it exists. If
any row of a GTIN fails phase 1 the GTIN gets neither: a partial link set would destroy
the missing language's link, and persisting the survivor's state would make the next run
classify it UNCHANGED and never retry.

Each row's :class:`~lib.records.RunOutcome` is appended to
``output/{client_id}/runs/{ts}.jsonl`` **as it completes**, regardless of success, and
successful rows update ``output/{client_id}/state.json``. Writing the log incrementally
means a run that dies part-way still leaves a record of what it managed to do, and gives a
parent process something to tail — this script has no other progress channel. The path is
printed to stderr at the start of the run as well as at the end. The run is idempotent
(§6.5) and resumable: re-running the same confirmed plan yields the same final state.

``--only`` runs one leg instead of both. ``pages`` writes the WordPress pages and links
them as translations, and stops — nothing permanent happens, so the run is reversible by
editing or deleting the pages. ``links`` writes only the Digital Link records and the QR,
pointing them at pages that already exist. Omitting the flag does both, which is the
behaviour every existing invocation gets. Operators do not type this: the ``/gs1-pages``,
``/gs1-links`` and ``/gs1-publish`` skills supply it after their intent gate, exactly as
``--i-understand-production`` is supplied after the environment gate.

**``--only links`` verifies before it writes.** Its targets do not come from a page this
run just created, so each one is resolved (from ``state.json``, else a slug lookup, else
the plan row's ``target_url``) and must serve 2xx/3xx before the resolver is touched. A
GTIN with any unverifiable target gets no GS1 write at all. This lives here rather than in
the skill because a GS1 record can never be deleted: a permanent QR target pointing at a
404 is not recoverable, and prose in a skill can be skipped.

``--dry-run`` (§5.4 Level B) walks the plan and logs the intended WordPress/GS1
mutations without performing them — no HTTP writes, no QR files, no state update. It builds
no clients and so needs no credentials, which is also why a dry-run ``--only links`` lists
its intended targets without verifying them; the refusal above happens at execute time.

**Production guard.** A real run (not ``--dry-run``) against a client whose ``gs1.environment``
is ``production`` is refused unless ``--i-understand-production`` is passed. This makes the live
write a deliberate, explicit act rather than a bare ``--plan`` invocation — the interactive
review gates otherwise live only in the ``flow-orchestrator`` skill, not in this script. The
pilot allowlist, HELD-drop, and E21 still apply on top.

**E21 is enforced here, not only in the plan.** A row whose product carries no generated tagline
for its language is dropped with a warning (:func:`_drop_without_copy`), for clients with a
``generator`` configured. Copy is written per run for the rows a run executes, so an UNCHANGED row
arrives without any — and ``--plan`` confirms *every* row in the file. Without this, that
documented invocation would render a blank tagline over a live page.

Exit codes:
    0  every confirmed row succeeded
    1  one or more rows errored (state still saved for the rows that succeeded)
    2  config/setup error (bad client id, unreadable/invalid plan, missing GS1 creds),
       or a production run without --i-understand-production
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple
from urllib.parse import urlsplit

from pydantic import ValidationError

from lib import issue_report_files, resolver_check, run_quality, ticked_snapshot
from lib.acf import build_acf_payload
from lib.config import ClientConfig, MediaConfig, get_client
from lib.data_dir import enter_data_dir
from lib.eligibility import eligibility
from lib.env import load_env
from lib.errors import (
    ConfigError,
    GS1APIError,
    LLMAPIError,
    MediaIntegrityError,
    MediaOwnershipError,
    StateError,
    VideoMapError,
    WordPressAPIError,
)
from lib.gs1_dl_client import GS1Config as ResolvedGS1Config
from lib.gs1_dl_client import GS1DigitalLinkClient
from lib.input_layout import archive_path
from lib.link_set import build_links, link_set_hash
from lib.media import convert_image_for_web
from lib.media_video import (
    CLOSED_GATE,
    VideoGate,
    load_video_map,
    prepare_video,
    video_gate,
)
from lib.provenance import history_path, record_run
from lib.qr import render_qr
from lib.records import (
    ConfirmedPlan,
    Plan,
    PlanClassification,
    PlanRow,
    RunOutcome,
    State,
    StateEntry,
)
from lib.result_sheet import build as build_result_sheet
from lib.result_sheet import load_products
from lib.run_files import (
    QUALITY_NAME,
    SCAN_CHECK_NAME,
    SELECTION_NAME,
    SOURCES_NAME,
    TICKED_NAME,
    UPLOAD_NAME,
    log_path,
    sibling,
    stamp_of,
)
from lib.state import keeps_live_copy, load_state, save_state
from lib.templates import TemplateEngine
from lib.wp_client import MediaUpload, WordPressClient

_log = logging.getLogger("scripts.run_execute")

_EXIT_OK = 0
_EXIT_ERRORS = 1
_EXIT_CONFIG_ERROR = 2

#: Fallback resolver link type when a client defines no ``gs1_links`` (§2.4). A GS1 Web
#: Vocabulary CURIE: the API stores ``linkType`` unvalidated, so a bare ``"pip"`` is
#: accepted with a 200 and read back with a null ``linkTypeTitle`` — i.e. unrecognised.
#: Run-log timestamp format (UTC), shared with the JSONL filename.
_TS_FORMAT = "%Y%m%dT%H%M%SZ"


class _Mode(StrEnum):
    """Which leg of the publish a run performs (``--only``).

    ``BOTH`` is not a spelling of the flag — it is what omitting the flag means, so every
    invocation written before ``--only`` existed keeps its behaviour untouched.
    """

    PAGES = "pages"
    LINKS = "links"
    BOTH = "both"

    @property
    def writes_pages(self) -> bool:
        """Whether this mode renders and upserts WordPress pages."""
        return self is not _Mode.LINKS

    @property
    def writes_links(self) -> bool:
        """Whether this mode writes GS1 resolver records (and the QR that encodes them)."""
        return self is not _Mode.PAGES


# --- Plan loading ------------------------------------------------------------


def _load_confirmed(args: argparse.Namespace) -> ConfirmedPlan:
    """Load the plan and resolve the confirmed ``(gtin, language)`` subset (§8.3).

    ``--confirmed`` is read as a :class:`ConfirmedPlan`; ``--plan`` is read as a
    :class:`Plan` with every row implicitly confirmed.
    """
    if args.confirmed:
        data = json.loads(Path(args.confirmed).read_text(encoding="utf-8"))
        return ConfirmedPlan.model_validate(data)
    plan = Plan.model_validate(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    confirmed = {(row.gtin, row.language) for row in plan.rows}
    return ConfirmedPlan(plan=plan, confirmed_gtins_by_lang=confirmed)


# --- Per-row helpers ---------------------------------------------------------


def _client_meta(cfg: ClientConfig) -> dict[str, str]:
    """Return the client-level template context (§4.6)."""
    return {
        "id": cfg.client_id,
        "display_name": cfg.display_name,
        "default_language": cfg.wordpress.default_language,
    }


class _Page(NamedTuple):
    """One language's live page: what the per-GTIN phase needs to know about it.

    ``page_id`` is ``None`` only on the ``--only links`` path, for a page this tool does not
    manage and could not find by slug — the URL is then the plan row's ``target_url``. The
    resolver can still point at it (once verified), but nothing that needs an id may run,
    and no state is recorded for it.
    """

    page_id: int | None
    url: str
    title: str
    featured_media_id: int | None = None
    video_file: str | None = None
    video_failed: str | None = None
    video_failed_retry: bool | None = None


def _known_pages(gtin: str, fresh: dict[str, _Page], state: State) -> dict[str, _Page]:
    """Every language this GTIN has a page for — this run's, plus state's for the rest.

    An operator can confirm rows individually, so a run may carry only the fr row of a
    GTIN whose nl page already exists. The GS1 link array replaces, and WPML's translation
    group is the full set, so building either from the confirmed rows alone would drop nl
    — deleting its resolver link and breaking the translation pair. The state entry is the
    only record of a page this run did not touch, so it is what the missing languages are
    rebuilt from.

    Fresh pages win: a language written this run is more current than its state entry.
    """
    known = dict(fresh)
    for language, entry in state.entries.get(gtin, {}).items():
        if language not in known:
            known[language] = _Page(
                entry.wp_page_id, entry.wp_url, entry.title or "", entry.wp_featured_media_id
            )
    return known


def _find_page(cfg: ClientConfig, row: PlanRow, wp: WordPressClient) -> _Page:
    """Locate a page for a row this run is not writing: by slug, else by its planned URL.

    ``--only links`` exists to point resolver records at pages that already exist, so for a
    language with no state entry the page was made by somebody other than this tool. The id
    is looked up by slug (a read, no write); when even that misses — a site whose pages do
    not follow ``slug_pattern``, which is the ordinary case for pre-existing content — the
    row's planned ``target_url`` is used with no id at all.

    Neither branch is trusted on its own: :func:`_verify_targets` still has to see the URL
    serve before anything permanent is written.
    """
    try:
        found = wp.find_by_slug(cfg.wordpress.post_type, row.slug, row.language)
    except WordPressAPIError as exc:
        _log.warning(
            "slug lookup for %s/%s failed (%r); falling back to the planned URL",
            row.gtin,
            row.language,
            exc,
        )
        found = None
    if found is not None:
        return _Page(found["id"], found["link"], row.title)
    _log.warning(
        "gtin %s (%s): no state entry and no %s with slug %r — the resolver will point at "
        "the planned URL %s, and no state will be recorded for it",
        row.gtin,
        row.language,
        cfg.wordpress.post_type,
        row.slug,
        row.target_url,
    )
    return _Page(None, row.target_url, row.title)


def _pages_for_links(  # noqa: PLR0913 — three sources of pages, plus what to look the rest up with
    cfg: ClientConfig,
    gtin: str,
    rows: list[PlanRow],
    fresh: dict[str, _Page],
    wp: WordPressClient,
    state: State,
) -> dict[str, _Page]:
    """Every language's page this GTIN's resolver link set must span.

    Starts from :func:`_known_pages` — this run's pages plus state's — for the reason that
    function exists: the GS1 link array **replaces**, so a language left out is deleted from
    the record. Any confirmed language still unaccounted for is then located live, which is
    the ``--only links`` case where state has never heard of the product.
    """
    pages = _known_pages(gtin, fresh, state)
    for row in rows:
        if row.language not in pages:
            pages[row.language] = _find_page(cfg, row, wp)
    return pages


def _same_page(a: str, b: str) -> bool:
    """Whether two addresses name one page: scheme, host case and a trailing slash aside."""

    def key(url: str) -> tuple[str, str]:
        parts = urlsplit(url.strip())
        return ((parts.hostname or "").lower(), parts.path.rstrip("/") or "/")

    return key(a) == key(b)


def _listed_conflict(row: PlanRow) -> str | None:
    """Why a run that writes pages must refuse this row's GTIN, or ``None``.

    A listed page is the operator saying "this product already has a page, here". A pages or
    both run would publish a second one at ``target_url``, and a both run would then point the
    permanent GS1 record at the new page while the list says otherwise. Two answers to "which
    page is this product's" is a stop, not a choice to make on the operator's behalf.
    """
    if not row.listed_url or _same_page(row.listed_url, row.target_url):
        return None
    return (
        f"the process list names {row.listed_url} as this product's page, but this run would "
        f"publish its own at {row.target_url} — clear the cell to replace the old page, or run "
        f"--only links to point GS1 at it"
    )


def _listed_pages(
    cfg: ClientConfig,
    gtin: str,
    rows: list[PlanRow],
    wp: WordPressClient,
    state: State,
) -> dict[str, _Page]:
    """``--only links`` targets for a GTIN whose page the operator listed in the process list.

    The listed address is the default language's page, and the record's default link — so every
    scan resolves to it. Each other confirmed language gets a link too when the listed page names
    a translation for it (its ``hreflang`` alternate, which is how WPML advertises one); a page
    that names none gets no link in that language, rather than the GTIN being refused. Older
    pages are often Dutch only, and their QR still has somewhere to go.

    Refused when this tool already has its own page for the product at a different address —
    in state, or findable by ``slug_pattern``. Then there are two answers to which page the
    permanent record should name, and choosing one is the operator's call. When the tool's
    page *is* the listed one, nothing is in dispute and the ordinary lookup runs.

    No page here gets an id, so :func:`_commit_state` records no state for them — the same rule
    as any page this tool does not manage.
    """
    listed = rows[0].listed_url
    assert listed is not None  # the caller routes here only for a listed GTIN
    default = cfg.wordpress.default_language
    own = _known_pages(gtin, {}, state).get(default)
    if own is None:
        found = wp.find_by_slug(cfg.wordpress.post_type, rows[0].slug, default)
        own = _Page(found["id"], found["link"], rows[0].title) if found else None
    if own is not None:
        if _same_page(own.url, listed):
            return _pages_for_links(cfg, gtin, rows, {}, wp, state)
        raise RuntimeError(
            f"the process list names {listed} as this product's page, but this tool already "
            f"published its own at {own.url} — clear one of them before pointing a permanent "
            f"GS1 record at either"
        )
    pages: dict[str, _Page] = {}
    for row in rows:
        if row.language == default:
            pages[row.language] = _Page(None, listed, row.title)
            continue
        alternate = wp.alternate_url(listed, row.language)
        if alternate is None:
            _log.warning(
                "gtin %s: the listed page %s names no %s translation — the record gets no %s "
                "link, and a %s scan resolves to the %s page like every other",
                gtin,
                listed,
                row.language,
                row.language,
                row.language,
                default,
            )
            continue
        pages[row.language] = _Page(None, alternate, row.title)
    # Only another language was confirmed: the listed page is still the record's default link.
    pages.setdefault(default, _Page(None, listed, rows[0].title))
    return pages


def _verify_targets(wp: WordPressClient, pages: dict[str, _Page], verified: set[str]) -> None:
    """Raise unless every target not already verified this run serves 2xx/3xx.

    A GS1 record can never be deleted, so a resolver target pointing at a 404 is permanent —
    which makes this the one precondition that cannot live in skill prose, because prose can
    be skipped and a manual invocation would then burn a real GTIN.

    ``verified`` is the set of URLs :func:`_upsert_row` already checked immediately after
    writing them, so on the both-flow this covers only the languages :func:`_known_pages`
    rebuilt from state — which until now were used unverified, on nothing but the age of the
    state file. On ``--only links`` it covers all of them.

    ``verify_url`` returns ``True`` or raises; both outcomes are handled, since a caller that
    trusted only the return value would sail straight past the raise.
    """
    for language, page in sorted(pages.items()):
        if page.url in verified:
            continue
        try:
            ok = wp.verify_url(page.url)
        except WordPressAPIError as exc:
            raise RuntimeError(
                f"target URL for language {language} does not serve: {page.url} ({exc!r}) — "
                f"refusing to point a permanent GS1 record at it"
            ) from exc
        if not ok:
            raise RuntimeError(
                f"target URL for language {language} does not serve: {page.url} — "
                f"refusing to point a permanent GS1 record at it"
            )


def _digital_link_url(cfg: ClientConfig, row: PlanRow) -> str:
    """Build the canonical Digital Link URI for a GTIN (``.../01/{gtin14}``)."""
    return cfg.gs1.digital_link_url_pattern.format(gtin14=row.product.gtin14)


# --- Media (Phase 9.5) -------------------------------------------------------


class _RowMedia(NamedTuple):
    """The media a row contributes to its page: the hero image and this language's video.

    ``created_ids`` holds only the attachments *this row* put on the site, and exists so a row
    that fails afterwards can take them back down. Deduped ids are deliberately absent: they
    belong to an earlier run and are very likely carried by a page that is live and fine.
    """

    featured_media_id: int | None
    image_acf_value: int | str | None
    video_media_id: int | None
    created_ids: tuple[int, ...] = ()
    #: What the page was given — see :attr:`lib.records.StateEntry.video_file`.
    video_file: str | None = None
    #: A confirmed file that would not prepare — see :attr:`lib.records.StateEntry.video_failed`.
    video_failed: str | None = None
    #: Whether to try it again once it is in its folder — see ``StateEntry.video_failed_retry``.
    video_failed_retry: bool | None = None


_NO_MEDIA = _RowMedia(None, None, None)


class _Video(NamedTuple):
    """This language's video upload, and the filename the page is recorded as carrying.

    ``file`` is ``None`` when the client tracks no videos for this language at all, ``""`` when it
    does and the page gets none — no confirmed file, an unreadable mapping, a file that would not
    prepare — and the filename when one was uploaded. ``""`` is recorded so the next run can tell a
    page that is *waiting* for a video from one that never could have one.
    """

    upload: MediaUpload | None
    file: str | None
    #: A confirmed filename that could not be prepared; ``file`` is then ``""``.
    failed: str | None = None
    #: ``True`` when it failed for this machine's reason (file not in its folder, no ffmpeg).
    retry: bool | None = None


def _row_media(cfg: ClientConfig, row: PlanRow, wp: WordPressClient) -> _RowMedia:
    """Resolve the hero image and video for a row, uploading each to WP media.

    Every failure to *resolve* media — a 404 image, an undecodable file, a missing video, an
    ffmpeg error — degrades to ``None`` (edge E7): a source problem must never stop the page
    publishing. Returns all-``None`` when the client has no ``media`` config, so clients without
    media are untouched.

    **The upload itself is not in that category and deliberately propagates.** A refused upload
    (``WordPressAPIError``) or one WordPress stored short (``MediaIntegrityError``) fails the
    row, because the alternative is publishing a page whose video is absent or truncated while
    reporting success — and a half-uploaded video that the site serves happily is worse than a
    failed one. E7 is about media that was never available; this is about media that is wrong.

    Uploads are idempotent — ``upload_media`` dedupes by content hash and the converters are
    deterministic — so re-runs reuse attachments rather than duplicating them.
    """
    media = cfg.media
    if media is None:
        return _NO_MEDIA
    hero = _hero_media_id(cfg.client_id, media, row, wp)
    hero_id = hero.media_id if hero else None
    image_value = _image_acf_value(media, hero_id, wp)
    video = _video_media_id(cfg.client_id, media, row, wp)
    upload = video.upload
    created = tuple(up.media_id for up in (hero, upload) if up is not None and up.created)
    return _RowMedia(
        hero_id,
        image_value,
        upload.media_id if upload else None,
        created,
        video.file,
        video.failed,
        video.retry,
    )


def _hero_media_id(
    client_id: str, media: MediaConfig, row: PlanRow, wp: WordPressClient
) -> MediaUpload | None:
    """Download the export image, convert it to web JPEG, and upload it; ``None`` on any failure."""
    url = row.product.image_url
    if not url:
        return None
    data = wp.download_image(url)  # bytes | None (E7)
    if data is None:
        return None
    dest = Path("output") / client_id / "media" / "images" / f"{row.gtin}.jpg"
    jpeg = convert_image_for_web(
        data, dest, max_dim=media.image_max_dim, quality=media.image_quality
    )
    if jpeg is None:
        return None
    # Keyed on the barcode, so a renamed product finds the image it already has (see upload_media).
    return wp.upload_media(jpeg, title=f"{row.title} ({row.gtin})", key=row.gtin)


def _image_acf_value(
    media: MediaConfig, hero_id: int | None, wp: WordPressClient
) -> int | str | None:
    """The value written to the image ACF fields: an attachment id or its URL, per config."""
    if hero_id is None:
        return None
    if media.image_write_shape == "url":
        return wp.media_source_url(hero_id)
    return hero_id


def _missing_on_this_machine(src: Path, media: MediaConfig) -> bool:
    """Whether ``src`` cannot be prepared for a reason of this machine's — and warn which.

    Asked before ffmpeg is: ffmpeg reports a missing input as a bare exit code (4294967294 on
    Windows), which reads like a broken ffmpeg rather than a file in the wrong folder. Either
    reason is retried once it is fixed (``StateEntry.video_failed_retry``), unlike a file
    ffmpeg read and refused.
    """
    if not src.is_file():
        _log.warning(
            "video file not found at %s (looked from the folder %s) — skipping video",
            src,
            Path.cwd(),
        )
        return True
    if media.video_transcode and shutil.which(media.ffmpeg_bin) is None:
        _log.warning("ffmpeg not found (%s) — %s skipped; install ffmpeg", media.ffmpeg_bin, src)
        return True
    return False


def _video_media_id(
    client_id: str, media: MediaConfig, row: PlanRow, wp: WordPressClient
) -> _Video:
    """Resolve, prepare (transcode), and upload this language's video — see :class:`_Video`."""
    folder = media.video_folders.get(row.language)
    if not folder or not media.video_map_path:
        return _Video(None, None)
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError as exc:
        _log.warning("could not load video map %s: %s (skipping video)", media.video_map_path, exc)
        return _Video(None, "")
    filename = vmap.resolve(row.gtin, row.language)
    if not filename:
        return _Video(None, "")
    src = Path(folder) / filename
    if _missing_on_this_machine(src, media):
        return _Video(None, "", filename, retry=True)
    prepared = prepare_video(
        src,
        Path("output") / client_id / "media" / "videos",
        transcode=media.video_transcode,
        ffmpeg_bin=media.ffmpeg_bin,
    )
    if prepared is None:
        return _Video(None, "", filename, retry=False)
    upload = wp.upload_media(
        prepared,
        title=f"{row.title} video {row.language} ({row.gtin})",
        key=f"{row.gtin} video {row.language}",
    )
    return _Video(upload, filename)


# --- Execution ---------------------------------------------------------------


def _rollback_media(wp: WordPressClient, media: _RowMedia, row: PlanRow) -> None:
    """Take back down the attachments this row uploaded, when its page write did not happen.

    Nothing else in the tool removes them: an upload that succeeds before a failing page write
    leaves media referenced by no page and recorded in no state file, invisible to
    ``scripts.reconcile`` (which compares *pages*) and to any later run.

    Three things this deliberately does not do:

    * **It does not sweep.** Only ids from ``created_ids`` — attachments this row put there — are
      touched. There is no way to identify an orphan from an earlier run without inferring it,
      and inference against a live media library is not a thing to do with a DELETE.
    * **It does not touch a deduped id.** Those belong to an earlier run and are very probably
      carried by a page that is live and correct.
    * **It does not mask the original failure.** A delete that fails is logged with the id, so
      the operator can remove it by hand, and the row's real error is what propagates.
    """
    for media_id in media.created_ids:
        try:
            wp.delete_media(media_id)
        except (WordPressAPIError, MediaOwnershipError) as exc:
            _log.warning(
                "row %s/%s: could not clean up orphaned media %s (%r) — remove it by hand",
                row.gtin,
                row.language,
                media_id,
                exc,
            )
        else:
            _log.info(
                "row %s/%s: removed orphaned media %s after the page write failed",
                row.gtin,
                row.language,
                media_id,
            )


def _page_terms(cfg: ClientConfig, row: PlanRow, wp: WordPressClient) -> dict[str, list[int]]:
    """The taxonomy terms this row's page is put in — its category, in the page's language.

    Each ``wordpress.taxonomies`` entry names the product field its term comes from (``category``,
    which the plan fills from the GPC brick map). Without this the site's category pages never
    list a page this tool made: all 38 live products were in no category when that was found
    (2026-10-08). A product with no value gets no term — the plan already reported its unmapped
    brick.

    A value the site has no term for, in this language, refuses the row while
    ``categories.require_terms_exist`` (the default) — the term must be created on the site, or
    the map corrected, rather than a page going live where no category page shows it. Off, it is
    a warning and the page goes live without the term.
    """
    terms: dict[str, list[int]] = {}
    for taxonomy, source in cfg.wordpress.taxonomies.items():
        value = getattr(row.product, source.map_from_column, None)
        if not isinstance(value, str) or not value:
            continue
        term = wp.term_id(taxonomy, value, row.language)
        if term is not None:
            terms[taxonomy] = [term]
            continue
        message = (
            f"the site has no {taxonomy!r} term {value!r} in {row.language} — create it there, or "
            f"map the product to one that exists"
        )
        if cfg.categories is None or cfg.categories.require_terms_exist:
            raise RuntimeError(message)
        _log.warning("%s/%s: %s; publishing without it", row.gtin, row.language, message)
    return terms


def _upsert_row(  # noqa: PLR0913 — one collaborator per step, plus the outcome it annotates
    cfg: ClientConfig,
    row: PlanRow,
    wp: WordPressClient,
    engine: TemplateEngine,
    state: State,
    outcome: RunOutcome,
) -> _Page:
    """Phase 1 for one row: render, upsert the page, verify it serves. Raises on failure.

    Deliberately writes no state and sets no final status — the row is not done until its
    GTIN's per-product phase has run (see the module docstring).

    ``outcome`` is filled in as we go rather than from the return value, so that a page
    created and *then* failed by ``verify_url`` still reports its id and URL in the run
    log. Without that the operator gets an error naming no page.
    """
    prior = state.entries.get(row.gtin, {}).get(row.language)
    if row.keeps_live_copy and not keeps_live_copy(prior, row.classification):
        # The plan said this page is live and keeps its text; the ledger says otherwise. Writing it
        # anyway would publish a page with no tagline and no description — refused before any
        # upload, so it costs nothing.
        raise RuntimeError(
            "planned to keep the live page's text, but no published page is recorded for it — "
            "write copy for it on the Content screen, then plan again"
        )
    html = engine.render(row.product, row.language, _client_meta(cfg))
    terms = _page_terms(cfg, row, wp)  # before any upload: a missing term must cost nothing
    # Themes that render from ACF (Oxygen) ignore post_content entirely, so for those
    # clients the ACF payload *is* the page. The body is still written: it is inert
    # where it is ignored, and it is what non-ACF clients render from.
    acf = build_acf_payload(row.product, row.language, cfg.wordpress.acf_map)
    # Media (Phase 9.5): the hero image and this language's video are uploaded here and injected
    # into the ACF dict imperatively — their attachment ids are only known after upload, so they
    # cannot ride the static acf_map. They join the second (_write_acf) call, never the ?lang
    # create; featured_media is a core field on the create/update body.
    media = _row_media(cfg, row, wp)
    if cfg.media is not None:
        if media.image_acf_value is not None:
            acf[cfg.media.header_image_field] = media.image_acf_value
            acf[cfg.media.regular_image_field] = media.image_acf_value
        if media.video_media_id is not None:
            acf[cfg.media.video_file_field] = media.video_media_id
    # From here to the upsert returning, any failure leaves the attachments above referenced by
    # nothing. Past it, the page exists and carries them, so removing them would turn a failed
    # row into a broken live page — which is why the window closes exactly here.
    try:
        page = wp.upsert_page(
            post_type=cfg.wordpress.post_type,
            slug=row.slug,
            title=row.title,
            content=html,
            language=row.language,
            featured_media=media.featured_media_id,
            meta={"gtin": row.gtin},
            existing_id=prior.wp_page_id if prior else None,
            acf=acf,
            terms=terms or None,
        )
    except Exception:
        _rollback_media(wp, media, row)
        raise
    page_url = page["link"]
    outcome.wp_page_id = page["id"]
    outcome.wp_url = page_url
    # A file that would not prepare leaves the ACF field untouched, so the page still shows
    # whatever video it had: record that, not "no video", or the note and the next plan both lie.
    video_file = media.video_file
    if media.video_failed is not None:
        video_file = prior.video_file if prior is not None else ""
    outcome.video_file = video_file
    outcome.video_failed = media.video_failed
    if not wp.verify_url(page_url):
        raise RuntimeError(f"WordPress URL {page_url} did not return 200")
    return _Page(
        page["id"],
        page_url,
        row.title,
        media.featured_media_id,
        video_file,
        media.video_failed,
        media.video_failed_retry,
    )


def _item_description(cfg: ClientConfig, rows: list[PlanRow], pages: dict[str, _Page]) -> str:
    """The GS1 record's ``itemDescription`` — one per GTIN, so the default language's."""
    page = pages.get(cfg.wordpress.default_language)
    if page is not None and page.title:
        return page.title
    return rows[0].title


def _failed_call(exc: BaseException) -> str | None:
    """The request an exception blames, following ``__cause__`` when it was wrapped.

    A row issues a page write, an ACF write, a URL verification and up to two media uploads,
    and the exception alone does not say which. The API clients record it on the error; this
    reads it back for the run log.

    The chain walk is not defensive padding: :func:`_verify_targets` deliberately re-raises a
    :class:`WordPressAPIError` as a ``RuntimeError`` to explain *why* a non-serving target
    refuses a permanent GS1 write, so the one path that already adds context is the one that
    would otherwise drop the call identity.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, WordPressAPIError | GS1APIError | LLMAPIError | MediaIntegrityError):
            return current.call
        current = current.__cause__
    return None


def _record_failure(outcome: RunOutcome, exc: BaseException) -> None:
    """Mark a row failed, recording both what went wrong and which call it was."""
    outcome.status = "error"
    outcome.error = repr(exc)
    outcome.failed_call = _failed_call(exc)


def _block_gtin(gtin: str, rows: list[PlanRow], outcomes: dict[str, RunOutcome]) -> None:
    """Fail every row of a GTIN whose sibling failed phase 1, writing no state.

    Neither half of the per-product phase can run: a link set built from the surviving
    languages would **replace** the array and delete the failed language's link, and a
    translation group cannot be linked to a page that does not exist. Marking the survivor
    ``error`` is not bookkeeping — persisting its state would have the next run classify it
    UNCHANGED, so the GS1 write would never be retried and the failure would vanish.
    """
    failed = sorted(lang for lang, o in outcomes.items() if o.status == "error")
    for row in rows:
        outcome = outcomes[row.language]
        if outcome.status == "error":
            continue
        outcome.status = "error"
        outcome.error = (
            f"blocked: language(s) {', '.join(failed)} of this GTIN failed, so its GS1 link "
            f"set and translation group were not written"
        )
        _log.error("row %s/%s blocked by failed sibling(s) %s", gtin, row.language, failed)


def _finish_pages(  # noqa: PLR0913 — one collaborator per step; bundling them only hides them
    gtin: str,
    rows: list[PlanRow],
    fresh: dict[str, _Page],
    wp: WordPressClient,
    state: State,
    ts: datetime,
) -> dict[str, StateEntry]:
    """Phase 2, WordPress half: link the languages as translations; build their state.

    Returns the state entries rather than writing them — see :func:`_commit_state` for why
    a GTIN's state has to land all at once or not at all.

    ``gs1_link_set_hash`` carries the prior value, or ``""`` when there is none. That empty
    string is the record of "page published, resolver link never written": ``lib.state``
    reads it and reports the row CHANGED, so a ``--only pages`` run followed by ``--only
    links`` still has something to plan. Preserving a *prior* hash rather than blanking it
    matters just as much — re-running pages over a fully published product must not make it
    look like its resolver link vanished.
    """
    pages = _known_pages(gtin, fresh, state)
    rebuilt = sorted(set(pages) - set(fresh))
    if rebuilt:
        _log.warning(
            "gtin %s: language(s) %s were not written this run; their translation ids come "
            "from state, not from a page verified just now",
            gtin,
            rebuilt,
        )
    wp.link_translations(
        {lang: page.page_id for lang, page in pages.items() if page.page_id is not None}
    )
    entries: dict[str, StateEntry] = {}
    for row in rows:
        prior = state.entries.get(gtin, {}).get(row.language)
        page = fresh[row.language]
        if page.page_id is None:  # every page in ``fresh`` came from an upsert that returned one
            raise RuntimeError(f"page for {gtin}/{row.language} has no id after its upsert")
        entries[row.language] = StateEntry(
            wp_page_id=page.page_id,
            wp_url=page.url,
            wp_featured_media_id=page.featured_media_id,
            content_hash=row.content_hash,
            gs1_link_set_hash=prior.gs1_link_set_hash if prior else "",
            last_run=ts,
            title=row.title,  # the next run diffs against this (§10.6.2)
            video_file=page.video_file,  # lets a page published without one get it later
            video_failed=page.video_failed,
            video_failed_retry=page.video_failed_retry,
        )
    return entries


def _finish_links(  # noqa: PLR0913 — one collaborator per step; bundling them only hides them
    cfg: ClientConfig,
    gtin: str,
    rows: list[PlanRow],
    pages: dict[str, _Page],
    gs1: GS1DigitalLinkClient,
    outcomes: dict[str, RunOutcome],
) -> str:
    """Phase 2, GS1 half: one resolver write for the whole GTIN, then its QR.

    Returns the link-set hash for :func:`_commit_state`. One write carrying every language,
    never one per language: CreateOrUpdate replaces the ``links`` array wholesale.
    """
    links = build_links(cfg, rows[0].product, pages)
    gs1.safe_upsert(
        gtin=gtin,
        item_description=_item_description(cfg, rows, pages),
        links=links,
        is_enabled=True,
        overwrite=True,  # the plan is operator-confirmed; re-runs update in place (§6.5)
    )
    qr_paths = [str(p) for p in _render_qr_for(cfg, rows[0])]
    targets = {link["language"]: link["target_url"] for link in links}
    for row in rows:
        outcomes[row.language].gs1_set = True
        outcomes[row.language].qr_paths = qr_paths
        outcomes[row.language].resolver_target = targets.get(row.language)
    return link_set_hash(links)


def _commit_state(  # noqa: PLR0913 — the merge needs both legs' output plus where to put it
    state: State,
    gtin: str,
    rows: list[PlanRow],
    page_entries: dict[str, StateEntry],
    link_hash: str | None,
    ts: datetime,
) -> None:
    """Persist one GTIN's state, once every leg of this run has succeeded.

    Written here rather than inside each leg so a GTIN stays all-or-nothing. A resolver
    write that fails after the pages were upserted must not leave the page half behind: the
    next run would read a fresh ``content_hash``, and only the empty ``gs1_link_set_hash``
    would keep it from classifying UNCHANGED and never retrying the link.

    On ``--only links`` there are no page entries, so each row updates the entry already in
    state. A row with neither gets **no state at all** — that is the page this tool does not
    manage (:func:`_find_page`), and inventing a ``content_hash`` for it would claim we
    published content we never wrote, which the next run would believe.
    """
    for row in rows:
        entry = page_entries.get(row.language) or state.entries.get(gtin, {}).get(row.language)
        if entry is None:
            _log.warning(
                "gtin %s (%s): resolver link written for a page this tool does not manage; "
                "recording no state for it",
                gtin,
                row.language,
            )
            continue
        if link_hash is not None:
            # per-GTIN: every language shares the one link set. ``retracted`` is cleared
            # with it because the write that produced this hash passed ``is_enabled=True``
            # — the record is back. A held row only reaches here under ``--revive``
            # (:func:`_drop_held`), so this cannot un-hold anything on its own, and
            # ``_is_held`` is an OR: a product whose pages are still drafts stays held.
            entry = entry.model_copy(
                update={"gs1_link_set_hash": link_hash, "retracted": False, "last_run": ts}
            )
        state.entries.setdefault(gtin, {})[row.language] = entry


def _execute_gtin(  # noqa: PLR0913 — one collaborator per step; bundling them only hides them
    cfg: ClientConfig,
    gtin: str,
    rows: list[PlanRow],
    wp: WordPressClient,
    gs1: GS1DigitalLinkClient,
    engine: TemplateEngine,
    state: State,
    ts: datetime,
    mode: _Mode,
) -> list[RunOutcome]:
    """Run every confirmed row of one GTIN, then its per-product writes. Never raises."""
    outcomes = {
        row.language: RunOutcome(gtin=gtin, language=row.language, ts=ts, status="pending")
        for row in rows
    }
    fresh: dict[str, _Page] = {}
    if mode.writes_pages:
        conflict = next(filter(None, (_listed_conflict(row) for row in rows)), None)
        if conflict:
            for row in rows:
                _record_failure(outcomes[row.language], RuntimeError(conflict))
            _log.error("gtin %s refused: %s", gtin, conflict)
            return [outcomes[row.language] for row in rows]
        for row in rows:
            try:
                fresh[row.language] = _upsert_row(
                    cfg, row, wp, engine, state, outcomes[row.language]
                )
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the run
                _record_failure(outcomes[row.language], exc)
                _log.error("row %s/%s failed: %r", gtin, row.language, exc)
        if len(fresh) != len(rows):
            _block_gtin(gtin, rows, outcomes)
            return [outcomes[row.language] for row in rows]

    try:
        page_entries = _finish_pages(gtin, rows, fresh, wp, state, ts) if mode.writes_pages else {}
        link_hash = None
        if mode.writes_links:
            pages = (
                _pages_for_links(cfg, gtin, rows, fresh, wp, state)
                if mode.writes_pages or not rows[0].listed_url
                else _listed_pages(cfg, gtin, rows, wp, state)
            )
            _verify_targets(wp, pages, {page.url for page in fresh.values()})
            link_hash = _finish_links(cfg, gtin, rows, pages, gs1, outcomes)
        _commit_state(state, gtin, rows, page_entries, link_hash, ts)
    except Exception as exc:  # noqa: BLE001 — one bad GTIN must not abort the run
        for row in rows:
            _record_failure(outcomes[row.language], exc)
        _log.error("gtin %s failed its per-product writes: %r", gtin, exc)
        return [outcomes[row.language] for row in rows]

    for row in rows:
        outcomes[row.language].status = "ok"
    return [outcomes[row.language] for row in rows]


def _execute(  # noqa: PLR0913 — one collaborator per step; bundling them only hides them
    cfg: ClientConfig,
    rows: list[PlanRow],
    wp: WordPressClient,
    gs1: GS1DigitalLinkClient,
    engine: TemplateEngine,
    state: State,
    ts: datetime,
    mode: _Mode,
) -> Iterator[RunOutcome]:
    """Execute the confirmed rows grouped by GTIN, yielding outcomes as each GTIN finishes.

    A generator rather than a list so the caller can write each outcome to the run log the
    moment it is final — and *final* means when its GTIN finishes, not when its row does: a
    row's outcome stays mutable until then, because a later language failing blocks the whole
    GTIN and rewrites every one of its rows to ``error``.

    Grouped with a dict rather than by walking runs of adjacent rows: rows for one GTIN
    happen to be adjacent today only because ``diff_against_state`` builds them in a nested
    loop, and :class:`~lib.records.Plan` promises no such ordering. Outcomes therefore come
    out in completion order, which for any GTIN-grouped plan — every plan this tool writes —
    is also plan order.
    """
    by_gtin: dict[str, list[PlanRow]] = {}
    for row in rows:
        by_gtin.setdefault(row.gtin, []).append(row)

    for gtin, gtin_rows in by_gtin.items():
        yield from _execute_gtin(cfg, gtin, gtin_rows, wp, gs1, engine, state, ts, mode)


def _render_qr_for(cfg: ClientConfig, row: PlanRow) -> list[Path]:
    """Render the QR for a row, or nothing when the client has no QR config."""
    if cfg.qr is None:
        _log.warning("no qr config for client %s; skipping QR for %s", cfg.client_id, row.gtin)
        return []
    return render_qr(
        uri=_digital_link_url(cfg, row),
        output_dir=Path("output") / cfg.client_id / "qr",
        gtin=row.gtin,
        formats=cfg.qr.formats,
        size_mm=cfg.qr.size_mm,
        ecc=cfg.qr.error_correction,
        dpi=cfg.qr.dpi,
    )


def _preview_row(
    cfg: ClientConfig, row: PlanRow, engine: TemplateEngine, ts: datetime, mode: _Mode
) -> RunOutcome:
    """Render the template and log the intended mutations without performing them (§5.4)."""
    outcome = RunOutcome(gtin=row.gtin, language=row.language, ts=ts, status="dry-run")
    try:
        if mode.writes_pages:
            engine.render(row.product, row.language, _client_meta(cfg))
        # One line per row, but the GS1 write is per GTIN: a GTIN with two confirmed rows
        # gets one resolver write carrying both languages' links, not one write per line.
        _log.info("[dry-run] %s/%s: %s", row.gtin, row.language, _preview_text(cfg, row, mode))
    except Exception as exc:  # noqa: BLE001 — surface template errors as a failed preview row
        _record_failure(outcome, exc)
        _log.error("dry-run row %s/%s failed: %r", row.gtin, row.language, exc)
    return outcome


def _preview_text(cfg: ClientConfig, row: PlanRow, mode: _Mode) -> str:
    """One row's intended mutations, in the words of the leg(s) actually selected.

    The links half names the *planned* target rather than a resolved one: a dry run builds
    no clients, so it cannot read state or look a page up, let alone verify it. Saying so is
    the point — the verification that refuses a 404 happens at execute time.
    """
    parts = []
    if mode.writes_pages:
        media = " (with hero image/video)" if cfg.media is not None else ""
        category = f" in category {row.product.category!r}" if row.product.category else ""
        parts.append(
            f"upsert WP {cfg.wordpress.post_type!r} page {row.slug!r}{media}{category}, then link "
            f"this GTIN's languages as translations"
        )
    if mode.writes_links:
        target = row.target_url
        default = cfg.wordpress.default_language
        if row.listed_url and not mode.writes_pages and row.language == default:
            target = f"the listed page {row.listed_url}"
        elif row.listed_url and not mode.writes_pages:
            # What :func:`_listed_pages` does at execute time — a dry run cannot look it up.
            target = (
                f"the {row.language} version the listed page {row.listed_url} names (if it names "
                f"none, no {row.language} link: a {row.language} scan reaches the {default} page)"
            )
        parts.append(
            f"point GS1 {_digital_link_url(cfg, row)} at {target} and render its "
            f"QR (the real run verifies that target serves first, and refuses if it does not)"
        )
    conflict = _listed_conflict(row) if mode.writes_pages else None
    if conflict:
        return f"would refuse this GTIN: {conflict}"
    return "would " + ", then ".join(parts)


def _confirmed_rows(confirmed: ConfirmedPlan) -> list[PlanRow]:
    """Return the plan rows in the confirmed subset, in plan order."""
    keys = confirmed.confirmed_gtins_by_lang
    return [row for row in confirmed.plan.rows if (row.gtin, row.language) in keys]


def _drop_held(rows: list[PlanRow], *, revive: bool) -> list[PlanRow]:
    """Drop rows for GTINs that were deliberately unpublished, unless ``revive`` (§8.3).

    A held GTIN is one ``run_unpublish`` took down. Confirming a plan is a judgement about
    *content* — the operator is agreeing the pages are right, not that a product somebody
    unpublished should go back up — so reviving one takes its own flag rather than riding
    along on that confirmation.

    Dropped by GTIN rather than by row, for the reason the per-GTIN phase exists at all:
    the resolver write carries every language at once, so publishing one language of a
    held GTIN would write a link set missing the other.
    """
    held = {row.gtin for row in rows if row.classification is PlanClassification.HELD}
    if not held:
        return rows
    if revive:
        _log.warning(
            "--revive: re-publishing %d held GTIN(s): %s", len(held), ", ".join(sorted(held))
        )
        return rows
    _log.warning(
        "skipping %d held (unpublished) GTIN(s): %s — pass --revive to publish them again",
        len(held),
        ", ".join(sorted(held)),
    )
    return [row for row in rows if row.gtin not in held]


def _drop_without_copy(rows: list[PlanRow], *, generator_configured: bool) -> list[PlanRow]:
    """Drop rows with no generated tagline for their language — E21, enforced where it is claimed.

    This module's docstring has said "the pilot allowlist, HELD-drop, and **E21** still apply on
    top" since ``--only`` existed, and until now only the first two were true here: E21 lived in
    ``run_plan`` alone, so it protected a plan this tool had just written and nothing else.

    That was survivable while copy was written for every in-scope unit. It stopped being so when
    generation was scoped to the rows a run executes: an UNCHANGED row now legitimately carries no
    copy, and ``--plan`` confirms **every** row in the file (``docs/setup.md``, the pilot handoff),
    so the documented escape hatch would render a blank tagline and a description-less body over a
    live page. Rendering happens per row and the template does not check, so nothing downstream
    would have refused it.

    Dropped per **row**, not per GTIN like :func:`_drop_held`: copy is per language, and a GTIN
    with nl copy and no fr copy has one publishable row. The per-GTIN phase still refuses to write
    a partial link set, so the resolver cannot be damaged by the survivor.

    Args:
        rows: The confirmed rows, after the HELD drop.
        generator_configured: Whether the client has a ``generator`` block — the same condition
            ``run_plan`` derives ``require_generated_copy`` from. Without it this guard would drop
            every row of a client that publishes feed copy only, which is a run that does nothing
            and reports success.

    Returns:
        The rows that carry copy, or all of them when no generator is configured.
    """
    if not generator_configured:
        return rows
    kept, dropped = [], []
    for row in rows:
        tagline = row.product.generated_tagline
        # A live page planned without copy is written keeping its text; ``_upsert_row`` checks
        # against the ledger that the page really exists before it writes one without copy.
        if (tagline and tagline.values.get(row.language)) or row.keeps_live_copy:
            kept.append(row)
        else:
            dropped.append(f"{row.gtin}/{row.language}")
    if dropped:
        _log.warning(
            "skipping %d row(s) with no generated copy (E21): %s — an unchanged row is not "
            "generated for, which is expected; anything else means the results file does not "
            "answer this plan",
            len(dropped),
            ", ".join(dropped),
        )
    return kept


def _pilot_allowlist(cfg: ClientConfig) -> VideoGate | None:
    """The video gate a run must pass, or ``None`` when unrestricted (§9.5).

    ``None`` unless the client sets ``media.restrict_to_mapped_gtins``. Otherwise the
    :class:`~lib.media_video.VideoGate` built live from the mapping file — the same object the plan
    held against, so the two cannot disagree about ``media.publish_without_video``. If the mapping
    cannot be loaded the gate is **closed** (block everything) — failing safe, since a run that
    cannot determine what may publish must not publish anything.
    """
    media = cfg.media
    if media is None or not media.restrict_to_mapped_gtins or not media.video_map_path:
        return None
    try:
        vmap = load_video_map(Path(media.video_map_path))
    except VideoMapError as exc:
        _log.error(
            "cannot load video map %s for the pilot allowlist: %s — blocking every GTIN",
            media.video_map_path,
            exc,
        )
        return CLOSED_GATE
    return video_gate(
        vmap, cfg.wordpress.languages, publish_without_video=media.publish_without_video
    )


def _restrict_to_pilot(rows: list[PlanRow], gate: VideoGate | None) -> list[PlanRow]:
    """Drop rows for GTINs the video gate does not admit, so none is ever written (§9.5).

    A hard safety gate applied to every run that writes pages: even a plan passed with ``--plan``
    cannot publish a GTIN the gate holds — no confirmed video in some language (unless the client
    publishes without one), or two confirmed files in one. ``None`` means unrestricted, which is
    what a links-only run passes: it writes no page, so no video can be missing from one.
    """
    if gate is None:
        return rows
    blocked = sorted({row.gtin for row in rows if not gate.admits(row.gtin)})
    if blocked:
        _log.warning(
            "pilot restriction: blocking %d GTIN(s) the video rule holds: %s",
            len(blocked),
            ", ".join(blocked),
        )
    return [row for row in rows if gate.admits(row.gtin)]


class _RunLog:
    """The run's JSONL, written a row at a time rather than all at once at the end.

    Two things follow from writing incrementally. A run that dies part-way — a crashed
    process, a killed terminal, an unhandled error building the clients — used to lose the
    *entire* log, including the rows that had already succeeded and been committed to
    ``state.json``; now it keeps them. And a parent process has a file it can tail while the
    run is in flight, which is the only progress channel this script offers.

    The file is created with ``"x"`` (atomic exclusive create) and disambiguated on
    collision, because the name is ``datetime.now(UTC)`` to the second: two runs started in
    the same second would otherwise share one file and interleave their rows into nonsense.
    That does **not** make concurrent runs supported (E20) — ``state.json`` still races —
    it only stops one run's log from being corrupted by another's.

    Each row is flushed as it is written, so a hard kill loses nothing already reported.
    """

    #: How many same-second suffixes to try before giving up.
    _MAX_COLLISIONS: Final = 100

    def __init__(self, path: Path) -> None:
        for n in range(self._MAX_COLLISIONS):
            # The **directory** takes the suffix, not the log. Suffixing the log gave
            # `{stamp}/run-1.jsonl`, which `run_files.iter_logs` matches with neither of its globs
            # — `*/run.jsonl` nor `*.jsonl` — so a same-second second run was invisible on the Runs
            # screen and to `newest_log`, which is what `report_scope_result` reports on by default.
            # Two runs in one second is precisely what a re-run after a failure looks like, and an
            # invisible run is the one case an operator most needs to see: live pages and permanent
            # GS1 records may already exist for the rows that landed.
            #
            # As a directory the suffix costs nothing: `*/run.jsonl` finds it, `stamp_of` labels it
            # `{stamp}-1`, and `sibling` puts its own four documents inside it rather than in a
            # subfolder of the first run's.
            folder = path.parent if n == 0 else path.parent.with_name(f"{path.parent.name}-{n}")
            candidate = folder / path.name
            folder.mkdir(parents=True, exist_ok=True)
            try:
                self._handle = candidate.open("x", encoding="utf-8")
            except FileExistsError:
                continue
            self.path = candidate
            return
        raise OSError(f"cannot create a run log at {path}: {self._MAX_COLLISIONS} names taken")

    def append(self, outcome: RunOutcome) -> RunOutcome:
        """Write one outcome as a JSON line and flush; returns it for convenient chaining."""
        self._handle.write(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self._handle.flush()
        return outcome

    def append_all(self, outcomes: Iterable[RunOutcome]) -> list[RunOutcome]:
        """Consume ``outcomes``, writing each as it arrives, and return them all."""
        return [self.append(outcome) for outcome in outcomes]

    def __enter__(self) -> _RunLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()


def _keep_selection(cfg: ClientConfig, log: Path, *, mode: str, dry_run: bool) -> None:
    """Copy the selection this run is about to consume into the run's own directory.

    ``input/`` holds one thing: what the **next** run will use. It is overwritten by the next
    batch, so a report generated afterwards from that file describes somebody else's rows — and
    until now that was the only copy, which meant a run's own scope became unknowable the moment
    the operator uploaded again.

    Best-effort on purpose. A run that has already begun writing live pages must not be stopped by
    a failure to copy a spreadsheet for a report, so this reports and carries on.
    """
    if cfg.process_list is None:
        return
    control = Path(cfg.process_list.path)
    # Both documents, because the report needs both: the ticked list says what ran, and the
    # upload it came from is the only thing that can name the rows the operator *dropped*. Reading
    # the upload out of ``input/`` afterwards worked until the next batch replaced it.
    #
    # The upload's path comes from ``lib.input_layout`` and not from a guess made here. This asked
    # for ``selection/uploaded.xlsx``, which nothing has written since the folders were split, and
    # the ``is_file`` skip below meant the copy silently never happened — so every result sheet
    # reported "no uploaded list archived" and could not name a single deselected row.
    for source, name in (
        (control, SELECTION_NAME),
        (archive_path(control), UPLOAD_NAME),
    ):
        if not source.is_file():
            continue
        try:
            shutil.copyfile(source, sibling(log, name))
        except OSError as exc:  # noqa: BLE001 — a report file is not worth stopping a live run
            print(f"warning: could not keep a copy of {source}: {exc}", file=sys.stderr)
    # What was ticked on the Data screen, so the run's issue report covers the same products the
    # screen's did — only when it was saved with this very selection.
    if ticked_snapshot.read(ticked_snapshot.path_for(control), control) is not None:
        try:
            shutil.copyfile(ticked_snapshot.path_for(control), sibling(log, TICKED_NAME))
        except OSError as exc:  # noqa: BLE001 — as above
            print(f"warning: could not keep the ticked products: {exc}", file=sys.stderr)

    # And what those documents *are*, by name and hash, so "which export did this run use?" has an
    # answer that survives the next upload. The copies above answer "which rows"; they cannot say
    # which export the ticks were chosen against, because a selection carries no trace of it.
    export = Path(cfg.export.path)
    record_run(
        sibling(log, SOURCES_NAME),
        mode=mode,
        dry_run=dry_run,
        selection=control,
        product_list=archive_path(control),
        export=export,
        history=history_path(export),
    )


def _run(  # noqa: PLR0913 — the plan, its credentials, and one flag per policy switch
    cfg: ClientConfig,
    confirmed: ConfirmedPlan,
    resolved_gs1: ResolvedGS1Config | None,
    *,
    dry_run: bool,
    revive: bool,
    mode: _Mode,
) -> int:
    """Execute (or preview) the confirmed plan; return the process exit code."""
    rows = _restrict_to_pilot(
        _drop_without_copy(
            _drop_held(_confirmed_rows(confirmed), revive=revive),
            # A links run renders no page, so the copy a page would carry is not its business:
            # the resolver link's title comes from the product name, not the tagline.
            generator_configured=cfg.generator is not None and mode.writes_pages,
        ),
        # The video gate is about a page; a links run writes none (operator, 2026-10-09). Its one
        # precondition is a target that serves, checked by ``_verify_targets`` before every write.
        _pilot_allowlist(cfg) if mode.writes_pages else None,
    )
    engine = TemplateEngine(cfg.client_id, cfg.template)
    ts = datetime.now(UTC)
    prefix = "[dry-run] " if dry_run else ""
    leg = "" if mode is _Mode.BOTH else f" ({mode} only)"

    # Announced up front, not just at the end: the name is derived from a timestamp only this
    # process knows, so nothing outside it can compute where the run is reporting to — and a
    # run that dies never reaches the closing line at all.
    with _RunLog(log_path(cfg.client_id, ts.strftime(_TS_FORMAT))) as log:
        print(f"{prefix}{len(rows)} row(s){leg}; log: {log.path}", file=sys.stderr)
        _keep_selection(cfg, log.path, mode=str(mode), dry_run=dry_run)
        if dry_run or resolved_gs1 is None:
            outcomes = log.append_all(_preview_row(cfg, row, engine, ts, mode) for row in rows)
        else:
            state = load_state(cfg.client_id)
            with (
                WordPressClient(cfg.wordpress) as wp,
                GS1DigitalLinkClient(resolved_gs1) as gs1,
            ):
                outcomes = log.append_all(_execute(cfg, rows, wp, gs1, engine, state, ts, mode))
            save_state(state)

    errors = sum(1 for o in outcomes if o.status == "error")
    _write_result_sheet(
        cfg,
        log.path,
        outcomes,
        mode=str(mode),
        dry_run=dry_run,
        confirmed={row.product.gtin14 for row in rows},
    )
    if not dry_run and resolved_gs1 is not None:
        _write_quality_note(log.path, outcomes)
        if mode.writes_links:
            _check_landings(cfg, log.path, outcomes)
    _log.info("run complete: %d ok, %d error(s)", len(outcomes) - errors, errors)
    print(
        f"{prefix}{len(outcomes)} row(s){leg}, {errors} error(s); log: {log.path}",
        file=sys.stderr,
    )
    return _EXIT_ERRORS if errors else _EXIT_OK


def _write_result_sheet(  # noqa: PLR0913 — the run, as the report needs it
    cfg: ClientConfig,
    log: Path,
    outcomes: list[RunOutcome],
    *,
    mode: str,
    dry_run: bool,
    confirmed: set[str],
) -> None:
    """The per-row result sheet, without being asked for. On failure too.

    The operator sent a list of barcodes and gets back a log keyed by (GTIN, language); this is
    the sheet that turns the second back into the first. It was built by the *shell*, in the handler
    that had just streamed the run — so a publish driven from anywhere else produced no sheet at
    all, and the moment it is most wanted is the moment a run has half-failed, which is also when
    nobody goes looking for a report.

    Best-effort, and broadly so. Every exception is caught, not just ``OSError``: this runs after
    live pages and permanent GS1 records already exist, and a report that raised here would take the
    run's exit code with it — turning a publish that worked into one that reads as failed, with no
    way to tell which from the outside.
    """
    if cfg.process_list is None:
        return
    try:
        built = build_result_sheet(cfg, log, warn=lambda line: print(line, file=sys.stderr))
    except Exception as exc:  # noqa: BLE001 — see the docstring; a report must not fail a publish
        print(f"warning: could not write the result sheet: {exc}", file=sys.stderr)
        return
    print(f"result sheet: {built.out}", file=sys.stderr)
    # The client's short report — failed products and why — from the same rows, beside it.
    try:
        products = load_products(Path("output") / cfg.client_id / "data" / "products.json")
        expected = {}
        if dry_run and mode != "links":
            ran = built.sheets.control.listed_gtins()
            verdict = eligibility(cfg, [p for p in products if p.gtin14 in ran])
            expected = verdict.missing_video
        report = issue_report_files.for_run(
            f"Product issues - {cfg.display_name}",
            stamp_of(log),
            built.rows,
            outcomes,
            products,
            mode=mode,
            dry_run=dry_run,
            default_language=cfg.wordpress.default_language,
            expected_without_video=expected,
            ticked=ticked_snapshot.read(sibling(log, TICKED_NAME)),
            confirmed=confirmed,
        )
        pdf, _ = issue_report_files.write(report, log.parent)
    except Exception as exc:  # noqa: BLE001 — as above: a report must not fail a publish
        print(f"warning: could not write the issue report: {exc}", file=sys.stderr)
        return
    print(f"issue report: {pdf} (and .xlsx)", file=sys.stderr)


def _write_quality_note(log: Path, outcomes: list[RunOutcome]) -> None:
    """The run's ``data-quality.md`` — the pages it published with no video. Best-effort.

    Written beside the log on every live run, including one where nothing is missing, so the
    absence of the file means "this run predates it" rather than "nothing to report". Caught as
    broadly as :func:`_write_result_sheet`, for the same reason: it runs after live writes, and a
    report must never turn a publish that worked into one that reads as failed.
    """
    try:
        target = sibling(log, QUALITY_NAME)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(run_quality.render(stamp_of(log), outcomes), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — see the docstring; a report must not fail a publish
        print(f"warning: could not write the data-quality note: {exc}", file=sys.stderr)
        return
    bare = len(run_quality.without_video(outcomes))
    if bare:
        print(f"{bare} page(s) went live without a video — see {target}", file=sys.stderr)


def _landing_targets(outcomes: list[RunOutcome]) -> dict[str, dict[str, str]]:
    """GTIN → language → page, for every row whose GS1 record this run wrote."""
    targets: dict[str, dict[str, str]] = {}
    for outcome in outcomes:
        if outcome.status == "ok" and outcome.gs1_set and outcome.resolver_target:
            targets.setdefault(outcome.gtin, {})[outcome.language] = outcome.resolver_target
    return targets


def _check_landings(cfg: ClientConfig, log: Path, outcomes: list[RunOutcome]) -> None:
    """Scan each record this run wrote, once per language, and warn where a scan lands wrong.

    Best-effort and warning-only, like :func:`_write_quality_note`: the record is written and
    its pages serve, so a scan landing on the other language's page is a finding for a person,
    not a failed publish. Written beside the log as ``scan-check.json`` on every links run, so
    a missing file means "this run predates the check", never "nothing was wrong".
    """
    targets = _landing_targets(outcomes)
    if not targets:
        return
    print(
        f"checking where a scan lands for {len(targets)} product(s), per language — "
        f"a fresh record can take a minute to show…",
        file=sys.stderr,
    )
    try:
        landings = resolver_check.check_until_settled(
            targets,
            lambda gtin: cfg.gs1.digital_link_url_pattern.format(gtin14=gtin),
            _fetch_landing,
            sleep=_sleep,
            clock=_clock,
        )
        bad = resolver_check.wrong(landings)
        target = sibling(log, SCAN_CHECK_NAME)
        target.write_text(
            json.dumps([{**asdict(landing), "ok": landing.ok} for landing in landings], indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring; a check must not fail a publish
        print(f"warning: could not check where the scans land: {exc}", file=sys.stderr)
        return
    if not bad:
        print(f"scan check: all {len(landings)} scan(s) land on their own page", file=sys.stderr)
        return
    print(
        f"WARNING scan check: {len(bad)} of {len(landings)} scan(s) do not land on their own "
        f"page (GS1 may still be updating — re-scan in a few minutes; details in {target}):",
        file=sys.stderr,
    )
    for landing in bad:
        print(f"  {landing.describe()}", file=sys.stderr)


#: Injection points for the scan check, so a test never reaches the real resolver or waits.
_fetch_landing: resolver_check.Fetch = resolver_check.fetch_landing
_sleep = time.sleep
_clock = time.monotonic


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_execute", description="Execute a confirmed run plan."
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", help="Path to a Plan JSON (all rows confirmed)")
    source.add_argument("--confirmed", help="Path to a ConfirmedPlan JSON")
    parser.add_argument(
        "--only",
        choices=[_Mode.PAGES.value, _Mode.LINKS.value],
        help=(
            "Run one leg instead of both: 'pages' writes the WordPress pages and stops "
            "(reversible); 'links' writes only the Digital Link records and QR, pointing them "
            "at pages that already exist (permanent, and each target is verified first). "
            "Omit to do both, which is the default."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview intended mutations without performing them"
    )
    parser.add_argument(
        "--revive",
        action="store_true",
        help="Also publish GTINs that run_unpublish took down (skipped by default)",
    )
    parser.add_argument(
        "--i-understand-production",
        action="store_true",
        help=(
            "Required to execute a real run when gs1.environment is 'production' — guards live "
            "writes to WordPress and permanent GS1 records. Not needed with --dry-run."
        ),
    )
    return parser.parse_args(argv)


#: Exit-2 message for a ``run_plan --links-only`` plan offered to a mode that writes pages. That
#: plan kept units with no generated copy, and rendering one publishes a blank tagline over a
#: live page — the very thing E21 exists to stop.
_LINKS_ONLY_REFUSAL = (
    "config error: this plan was built with `run_plan --links-only`, so it carries rows with no "
    "generated copy; it can only run with `--only links`, not in {mode!r} mode. Re-run run_plan "
    "without --links-only to publish pages."
)


def _production_refusal(cfg: ClientConfig, mode: _Mode) -> str:
    """The exit-2 message for a production run without the acknowledgment flag.

    Names only what the selected leg actually does. ``--only pages`` writes live pages but
    creates no permanent records, and claiming otherwise would teach the operator to read
    past this message — which is the one thing it cannot afford.
    """
    if mode is _Mode.PAGES:
        consequence = f"would publish live pages to {cfg.wordpress.site_url}"
    elif mode is _Mode.LINKS:
        consequence = (
            "would register permanent GS1 records (GS1 v2 has no delete — records can only "
            "be disabled)"
        )
    else:
        consequence = (
            f"would publish live pages to {cfg.wordpress.site_url} and register permanent GS1 "
            "records (GS1 v2 has no delete — records can only be disabled)"
        )
    return (
        f"refusing to write to PRODUCTION: gs1.environment is 'production', so this run "
        f"{consequence}. Re-run with --i-understand-production to proceed, or --dry-run to "
        f"preview without writing."
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = _parse_args(argv)
    # A dry run exists to show the intended mutations, and those are logged at INFO — at
    # WARNING it printed nothing but a row count, which is the one thing it did not need to
    # say. Only dry runs are raised: a real run's per-row detail belongs in the JSONL.
    logging.basicConfig(
        level=logging.INFO if args.dry_run else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    mode = _Mode(args.only) if args.only else _Mode.BOTH
    try:
        cfg = get_client(args.client_id)
        confirmed = _load_confirmed(args)
        if confirmed.plan.links_only and mode is not _Mode.LINKS:
            print(_LINKS_ONLY_REFUSAL.format(mode=mode), file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        if (
            not args.dry_run
            and cfg.gs1.environment == "production"
            and not args.i_understand_production
        ):
            print(_production_refusal(cfg, mode), file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        resolved_gs1 = None if args.dry_run else cfg.gs1.resolve()
    except (
        ConfigError,
        StateError,
        FileNotFoundError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    return _run(cfg, confirmed, resolved_gs1, dry_run=args.dry_run, revive=args.revive, mode=mode)


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
