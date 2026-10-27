"""Client for the WordPress REST API v2.

Implements ``docs/IMPLEMENTATION_SPEC.md`` §4.4 (client shape), §5.1 (the WordPress
error-handling matrix), §6.1/§6.2 (upsert/media idempotency), and the edge cases E7
(image 404 → featured media skipped), E8 (mismatched ``meta.gtin`` → skip row), and
E11 (slug collision with a non-GTIN page → human intervention).

Auth is HTTP Basic with an application password (``PROJECT_HANDOVER.md`` §4.4): the
username comes from config and the password is resolved lazily from the environment
variable named in ``app_password_env`` and sent as an ``Authorization: Basic`` header.
The header is never logged — :func:`lib.logging_setup.scrub_headers` redacts it and
:func:`lib.logging_setup.scrub_response_body` redacts the WordPress ``meta.*`` subtree.

The client mirrors ``lib/gs1_dl_client.py``: a single ``_request`` retry loop with
independent 429/5xx budgets (minus the OAuth token dance — per §5.1 a WordPress
``401`` is terminal), and an ``_api_error`` builder that logs failures scrubbed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import re
import time
from collections.abc import Callable
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Final, Literal, NamedTuple, TypedDict, cast

import httpx

from lib.config import WordPressConfig
from lib.errors import (
    ERROR_BODY_LIMIT,
    GtinMismatchError,
    MediaIntegrityError,
    MediaOwnershipError,
    MissingCredentialError,
    WordPressAPIError,
)
from lib.logging_setup import scrub_response_body
from lib.multilingual import MultilingualAdapter, make_adapter

_log = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

#: REST base for posts of any (custom) post type (§4.4). Post types must be
#: registered with ``show_in_rest => true`` to appear here.
_WP_API_PREFIX: Final = "/wp-json/wp/v2"
#: Media collection endpoint.
_MEDIA_PATH: Final = f"{_WP_API_PREFIX}/media"
#: Polylang detection route — a 200 means the plugin is active (§4.4).
_PLL_LANGUAGES_PATH: Final = "/wp-json/pll/v1/languages"
#: WPML detection route — its presence means WPML is active (§4.4).
#: WPML's REST namespace root. Probed rather than a concrete endpoint because WPML's routes
#: are admin-gated and version-dependent — the namespace index is the one thing that answers
#: 200 unauthenticated on any WPML site. (The former probe,
#: ``/wp-json/sitepress-multilingual-cms/v1/languages``, is not a namespace WPML registers; it
#: 404s even on a WPML site, so detection always fell through to "none".)
_WPML_PROBE_PATH: Final = "/wp-json/wpml/v1"

#: Results per request when enumerating a post type. WordPress core caps ``per_page`` at 100
#: and answers 400 above it, so this is the ceiling rather than a preference.
_PER_PAGE: Final = 100
#: How many result pages an enumeration will fetch before giving up. 100 × 100 is far past any
#: catalogue this tool publishes, and it stops a paginating loop against a site that answers
#: every page identically — which is what a broken ``page`` param looks like from here.
_PAGE_LIMIT: Final = 100

#: Post ``meta`` key holding the GTIN — the idempotency key for ``upsert_page`` (§6.1).
_GTIN_META_KEY: Final = "gtin"
#: Media ``meta`` key holding the SHA-256 of the uploaded bytes (§6.2). Still stored for
#: diagnostics, but dedup no longer reads it back — see :func:`_content_slug`.
_CONTENT_HASH_META_KEY: Final = "content_sha256"
#: Hex chars of the content SHA-256 folded into a media slug (§6.2 idempotency).
_SLUG_HASH_LEN: Final = 12

# Retry policy (§5.1). 429 and 5xx use independent attempt budgets, matching the
# GS1 client. A WordPress 401/403 is terminal (no token refresh to attempt).
_RETRY_429_MAX_ATTEMPTS: Final = 5
_RETRY_429_BASE_SECONDS: Final = 1.0
_RETRY_429_MAX_SECONDS: Final = 60.0
_RETRY_5XX_MAX_ATTEMPTS: Final = 3
_RETRY_5XX_BASE_SECONDS: Final = 0.5
_RETRY_5XX_MAX_SECONDS: Final = 30.0

#: Default per-operation timeouts (§4.4): read 60s (WordPress renders can be slow).
_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

#: Sentinel status code used when an error originates below HTTP (network error).
_NETWORK_ERROR_STATUS: Final = 0

# HTTP status ranges used in retry/verify classification (bounds exclusive on max).
_HTTP_SUCCESS_MIN: Final = 200
_HTTP_SUCCESS_MAX: Final = 300
_HTTP_REDIRECT_MAX: Final = 400
_HTTP_SERVER_ERROR_MIN: Final = 500
_HTTP_SERVER_ERROR_MAX: Final = 600


# --- Wire shapes (TypedDict per §1: TypedDict for HTTP shapes) ----------------


class WordPressPage(TypedDict, total=False):
    """A WordPress post/page as returned by the REST API (``context=edit``).

    ``title`` and ``content`` are ``{"rendered": ..., "raw": ...}`` objects; ``raw`` is
    only present under ``context=edit``, which the client always requests on lookups so
    ``meta`` and raw content are readable. All fields are optional (``total=False``).
    """

    id: int
    slug: str
    status: str
    type: str
    link: str
    title: dict[str, str]
    content: dict[str, str]
    parent: int
    featured_media: int
    meta: dict[str, object]


class WordPressMedia(TypedDict, total=False):
    """A WordPress media attachment as returned by the REST API.

    ``media_details`` is WordPress's own read of the stored file — for images the original's
    dimensions and ``filesize``, for video the ID3 block. It is how an upload is checked against
    what was sent; it is not guaranteed for every attachment type on every version, hence the
    ``HEAD`` fallback in :meth:`WordPressClient._stored_bytes`.
    """

    id: int
    slug: str
    source_url: str
    title: dict[str, str]
    meta: dict[str, object]
    media_details: dict[str, object]


class MediaUpload(NamedTuple):
    """An attachment id, and whether *this* call is what put it on the site.

    The distinction is what makes rolling back a failed row safe. Dedup means an upload very
    often returns an attachment an earlier run created and a live page is already using, so a
    caller cleaning up after itself must delete only what it actually added. ``created`` is the
    only way to tell those apart from the outside: both are ours, and both look identical.
    """

    media_id: int
    created: bool


class WordPressIdentity(NamedTuple):
    """Who the configured credential authenticates as, and what it may do.

    ``roles`` is only populated under ``context=edit``, which is why :meth:`whoami` asks for
    it: without the roles the answer proves the password works but not that the account can
    still publish, and those fail at very different moments.
    """

    id: int
    slug: str
    roles: list[str]


MultilingualPlugin = Literal["polylang", "wpml", "none"]


class WordPressClient:
    """Synchronous client for the WordPress REST API v2 (§4.4).

    Detects the site's multilingual plugin at construction and selects the matching
    :class:`lib.multilingual.MultilingualAdapter`.

    Args:
        config: The WordPress target configuration for one client.
        timeout: Override the default per-operation timeouts (for tests).
        sleep: Injectable sleep function so retry backoff is instant in tests.
    """

    def __init__(
        self,
        config: WordPressConfig,
        *,
        timeout: httpx.Timeout | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._base_url = config.site_url.rstrip("/")
        self._username = config.username
        self._http = httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT)
        self._sleep = sleep
        self._term_ids: dict[tuple[str, str, str], int | None] = {}
        self.multilingual_plugin: MultilingualPlugin = self._resolve_plugin()
        self._adapter: MultilingualAdapter = make_adapter(
            self.multilingual_plugin,
            wpml_helper_path=config.wpml_helper_path,
            source_language=config.default_language,
        )

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> WordPressClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Auth -----------------------------------------------------------------

    def _auth_header(self) -> dict[str, str]:
        """Return the HTTP Basic ``Authorization`` header (§4.4).

        The application password is read lazily from the environment on every request
        so a missing secret surfaces as :class:`MissingCredentialError` at first use
        (edge E15). The header value is never logged.

        Raises:
            MissingCredentialError: The application-password env var is unset.
        """
        password = _require_env(self.config.app_password_env)
        token = base64.b64encode(f"{self._username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    # -- Public API -----------------------------------------------------------

    def detect_multilingual_plugin(self) -> MultilingualPlugin:
        """Detect which multilingual plugin the site runs, by probing (§4.4).

        Probes the Polylang route first, then WPML's namespace root; a 200 wins.

        Returns:
            ``"polylang"``, ``"wpml"``, or ``"none"`` when neither answers.
        """
        if self._probe(_PLL_LANGUAGES_PATH):
            return "polylang"
        if self._probe(_WPML_PROBE_PATH):
            return "wpml"
        return "none"

    def _resolve_plugin(self) -> MultilingualPlugin:
        """Choose the multilingual plugin: an explicit config value wins over detection (§4.4).

        Detection is a probe, and a probe can be wrong for reasons that have nothing to do
        with the site's real setup — a renamed route, a plugin version change, an
        admin-gated endpoint. When the operator has declared a plugin, that declaration is
        the stronger signal and it wins; detection only supplies the value when the config
        says ``none`` (i.e. "work it out"), and otherwise just warns on a mismatch.

        This ordering matters because the failure is silent: letting a failed probe override
        a configured ``wpml`` swaps in :class:`~lib.multilingual.NoOpAdapter`, whose
        ``link_translations`` does nothing and raises nothing — so every page publishes,
        reports ``ok``, and is simply never linked to its translation.

        Returns:
            The plugin to build the adapter for.
        """
        configured = self.config.multilingual_plugin
        detected = self.detect_multilingual_plugin()
        if configured == "none":
            return detected
        if configured != detected:
            _log.warning(
                "WP multilingual plugin configured as %r but detected %r; using the "
                "configured value",
                configured,
                detected,
            )
        return configured

    def _lang_params(self, language: str | None) -> dict[str, str]:
        """Return the ``lang`` query param scoping a lookup to one language (§4.4).

        On a multilingual site the same slug exists once per language — ``p-{gtin}`` is
        GTIN-derived and carries no language component — and an unscoped collection query
        answers with the **default language only**. Verified against a live WPML site:
        ``?slug=p-X`` returned the nl page while the fr page was invisible.

        Omitting this is not a near miss, it is data loss. Both pages share a ``meta.gtin``
        (same product), so the E8 guard cannot catch it: upserting the fr row finds the nl
        page by slug, the GTIN matches, and the nl page is *overwritten with French* — no fr
        page is created and the row reports ``ok``.

        Sent for Polylang as well as WPML. Only WPML is verified; for Polylang this is at
        worst inert, since WordPress silently ignores query params it does not know.
        """
        if language and self.multilingual_plugin in ("wpml", "polylang"):
            return {"lang": language}
        return {}

    def whoami(self) -> WordPressIdentity:
        """Return the authenticated user, proving the credential *and* its capability (§4.4).

        ``GET /wp/v2/users/me?context=edit``. The ``context=edit`` is load-bearing: a bare
        ``users/me`` answers 200 for any credential that authenticates and omits ``roles``
        entirely, so it cannot tell "the password works" from "the password works *and* the
        account can still publish". A demoted or capability-stripped bot passes the naive
        check and then fails mid-run, after some rows have already published.

        Read-only, and the cheapest way to answer "is this password actually good?" without
        writing anything. ``docs/troubleshooting.md`` documents the same request as a shell
        one-liner; this is that check, in code, so a preflight can make it.

        Raises:
            MissingCredentialError: The application-password env var is unset.
            WordPressAPIError: On a non-2xx response — notably 401 (bad credential) and 403
                (authenticated but lacking edit capability), after retries.
        """
        body = self._request(
            "GET", f"{_WP_API_PREFIX}/users/me", params={"context": "edit"}, label="users/me"
        ).json()
        return WordPressIdentity(
            id=int(body.get("id", 0)),
            slug=str(body.get("slug", "")),
            roles=[str(role) for role in body.get("roles", [])],
        )

    def read_page(self, post_type: str, page_id: int) -> WordPressPage | None:
        """GET one page by id, with **no language parameter**, for reading its ACF fields.

        The distinction is not cosmetic and was measured against the live site. On this WPML
        install, a request carrying ``lang=`` answers with ``acf: []`` — an empty array — for a
        page whose fields are populated and publicly rendering. The language-scoped *listing*
        carries the same flaw, so every French page reads as having no tagline and no
        Eigenschappen. Ask by id, without ``lang``, and the same page returns its fields.

        So a caller checking what the site actually carries must come through here. Listing is
        still how you find the ids — it is only the field read that has to be unscoped.
        ``context=edit`` is harmless; ``lang`` is the one that empties it.

        Read-only.

        Args:
            post_type: The (custom) post type slug.
            page_id: The WordPress post id, from a listing.

        Returns:
            The page, or ``None`` if the id 404s — a stale listing, not an error.
        """
        return self._get_page(post_type, page_id)

    def find_by_slug(
        self, post_type: str, slug: str, language: str | None = None
    ) -> WordPressPage | None:
        """Return the page with ``slug`` under ``post_type``, or ``None`` (§4.4).

        Args:
            post_type: The (custom) post type slug.
            slug: The page slug to look up.
            language: Scope the lookup to this language — **required on a multilingual
                site**, where the same slug exists per language (see :meth:`_lang_params`).

        Raises:
            WordPressAPIError: On a non-2xx response other than 404, after retries.
        """
        pages = self._get_list(
            f"{_WP_API_PREFIX}/{post_type}",
            params={"slug": slug, "context": "edit", **self._lang_params(language)},
            label=f"{post_type}?slug={slug}",
        )
        return cast(WordPressPage, pages[0]) if pages else None

    def term_id(self, taxonomy: str, term: str, language: str) -> int | None:
        """The id of category ``term`` as it exists in ``language``, or ``None`` if it does not.

        WPML keeps one term per language, and a translated term's slug is the original's with the
        language appended — ``keuken`` in nl is ``keuken-fr`` in fr (verified live 2026-10-08). A
        French page given the Dutch term's id would sit in the Dutch category, invisible on the
        French category page, so the lookup is scoped to the page's language and tries both
        spellings there. Read-only, and remembered for the life of this client: a run asks the
        same few terms once per page.
        """
        key = (taxonomy, term, language)
        if key not in self._term_ids:
            found = None
            for slug in (term, f"{term}-{language}"):
                terms = self._get_list(
                    f"{_WP_API_PREFIX}/{taxonomy}",
                    params={"slug": slug, **self._lang_params(language)},
                    label=f"{taxonomy}?slug={slug}",
                )
                if terms:
                    found = int(cast(int, terms[0]["id"]))
                    break
            self._term_ids[key] = found
        return self._term_ids[key]

    def page_terms(self, post_type: str, page_id: int, gtin: str, taxonomy: str) -> list[int]:
        """The term ids ``taxonomy`` gives page ``page_id`` — once it is shown to be ``gtin``'s.

        The ``meta.gtin`` guard is the one :meth:`upsert_page` applies (E8/E11): a page id from
        state can be stale, and a category written onto somebody else's page is a live mistake.

        Raises:
            GtinMismatchError: The page belongs to another product.
            WordPressAPIError: The page does not exist (404), carries no ``meta.gtin``, or the
                read failed.
        """
        page = self._get_page(post_type, page_id)
        if page is None:
            raise WordPressAPIError(
                int(HTTPStatus.NOT_FOUND),
                f"{post_type}/{page_id} does not exist",
                call=f"GET {post_type}/{page_id}",
            )
        self._guard_gtin_match(page, gtin)
        raw = cast(dict[str, object], page).get(taxonomy)
        return [int(cast(int, term)) for term in raw] if isinstance(raw, list) else []

    def set_page_terms(
        self, post_type: str, page_id: int, gtin: str, terms: dict[str, list[int]]
    ) -> None:
        """Set ``{taxonomy: [term id]}`` on an existing page, and change nothing else about it.

        An update carrying only the taxonomy fields: WordPress leaves every field a request does
        not name as it is, so title, content, ACF and status are untouched. Guarded by
        :meth:`page_terms` first, for the same reason.
        """
        for taxonomy in terms:
            self.page_terms(post_type, page_id, gtin, taxonomy)
        self._request(
            "POST",
            f"{_WP_API_PREFIX}/{post_type}/{page_id}",
            json_body=dict(terms),
            label=f"set terms on {post_type}/{page_id}",
        )

    def list_pages_with_gtin(
        self, post_type: str, language: str | None = None, *, page_limit: int = _PAGE_LIMIT
    ) -> list[WordPressPage]:
        """Every page under ``post_type`` carrying a ``meta.gtin``, for one language.

        This is what makes a live-against-state reconciliation possible: state records what
        *this machine* wrote, and only the site can say what is actually there. Read-only —
        a paginated ``GET`` and nothing else.

        Two things it does not trust:

        * **The server's idea of a page of results.** ``per_page`` is capped at 100 by core,
          so a catalogue larger than that needs following pages. It stops at ``page_limit``
          rather than looping forever against a site that answers every page identically.
        * **That every post is ours.** A page with no ``meta.gtin`` was made by a human in
          wp-admin, and reporting it as an unrecorded tool page would make the whole report
          noise. Filtering happens here, on the way out, for the same reason
          :meth:`_find_by_meta_gtin` verifies its own filter: ``meta_key`` is not a core REST
          feature, and a site without an enabler silently returns *everything*.

        Args:
            post_type: The (custom) post type slug.
            language: Scope to one language. **Required on a multilingual site** — an
                unscoped query answers with the default language only, so omitting it here
                would report every translated page as missing from the site.
            page_limit: Safety stop on the number of result pages fetched.

        Returns:
            The matching pages, in the order the site returned them.

        Raises:
            WordPressAPIError: On a non-2xx response other than 404, after retries.
        """
        found: list[WordPressPage] = []
        for number in range(1, page_limit + 1):
            batch = self._get_list(
                f"{_WP_API_PREFIX}/{post_type}",
                params={
                    "per_page": str(_PER_PAGE),
                    "page": str(number),
                    "context": "edit",
                    "orderby": "id",
                    "order": "asc",
                    **self._lang_params(language),
                },
                label=f"{post_type}?page={number}",
            )
            if not batch:
                break
            found.extend(
                cast(WordPressPage, item)
                for item in batch
                if _meta_gtin(cast(dict[str, object] | None, item.get("meta")))
            )
            if len(batch) < _PER_PAGE:
                break
        return found

    def upsert_page(  # noqa: PLR0913 — mirrors the §4.4 signature verbatim
        self,
        post_type: str,
        slug: str,
        title: str,
        content: str,
        language: str,
        featured_media: int | None = None,
        parent: int | None = None,
        meta: dict[str, object] | None = None,
        existing_id: int | None = None,
        acf: dict[str, object] | None = None,
        terms: dict[str, list[int]] | None = None,
    ) -> WordPressPage:
        """Create or update one product page, idempotently (§6.1).

        Lookup order: (1) ``existing_id``, (2) ``slug``, (3) ``meta.gtin`` — the last two
        scoped to ``language`` (see :meth:`_lang_params`). On a match the page is updated in
        place (same id, content replaced); with no match a new page is created carrying
        ``meta.gtin``. Callers must always set ``meta['gtin']`` — it is the idempotency key.

        On a multilingual site a create is issued with ``?lang=``, and ``acf`` is written in
        a **separate** follow-up call. Both are required, and both fail silently otherwise —
        see :meth:`_write_page` and :meth:`_write_acf`.

        Args:
            post_type: The (custom) post type slug.
            slug: The page slug (typically GTIN-derived).
            title: The page title.
            content: The page HTML content. Themes that render from ACF (e.g. Oxygen)
                ignore this — pass ``""`` and supply ``acf`` instead.
            language: The page's language code. Scopes the lookups and, on a multilingual
                site, is what the page is created *as*.
            featured_media: Media id for the featured image, if any.
            parent: Parent page id, if any.
            meta: Post meta; must include ``gtin``.
            existing_id: A known page id to update directly, if available.
            acf: ACF field values to write, if any.
            terms: ``{taxonomy: [term id, ...]}`` to set on the page — it *replaces* the page's
                terms in each taxonomy named, on create and update alike. Ids come from
                :meth:`term_id`, in the page's own language.

        Returns:
            The created or updated :class:`WordPressPage`. When ``acf`` was written, this is
            the response to *that* call, so the returned page reflects the ACF values.

        Raises:
            GtinMismatchError: The matched page's ``meta.gtin`` differs from the row's
                GTIN (edge E8) — the row must be logged and skipped.
            WordPressAPIError: The matched-or-target slug belongs to a non-GTIN page
                (edge E11, a 409-class collision needing human intervention), or any
                other non-2xx response after retries.
        """
        gtin = _meta_gtin(meta)
        found = self._lookup_existing(post_type, slug, gtin, existing_id, language)
        if found is not None:
            self._guard_gtin_match(found, gtin)
        page = self._write_page(
            post_type,
            title,
            content,
            language,
            slug,
            featured_media,
            parent,
            meta,
            page_id=found["id"] if found is not None else None,
            terms=terms,
        )
        if acf:
            page = self._write_acf(post_type, int(page["id"]), acf)
        return page

    def upload_media(
        self, file_path: str | Path, title: str | None = None, *, key: str | None = None
    ) -> MediaUpload:
        """Upload a media file, idempotently by a content-addressed slug (§6.2).

        The slug folds the SHA-256 of the bytes into a base derived from ``title`` (or the
        filename) — see :func:`_content_slug`. Dedup is then a pure slug lookup: a hit means
        the bytes are identical, so the existing id is returned without re-uploading. This
        needs no read-back of stored meta (so it does not depend on the attachment
        ``content_sha256`` meta being exposed in REST) and cannot be defeated by a stale
        same-base attachment squatting the base slug — the two failure modes seen live.

        **A content match is checked for length before it is trusted.** The slug proves what the
        bytes were *meant* to be, not what arrived: an upload cut off mid-transfer leaves a
        fragment stored under the hash of the whole file, and the lookup below would then return
        it forever, so re-running could never repair it. A hit whose stored size disagrees is
        deleted and re-uploaded rather than reused.

        Edge E7 (a source ``image_url`` that 404s or times out) is handled *before*
        this method by the run loop via :meth:`download_image`, which returns ``None``
        so the caller skips featured media and still creates the page.

        **``key`` names the file independently of its title.** The slug used to be seeded from the
        title alone, so renaming a product re-uploaded every image and video it had: the bytes were
        identical but the slug was not, and the lookup missed. That is what renaming Democlient's 38
        products did on 2026-10-22 — 109 duplicate attachments, about 480 MB. With a ``key`` (the
        caller passes the GTIN, plus the language for a video) a new upload is stored under the
        key's slug, and the lookup tries that slug first and the title's slug second, so an
        attachment named the old way is still found while its title has not moved.

        Args:
            file_path: Path to the local media file to upload.
            title: Optional media title, shown in the media library. Seeds the slug when no
                ``key`` is given, and is the fallback lookup when one is.
            key: A name for this file that does not change when the product is renamed.

        Returns:
            The attachment id and whether this call created it — see :class:`MediaUpload`. The
            flag matters to any caller that might have to undo its own work: a reused id belongs
            to an earlier run and is very likely in use by a live page.

        Raises:
            WordPressAPIError: On a non-2xx response after retries.
            MediaIntegrityError: WordPress stored a different number of bytes than were sent.
            MediaOwnershipError: The slug is held by an attachment this tool did not upload.
        """
        path = Path(file_path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        slugs = list(
            dict.fromkeys(
                _content_slug(_media_slug(base, path), digest)
                for base in ((key, title) if key else (title,))
            )
        )

        for slug in slugs:
            existing = self._find_media_by_slug(slug)
            if existing is not None and self._is_whole(existing, path, len(data), slug):
                _log.info(
                    "WP media %r already uploaded (content match), reusing id %s",
                    slug,
                    existing["id"],
                )
                return MediaUpload(existing["id"], created=False)
        return MediaUpload(self._create_media(path, data, title, slugs[0], digest), created=True)

    def download_image(self, url: str) -> bytes | None:
        """Fetch an image URL, returning ``None`` if it is unavailable (edge E7).

        A 404, any other non-2xx status, or a network timeout yields ``None`` (logged
        as a WARNING) so the caller skips featured media and still publishes the page.
        This is the primitive the Phase-6 run loop composes with :meth:`upload_media`.

        Args:
            url: The source image URL from the export.

        Returns:
            The image bytes on success, or ``None`` when the image cannot be fetched.
        """
        try:
            resp = self._http.request("GET", url)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            _log.warning("WP image fetch failed for %s: %r (skipping featured media)", url, exc)
            return None
        if _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_SUCCESS_MAX:
            return resp.content
        _log.warning("WP image fetch for %s -> %d (skipping featured media)", url, resp.status_code)
        return None

    def media_source_url(self, media_id: int) -> str | None:
        """Return the public ``source_url`` of a media attachment, or ``None`` if unavailable.

        Used when the ACF image field takes a URL string rather than an attachment id
        (``media.image_write_shape``). The write shape is confirmed live (page-adapter §3), so
        supporting both shapes makes the choice a config flip rather than a code change.
        """
        try:
            resp = self._request(
                "GET",
                f"{_MEDIA_PATH}/{media_id}",
                params={"context": "edit"},
                label=f"media {media_id}",
            )
        except WordPressAPIError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise
        data = resp.json()
        url = data.get("source_url") if isinstance(data, dict) else None
        return url if isinstance(url, str) else None

    def verify_url(self, url: str) -> bool:
        """Return whether ``url`` resolves to a 2xx/3xx response via HEAD (§4.4, §5.1).

        Args:
            url: The absolute URL to verify (e.g. a published page's public link).

        Returns:
            ``True`` for a 2xx/3xx status.

        Raises:
            WordPressAPIError: For any other status or a network error (no retry).
        """
        try:
            resp = self._http.request("HEAD", url)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            _log.error("WP verify_url network error for %s: %r", url, exc)
            raise WordPressAPIError(
                _NETWORK_ERROR_STATUS,
                f"verify_url network error: {exc!r}",
                call=f"HEAD {url}",
            ) from exc
        if _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_REDIRECT_MAX:
            return True
        _log.error("WP verify_url %s -> %d", url, resp.status_code)
        raise WordPressAPIError(resp.status_code, resp.text, call=f"HEAD {url}")

    def alternate_url(self, url: str, language: str) -> str | None:
        """The ``hreflang`` alternate that the page at ``url`` names for ``language``.

        For a page this tool did not make, the page itself is the only record of its
        translations: WPML prints them as ``<link rel="alternate" hreflang="fr-fr" …>``, and
        prints none for a language the page was never translated into. ``x-default`` is never
        an answer — it names the fallback page, not a translation.

        An unauthenticated public GET (redirects followed), like ``verify_url``: it reads what
        a visitor is served, which is what a GS1 record will send them to.

        Args:
            url: The absolute address of the page to read.
            language: A language code (``fr``); matches ``fr`` and any ``fr-*`` region.

        Returns:
            The alternate's absolute address, or ``None`` when the page names none.

        Raises:
            WordPressAPIError: For a non-2xx response or a network error (no retry).
        """
        call = f"GET {url}"
        try:
            resp = self._http.request("GET", url, follow_redirects=True)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise WordPressAPIError(
                _NETWORK_ERROR_STATUS, f"alternate_url network error: {exc!r}", call=call
            ) from exc
        if not _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_SUCCESS_MAX:
            raise WordPressAPIError(resp.status_code, resp.text, call=call)
        return _hreflang_alternates(resp.text).get(language.lower())

    def link_translations(self, translations: dict[str, int]) -> None:
        """Link per-language page ids as translations of one another (§4.5).

        Delegates to the multilingual adapter selected at construction. A no-op when
        the site has no multilingual plugin.

        Args:
            translations: Mapping of language code to the page id in that language.
        """
        self._adapter.link_translations(self, translations)

    def set_page_status(
        self, post_type: str, page_id: int, *, gtin: str, status: str
    ) -> WordPressPage | None:
        """Set one product page's post status, guarded by its GTIN (§4.4, E8, E11).

        Takes the same GTIN guard as :meth:`delete_page`, for the same reason: a page id
        on its own is just a number, and a stale one addresses somebody else's content
        every bit as validly as the intended page. Drafting a stranger's page is not as
        destructive as deleting it, but it is just as invisible.

        Deliberately **not** routed through :meth:`_write_page`, which hardcodes
        ``status=self.config.post_status`` — the client-wide default that publishes — and
        resends title, content and slug. Sending only ``status`` keeps this a status
        change rather than a rewrite that happens to move the status.

        Idempotent twice over: an id that is already gone is a no-op returning ``None``,
        and a page already at ``status`` is returned unwritten.

        Args:
            post_type: The (custom) post type slug.
            page_id: The page to restatus.
            gtin: The GTIN the caller believes this page carries. Required — the write is
                refused unless ``meta.gtin`` matches it.
            status: The target post status, e.g. ``"draft"`` or ``"publish"``.

        Returns:
            The page as it now stands, or ``None`` if the id was already gone.

        Raises:
            GtinMismatchError: The page's ``meta.gtin`` differs from ``gtin`` (E8).
            WordPressAPIError: The page carries no ``meta.gtin`` (E11), or any other
                non-2xx after retries.
        """
        found = self._get_page(post_type, page_id)
        if found is None:
            _log.info("WP set_page_status %s/%s: already gone", post_type, page_id)
            return None
        self._guard_gtin_match(found, gtin)
        if found.get("status") == status:
            _log.info("WP %s/%s already %s (gtin=%s)", post_type, page_id, status, gtin)
            return found
        resp = self._request(
            "POST",
            f"{_WP_API_PREFIX}/{post_type}/{page_id}",
            json_body={"status": status},
            label=f"status {post_type}/{page_id} -> {status} (gtin={gtin})",
        )
        _log.warning(
            "WP %s/%s status %s -> %s (gtin=%s)",
            post_type,
            page_id,
            found.get("status"),
            status,
            gtin,
        )
        return cast(WordPressPage, resp.json())

    def delete_page(
        self, post_type: str, page_id: int, *, gtin: str, force: bool = True
    ) -> WordPressPage | None:
        """Delete one product page, guarded by its GTIN and idempotent (§4.4, E8, E11).

        Reads the page first and refuses unless its ``meta.gtin`` matches ``gtin``. That
        guard is why this takes a GTIN at all: a page id on its own is just a number, and
        a stale or mistyped one addresses somebody else's content every bit as validly as
        the intended page. Deleting is strictly more destructive than the ``upsert_page``
        overwrite the same guards already cover, so it gets the same guards.

        Idempotent: an id that is already gone (404, or 410 when it was purged between
        the read and the delete) is a no-op returning ``None``, so this is safe to call
        unconditionally from a teardown or a retry.

        Args:
            post_type: The (custom) post type slug.
            page_id: The page to delete.
            gtin: The GTIN the caller believes this page carries. Required — the delete
                is refused unless ``meta.gtin`` matches it.
            force: ``True`` (default) bypasses the trash and deletes permanently.
                ``False`` trashes the page, which keeps its row and appends
                ``__trashed`` to its slug.

        Returns:
            The page as it was immediately before deletion, or ``None`` if it was
            already gone.

        Raises:
            GtinMismatchError: The page's ``meta.gtin`` differs from ``gtin`` (E8).
            WordPressAPIError: The page carries no ``meta.gtin`` (E11, a 409-class
                collision with a non-GTIN page), or any other non-2xx after retries.
        """
        found = self._get_page(post_type, page_id)
        if found is None:
            _log.info("WP delete %s/%s: already gone", post_type, page_id)
            return None
        self._guard_gtin_match(found, gtin)
        try:
            resp = self._request(
                "DELETE",
                f"{_WP_API_PREFIX}/{post_type}/{page_id}",
                params={"force": "true"} if force else None,
                label=f"delete {post_type}/{page_id} (gtin={gtin})",
            )
        except WordPressAPIError as exc:
            if exc.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.GONE):
                return None
            raise
        _log.warning("WP deleted %s/%s (gtin=%s, force=%s)", post_type, page_id, gtin, force)
        return _deleted_page(resp.json())

    def delete_media(self, media_id: int) -> bool:
        """Permanently delete one media attachment; idempotent (§4.4).

        Always force-deletes: WordPress refuses to trash attachments
        (``rest_trash_not_supported``, HTTP 501), so bypassing the trash is the only
        mode there is and a ``force`` flag here could only ever be ``True``.

        **Guarded by ownership, like :meth:`delete_page`.** The attachment is re-read first and
        the delete is refused unless it carries a non-empty ``meta.content_sha256`` — the marker
        :meth:`upload_media` writes on everything it creates. This used to take an id at face
        value, on the reasoning that media has no GTIN to check against. It has something just
        as good, and the gap was not theoretical: the pilot site's media library holds 406
        attachments, of which 366 are the client's own, uploaded long before this tool existed.

        Args:
            media_id: The attachment to delete.

        Returns:
            ``True`` when an attachment was deleted, ``False`` when it was already gone.

        Raises:
            MediaOwnershipError: The attachment exists but this tool did not upload it.
            WordPressAPIError: On any non-2xx response after retries.
        """
        existing = self._get_media(media_id)
        if existing is None:
            return False  # already gone; idempotent
        if not _is_ours(existing):
            raise MediaOwnershipError(media_id, "it carries no content hash from this tool")
        return self._force_delete_media(media_id)

    def _force_delete_media(self, media_id: int) -> bool:
        """Delete an attachment with **no ownership check**; idempotent.

        Only two callers may use this, and both hold proof the public guard would otherwise
        have to go and fetch: :meth:`delete_media`, which has just performed the check, and
        :meth:`_assert_whole`, whose id came out of its own ``POST`` response moments earlier —
        provenance no lookup could improve on, and which the guard would in fact *fail*, because
        the content hash is not written until the finalise call that a bad upload never reaches.
        """
        try:
            self._request(
                "DELETE",
                f"{_MEDIA_PATH}/{media_id}",
                params={"force": "true"},
                label=f"delete media {media_id}",
            )
        except WordPressAPIError as exc:
            if exc.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.GONE):
                return False
            raise
        _log.warning("WP deleted media %s", media_id)
        return True

    def _get_media(self, media_id: int) -> WordPressMedia | None:
        """GET one attachment (``context=edit``, so ``meta`` is readable); ``None`` if gone."""
        try:
            resp = self._request(
                "GET",
                f"{_MEDIA_PATH}/{media_id}",
                params={"context": "edit"},
                label=f"media {media_id}",
            )
        except WordPressAPIError as exc:
            if exc.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.GONE):
                return None
            raise
        return cast(WordPressMedia, resp.json())

    # -- Lookup internals -----------------------------------------------------

    def _lookup_existing(  # noqa: PLR0913 — one param per lookup key, plus its language scope
        self,
        post_type: str,
        slug: str,
        gtin: str | None,
        existing_id: int | None,
        language: str | None,
    ) -> WordPressPage | None:
        """Resolve the existing page by id, then slug, then ``meta.gtin`` (§6.1).

        Both collection lookups are scoped to ``language``: on a multilingual site they
        would otherwise answer for the default language only, and adopt the wrong page
        (see :meth:`_lang_params`). ``existing_id`` needs no scope — an id is unambiguous.
        """
        if existing_id is not None:
            page = self._get_page(post_type, existing_id)
            if page is not None:
                return page
        page = self.find_by_slug(post_type, slug, language)
        if page is not None:
            return page
        if gtin is not None:
            return self._find_by_meta_gtin(post_type, gtin, language)
        return None

    def _guard_gtin_match(self, found: WordPressPage, gtin: str | None) -> None:
        """Enforce the GTIN-ownership guard on a matched page (edges E8, E11).

        Raises:
            GtinMismatchError: The page carries a different ``meta.gtin`` (E8).
            WordPressAPIError: The page carries no ``meta.gtin`` — a collision with a
                non-GTIN page (E11), reported as a 409 needing human intervention.
        """
        if gtin is None:
            return
        existing_gtin = _meta_gtin(found.get("meta"))
        page_id = found.get("id", 0)
        if existing_gtin is None:
            _log.error(
                "WP page %s at this slug has no meta.gtin (non-GTIN page); refusing to "
                "overwrite (E11)",
                page_id,
            )
            raise WordPressAPIError(
                int(HTTPStatus.CONFLICT),
                f"slug collision with non-GTIN WordPress page {page_id}",
            )
        if existing_gtin != gtin:
            _log.error(
                "WP page %s has meta.gtin %r != row GTIN %r; skipping (E8)",
                page_id,
                existing_gtin,
                gtin,
            )
            raise GtinMismatchError(gtin, existing_gtin, page_id)

    def _get_page(self, post_type: str, page_id: int) -> WordPressPage | None:
        """GET one page by id (``context=edit``); ``None`` if it 404s (stale id)."""
        try:
            resp = self._request(
                "GET",
                f"{_WP_API_PREFIX}/{post_type}/{page_id}",
                params={"context": "edit"},
                label=f"{post_type}/{page_id}",
            )
        except WordPressAPIError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise
        return cast(WordPressPage, resp.json())

    def _find_by_meta_gtin(
        self, post_type: str, gtin: str, language: str | None = None
    ) -> WordPressPage | None:
        """GET the page whose ``meta.gtin`` equals ``gtin``, or ``None`` (§6.1).

        The ``meta_key``/``meta_value`` params are **not** core WordPress REST features:
        core silently drops unknown query params rather than erroring, so a site without a
        ``rest_{post_type}_query`` enabler answers this request with an unfiltered page of
        *every* post. Never trust the server to have filtered — verify ``meta.gtin`` on the
        way out. Taking ``pages[0]`` on an unfiltered list returns an arbitrary unrelated
        page, which the E8/E11 guards in :meth:`_guard_gtin_match` then reject, turning
        every would-be create into a bogus "slug collision" error.

        Returning ``None`` when nothing matches is also the right fallback on a site with no
        enabler: the caller creates the page instead of adopting the wrong one.
        """
        pages = self._get_list(
            f"{_WP_API_PREFIX}/{post_type}",
            params={
                "meta_key": _GTIN_META_KEY,
                "meta_value": gtin,
                "context": "edit",
                **self._lang_params(language),
            },
            label=f"{post_type}?meta.gtin={gtin}",
        )
        for page in pages:
            meta = page.get("meta")
            if isinstance(meta, dict) and _meta_gtin(meta) == gtin:
                return cast(WordPressPage, page)
        return None

    def _find_media_by_slug(self, slug: str) -> WordPressMedia | None:
        """GET the media item at ``slug``, or ``None`` (§6.2)."""
        items = self._get_list(
            _MEDIA_PATH, params={"slug": slug, "context": "edit"}, label=f"media?slug={slug}"
        )
        return cast(WordPressMedia, items[0]) if items else None

    def _get_list(
        self, path: str, *, params: dict[str, str], label: str
    ) -> list[dict[str, object]]:
        """GET a collection endpoint, returning the JSON list (``[]`` on 404)."""
        try:
            resp = self._request("GET", path, params=params, label=label)
        except WordPressAPIError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                return []
            raise
        data = resp.json()
        return cast(list[dict[str, object]], data) if isinstance(data, list) else []

    # -- Write internals ------------------------------------------------------

    def _write_page(  # noqa: PLR0913 — assembles the full §4.4 create/update body
        self,
        post_type: str,
        title: str,
        content: str,
        language: str,
        slug: str,
        featured_media: int | None,
        parent: int | None,
        meta: dict[str, object] | None,
        *,
        page_id: int | None,
        terms: dict[str, list[int]] | None = None,
    ) -> WordPressPage:
        """POST a create (``page_id is None``) or update to the post-type endpoint.

        A create on a multilingual site carries ``?lang=`` so the page is created **as** that
        language. This is what keeps the slug intact: ``slug_pattern`` is GTIN-derived and
        identical across languages, so a page created without a language collides with its
        sibling and WordPress silently appends ``-2``. The French page would then live at
        ``/fr/…/p-{gtin}-2/`` while ``target_url_pattern`` builds ``/fr/…/p-{gtin}/`` — and
        the GS1 resolver would point every French QR at a 404. Verified live: with ``?lang=``
        both pages keep ``p-{gtin}``.

        Deliberately no ``acf`` here — see :meth:`_write_acf`.
        """
        body: dict[str, object] = {
            "title": title,
            "content": content,
            "status": self.config.post_status,
            "slug": slug,
        }
        if meta is not None:
            body["meta"] = meta
        if featured_media is not None:
            body["featured_media"] = featured_media
        if parent is not None:
            body["parent"] = parent
        for taxonomy, ids in (terms or {}).items():
            body[taxonomy] = ids
        if self.multilingual_plugin == "polylang":
            body["lang"] = language

        if page_id is None:
            path = f"{_WP_API_PREFIX}/{post_type}"
            label = f"create {post_type} {slug}"
            # Only on create: an existing page already has its language, and re-asserting it
            # here is not this method's job (translation linking owns it).
            params = self._lang_params(language)
        else:
            path = f"{_WP_API_PREFIX}/{post_type}/{page_id}"
            label = f"update {post_type}/{page_id}"
            params = {}
        resp = self._request("POST", path, params=params or None, json_body=body, label=label)
        return cast(WordPressPage, resp.json())

    def _write_acf(self, post_type: str, page_id: int, acf: dict[str, object]) -> WordPressPage:
        """Write ACF field values in their own call, after the page exists (§3.1).

        ACF cannot ride along on a create that carries ``?lang=``: the values are **silently
        dropped** — ``201 Created``, fields empty, no error. Measured three ways against the
        live site: create without ``?lang`` + acf persists; create with ``?lang=fr`` + acf is
        empty; create with ``?lang=fr`` *then* acf in a second call persists. Unhandled, this
        yields pages with a correct URL and no content, which ``verify_url`` passes as ok.

        Done for updates too, not just creates: one path is easier to keep correct than two,
        and the extra call is cheap next to publishing a blank page.
        """
        resp = self._request(
            "POST",
            f"{_WP_API_PREFIX}/{post_type}/{page_id}",
            json_body={"acf": acf},
            label=f"acf {post_type}/{page_id} ({', '.join(sorted(acf))})",
        )
        return cast(WordPressPage, resp.json())

    def _create_media(
        self, path: Path, data: bytes, title: str | None, slug: str, digest: str
    ) -> int:
        """Upload media bytes, then set its slug/title and content-hash meta (§6.2)."""
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Name the stored file after the content-addressed slug so re-uploads of the same
        # bytes reuse (before reaching here) and the physical filename never churns -N suffixes.
        filename = f"{slug}{path.suffix}"
        # multipart/form-data, not a raw body with Content-Disposition. Both are accepted by
        # WordPress, but a security plugin on a live site inspected the raw form and refused an
        # ordinary H.264 video with a bare HTML 403 — while the identical bytes sent as multipart
        # were accepted. The raw form put arbitrary binary where a scanner reads a request body;
        # multipart is also the conventional way to post to /wp/v2/media.
        label = f"upload media {slug}"
        resp = self._request(
            "POST", _MEDIA_PATH, files={"file": (filename, data, mime)}, label=label
        )
        media = cast(WordPressMedia, resp.json())
        media_id = media["id"]
        # Before the finalise call, not after: the finalise is what claims the content-addressed
        # slug, and a fragment that holds that slug is returned by every later run as a content
        # match. Checking here means a truncated upload never becomes the answer to its own hash.
        self._assert_whole(media, media_id, path, len(data), call=f"POST {_MEDIA_PATH} ({label})")
        update_body: dict[str, object] = {
            "slug": slug,
            "meta": {_CONTENT_HASH_META_KEY: digest},
        }
        if title is not None:
            update_body["title"] = title
        self._request(
            "POST",
            f"{_MEDIA_PATH}/{media_id}",
            json_body=update_body,
            label=f"finalise media {media_id}",
        )
        return media_id

    def _is_whole(self, existing: WordPressMedia, path: Path, sent: int, slug: str) -> bool:
        """Whether a deduped attachment may be reused, discarding it if it is a fragment.

        The counterpart to :meth:`_assert_whole` on the path that does *no* upload. It returns
        rather than raises, because the caller has the bytes in hand and can simply upload them
        again — a stale fragment is repairable here, and only here.

        Unverifiable is reuse, not deletion. ``_stored_bytes`` returns ``None`` when nothing will
        say how large the file is, and deleting on a number nobody supplied would remove a live
        page's media on a guess. The bytes match by hash; absent evidence to the contrary, the
        attachment is the file.

        **Ownership is checked before anything is deleted here**, unlike on the create path: this
        id came from a slug *lookup*, so it is whatever the site had under that slug, not
        something this call minted. The lookup already fetched ``meta`` (``context=edit``), so the
        check costs no request. A same-slug attachment that is not ours is a collision with the
        client's own library — vanishingly unlikely given the slug embeds a SHA-256 prefix, and
        exactly the case where guessing is unacceptable.
        """
        media_id = existing["id"]
        stored = self._stored_bytes(existing)
        if stored is None or stored == sent:
            return True
        if not _is_ours(existing):
            raise MediaOwnershipError(
                media_id, f"it holds {stored} bytes rather than {sent}, but is not ours to replace"
            )
        _log.warning(
            "WP media %r matches by content hash but holds %d bytes, not %d — deleting the "
            "fragment (attachment %s) and re-uploading %s",
            slug,
            stored,
            sent,
            media_id,
            path.name,
        )
        if self._discard_media(media_id):
            return False
        raise MediaIntegrityError(
            path,
            sent,
            stored,
            media_id,
            deleted=False,
            call=f"DELETE {_MEDIA_PATH}/{media_id} (discard fragment {slug})",
        )

    def _assert_whole(  # noqa: PLR0913 — every argument is part of the failure it reports
        self,
        media: WordPressMedia,
        media_id: int,
        path: Path,
        sent: int,
        *,
        call: str,
    ) -> None:
        """Delete and raise if WordPress stored fewer (or more) bytes than were sent.

        A live upload was cut off mid-transfer and answered ``201`` anyway, leaving a 1.5 MB
        fragment of an 8 MB video that WordPress then served happily. A half-uploaded video is
        worse than a failed one: the page publishes, the QR resolves, and the product looks fine
        until someone presses play.
        """
        stored = self._stored_bytes(media)
        if stored is None:
            _log.warning(
                "WP stored size of media %s (%s) could not be established; upload unverified",
                media_id,
                path.name,
            )
            return
        if stored == sent:
            return
        _log.error(
            "WP stored %d bytes of %s but %d were sent; deleting attachment %s",
            stored,
            path.name,
            sent,
            media_id,
        )
        raise MediaIntegrityError(
            path,
            sent,
            stored,
            media_id,
            deleted=self._discard_media(media_id),
            call=call,
        )

    def _stored_bytes(self, media: WordPressMedia) -> int | None:
        """How many bytes WordPress holds for an attachment, or ``None`` if it will not say.

        ``media_details.filesize`` is the cheap answer — it rides along on the create response,
        and it is the size of the *original* file rather than of any derivative. It is not
        guaranteed for every attachment type on every WordPress version, so a ``HEAD`` on the
        public ``source_url`` is the fallback: the webserver's ``Content-Length`` is an
        independent measurement, and does not depend on WordPress having parsed the file.

        ``None`` is a real answer and callers must treat it as one — "unverified", never "fine".
        Deleting on a number nobody supplied would be deleting on inference, and
        :meth:`delete_media` has no ownership guard.
        """
        details = media.get("media_details")
        if isinstance(details, dict):
            filesize = details.get("filesize")
            if isinstance(filesize, int):
                return filesize
        source_url = media.get("source_url")
        if isinstance(source_url, str) and source_url:
            return self._content_length(source_url)
        return None

    def _content_length(self, url: str) -> int | None:
        """``Content-Length`` of ``url`` via HEAD, or ``None`` if it cannot be established.

        Unauthenticated and deliberately forgiving: this is a second opinion, and a proxy that
        answers without the header, or does not answer at all, must not turn into an upload
        failure. It only ever adds certainty.
        """
        try:
            resp = self._http.request("HEAD", url)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            _log.warning("WP HEAD %s failed (%r); upload size unverified", url, exc)
            return None
        if not _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_SUCCESS_MAX:
            _log.warning("WP HEAD %s -> %d; upload size unverified", url, resp.status_code)
            return None
        try:
            return int(resp.headers["Content-Length"])
        except (KeyError, ValueError):
            _log.warning("WP HEAD %s gave no usable Content-Length", url)
            return None

    def _discard_media(self, media_id: int) -> bool:
        """Delete an attachment we know is corrupt, without masking why we are deleting it.

        A delete raises on any non-2xx, and that exception would replace the integrity failure it
        is cleaning up after — leaving the operator with a delete error and no statement of what
        was wrong with the file.

        Goes through :meth:`_force_delete_media`: both callers have *already* established that
        the attachment is theirs — by provenance on the create path, by an explicit
        :func:`_is_ours` check on the dedup path — so re-fetching it here would only add a request
        and, on the create path, would wrongly refuse.
        """
        try:
            return self._force_delete_media(media_id)
        except WordPressAPIError as exc:
            _log.error("WP could not delete corrupt media %s: %r", media_id, exc)
            return False

    def _probe(self, path: str) -> bool:
        """Return whether a GET to ``path`` succeeds (2xx) — used for plugin detection."""
        try:
            self._request("GET", path, label=f"detect {path}")
        except WordPressAPIError:
            return False
        return True

    # -- HTTP core ------------------------------------------------------------

    def _request(  # noqa: PLR0913 — request knobs kept explicit and keyword-only
        self,
        method: str,
        path: str,
        *,
        label: str,
        params: dict[str, str] | None = None,
        json_body: object = None,
        content: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Issue one HTTP call with the retry policy in §5.1.

        Args:
            method: HTTP method.
            path: Path (with any leading prefix), appended to the base URL.
            label: Short label used only for logging.
            params: Query-string parameters.
            json_body: JSON-serialisable request body, if any.
            content: Raw byte body (media upload), mutually exclusive with ``json_body``.
            extra_headers: Additional headers (e.g. ``Content-Disposition``).

        Returns:
            The successful (2xx) response.

        Raises:
            WordPressAPIError: On a terminal non-2xx response or exhausted retries.
                Callers that treat 404 as not-found (lookups) catch this.
        """
        url = self._base_url + path
        endpoint = f"{method} {path}"
        attempts_429 = 0
        attempts_5xx = 0

        while True:
            headers = dict(self._auth_header())
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)
            started = time.monotonic()
            try:
                resp = self._send(method, url, params, json_body, content, headers, files)
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                attempts_5xx += 1
                if attempts_5xx >= _RETRY_5XX_MAX_ATTEMPTS:
                    _log.error("WP %s (%s) network error, giving up: %r", endpoint, label, exc)
                    raise WordPressAPIError(
                        _NETWORK_ERROR_STATUS,
                        f"network error: {exc!r}",
                        call=f"{endpoint} ({label})",
                    ) from exc
                backoff = _backoff_5xx(attempts_5xx)
                _log.warning(
                    "WP %s (%s) network error, retry %d/%d in %.1fs: %r",
                    endpoint,
                    label,
                    attempts_5xx,
                    _RETRY_5XX_MAX_ATTEMPTS,
                    backoff,
                    exc,
                )
                self._sleep(backoff)
                continue

            status = resp.status_code
            elapsed_ms = (time.monotonic() - started) * 1000

            if _HTTP_SUCCESS_MIN <= status < _HTTP_SUCCESS_MAX:
                _log.info("WP %s (%s) -> %d in %.0fms", endpoint, label, status, elapsed_ms)
                return resp

            if status == HTTPStatus.TOO_MANY_REQUESTS:
                attempts_429 += 1
                if attempts_429 >= _RETRY_429_MAX_ATTEMPTS:
                    raise self._api_error(resp, endpoint, label, final=True)
                backoff = _backoff_429(attempts_429, _retry_after_seconds(resp))
                _log.warning(
                    "WP %s (%s) -> 429, retry %d/%d in %.1fs",
                    endpoint,
                    label,
                    attempts_429,
                    _RETRY_429_MAX_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue

            if _HTTP_SERVER_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MAX:
                attempts_5xx += 1
                if attempts_5xx >= _RETRY_5XX_MAX_ATTEMPTS:
                    raise self._api_error(resp, endpoint, label, final=True)
                backoff = _backoff_5xx(attempts_5xx)
                _log.warning(
                    "WP %s (%s) -> %d, retry %d/%d in %.1fs",
                    endpoint,
                    label,
                    status,
                    attempts_5xx,
                    _RETRY_5XX_MAX_ATTEMPTS,
                    backoff,
                )
                self._sleep(backoff)
                continue

            # Any other 4xx (400/401/403/404/409): terminal per §5.1.
            raise self._api_error(resp, endpoint, label, final=True)

    def _send(  # noqa: PLR0913 — thin transport shim; args mirror httpx.request
        self,
        method: str,
        url: str,
        params: dict[str, str] | None,
        json_body: object,
        content: bytes | None,
        headers: dict[str, str],
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Issue the underlying HTTP request (multipart xor json xor raw content)."""
        if files is not None:
            return self._http.request(method, url, params=params, files=files, headers=headers)
        if content is not None:
            return self._http.request(method, url, params=params, content=content, headers=headers)
        if json_body is not None:
            return self._http.request(method, url, params=params, json=json_body, headers=headers)
        return self._http.request(method, url, params=params, headers=headers)

    def _api_error(
        self, resp: httpx.Response, endpoint: str, label: str, *, final: bool
    ) -> WordPressAPIError:
        """Build a :class:`WordPressAPIError`, logging it scrubbed (§5.2)."""
        body = resp.text
        error = WordPressAPIError(resp.status_code, body, call=f"{endpoint} ({label})")
        if final:
            if resp.status_code == HTTPStatus.NOT_FOUND:
                _log.info("WP %s (%s) -> not found (404)", endpoint, label)
            else:
                _log.error(
                    "WP %s (%s) -> %d; body=%s",
                    endpoint,
                    label,
                    resp.status_code,
                    scrub_response_body(body)[:ERROR_BODY_LIMIT],
                )
        return error


# --- Module helpers ----------------------------------------------------------


class _AlternateLinks(HTMLParser):
    """Collects ``<link rel="alternate" hreflang=…>`` tags, keyed by bare language code."""

    def __init__(self) -> None:
        super().__init__()
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        attr = {key: value or "" for key, value in attrs}
        if "alternate" not in attr.get("rel", "").lower().split():
            return
        hreflang = attr.get("hreflang", "").lower()
        href = attr.get("href", "")
        if not hreflang or hreflang == "x-default" or not href:
            return
        # First one wins: a page naming two regions of one language (fr-fr, fr-be) is
        # already a choice the site made in that order.
        self.found.setdefault(hreflang.split("-", 1)[0], href)


def _hreflang_alternates(html: str) -> dict[str, str]:
    """``{language: href}`` for every hreflang alternate in ``html`` (``x-default`` excluded)."""
    parser = _AlternateLinks()
    parser.feed(html)
    return parser.found


def _require_env(name: str) -> str:
    """Read an environment variable, raising a typed error if it is unset."""
    try:
        return os.environ[name]
    except KeyError as exc:
        raise MissingCredentialError(f"Environment variable {name!r} is not set") from exc


def _meta_gtin(meta: dict[str, object] | None) -> str | None:
    """Extract a string ``gtin`` from a post ``meta`` mapping, if present."""
    if not meta:
        return None
    value = meta.get(_GTIN_META_KEY)
    return str(value) if value is not None and value != "" else None


def _deleted_page(body: object) -> WordPressPage | None:
    """Unwrap a DELETE response into the page as it was before deletion.

    The shape depends on the mode: a force-delete answers
    ``{"deleted": true, "previous": {...}}``, while a trash answers with the trashed
    post itself. Casting the force-delete body straight to a page would yield a dict
    with no ``id``, and :class:`WordPressPage` is ``total=False``, so nothing would
    complain until a caller read a field that was never there.
    """
    if not isinstance(body, dict):
        return None
    if "previous" in body:
        return cast(WordPressPage, body["previous"])
    return cast(WordPressPage, body)


def _is_ours(media: WordPressMedia) -> bool:
    """Whether this tool uploaded an attachment, from the content hash it stamps on its own.

    ``upload_media`` writes ``meta.content_sha256`` on every attachment it creates. On the pilot
    site the key is registered site-wide, so it is *present* on all 406 attachments and non-empty
    on only the 40 that came from here — which makes "non-empty" the test, not "present".

    Anything unreadable or empty answers ``False``. That is the safe direction: at worst this
    declines to delete an orphan of our own, which is recoverable, rather than deleting a
    client's product photo, which is not.
    """
    meta = media.get("meta")
    if not isinstance(meta, dict):
        return False
    digest = meta.get(_CONTENT_HASH_META_KEY)
    return isinstance(digest, str) and digest != ""


def _content_slug(base: str, digest: str) -> str:
    """A content-addressed media slug: the base slug plus a prefix of the content hash.

    Folding the hash into the slug makes dedup a pure slug lookup — identical bytes always map
    to the same slug and different bytes to a different one. So :meth:`WordPressClient.upload_media`
    needs no read-back of the stored ``content_sha256`` meta (it works whether or not that meta is
    exposed in REST), and a stale attachment sharing only the *base* slug cannot shadow the match.
    """
    return f"{base}-{digest[:_SLUG_HASH_LEN]}"


def _media_slug(title: str | None, path: Path) -> str:
    """Derive a deterministic base media slug from the title (or filename) (§6.2)."""
    source = title if title else path.stem
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug or "media"


def _backoff_429(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, _RETRY_429_MAX_SECONDS)
    return min(_RETRY_429_BASE_SECONDS * 2.0 ** (attempt - 1), _RETRY_429_MAX_SECONDS)


def _backoff_5xx(attempt: int) -> float:
    return min(_RETRY_5XX_BASE_SECONDS * 2.0 ** (attempt - 1), _RETRY_5XX_MAX_SECONDS)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header expressed in seconds; ignore HTTP-dates."""
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
