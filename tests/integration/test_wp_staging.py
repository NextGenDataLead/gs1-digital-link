"""Live staging-WordPress smoke tests for the DoD (IMPLEMENTATION_SPEC §12 Phase 4).

These are the staging-gated Definition-of-Done checks that cannot run against a mock:
§6.1 (upsert idempotency), §6.2 (media idempotency), plugin detection, and the exit gate
"published page returns 200 at its URL". They are marked ``staging`` and skipped unless
the staging environment is configured, so CI stays green on mocks.

**These write to a live WordPress site.** Everything they create is cleaned up: pages are
force-deleted and media attachments removed in a ``finally``, so a failed assertion still
tears down. Three of the four tests create *drafts*; ``test_published_page_resolves``
cannot, because an unauthenticated HEAD of a draft is a 404 and publishing is the very
thing it asserts — so it publishes, and force-deletes in its own ``finally``.

``STAGING_GTIN`` has **no default** and must be a GTIN dedicated to smoke testing — not a
saleable product. Two guards enforce it, and neither is sufficient alone: the GTIN must
sit in Democlient's company prefix, *and* a pre-flight refuses to run if a page already
exists for it that this suite did not create. Without the second, a real product's GTIN
would let the upsert adopt its live page, overwrite it, and let teardown delete it — with
every ownership guard passing, because the GTIN would genuinely match.

Nothing auto-loads ``.env``, so export the variables (``set -a; source .env; set +a``)
before running. **Single-quote ``DEMOCLIENT_WP_APP_PASS``**: WordPress app passwords
contain spaces, so an unquoted value breaks ``source`` at the first space and loads
*empty* — every call then 401s, looking like a permissions problem::

    WP_STAGING_URL=https://staging.democlient.nl \\
    WP_STAGING_USER=gs1-publisher \\
    DEMOCLIENT_WP_APP_PASS='xxxx xxxx xxxx xxxx' \\
    STAGING_GTIN=02987653XXXXXX \\
    pytest -m staging

Optional overrides: ``WP_STAGING_POST_TYPE`` (default ``democlient``),
``WP_STAGING_APP_PASS_ENV`` (default ``DEMOCLIENT_WP_APP_PASS``), ``STAGING_GTIN_PREFIX``
(default ``2987653``).
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from lib.config import WordPressConfig
from lib.wp_client import WordPressClient, WordPressPage

_URL = os.environ.get("WP_STAGING_URL")
_USER = os.environ.get("WP_STAGING_USER")
_APP_PASS_ENV = os.environ.get("WP_STAGING_APP_PASS_ENV", "DEMOCLIENT_WP_APP_PASS")
_POST_TYPE = os.environ.get("WP_STAGING_POST_TYPE", "democlient")

#: No default: an unset GTIN skips the suite rather than writing to an arbitrary one.
_GTIN = os.environ.get("STAGING_GTIN")

#: Democlient's GS1 company prefix. Overridable for a pilot on another prefix.
_GTIN_PREFIX = os.environ.get("STAGING_GTIN_PREFIX", "2987653")

#: The export's own product list — the authoritative answer to "is this a real product?".
#: Written by parse_export and gitignored, so it may be absent; resolved at import because
#: tests chdir. See _assert_gtin_not_a_real_product for why this gate is not optional.
_PRODUCTS_JSON = Path(
    os.environ.get("STAGING_PRODUCTS_JSON", "output/democlient/data/products.json")
).resolve()

_SLUG = f"p-{_GTIN}"

#: The title every page this suite creates carries. The pre-flight uses it to tell our own
#: leftovers (safe to reuse and delete) from someone else's page (abort, touch nothing).
_SMOKE_TITLE = "Smoke test product"

# A 1x1 transparent PNG — small, valid image bytes for the media round-trip.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_STAGING_READY = bool(_URL and _USER and os.environ.get(_APP_PASS_ENV) and _GTIN)

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        not _STAGING_READY,
        reason="staging WP not configured (set WP_STAGING_URL, WP_STAGING_USER, "
        f"{_APP_PASS_ENV}, and STAGING_GTIN)",
    ),
]


def _config(*, post_status: str) -> WordPressConfig:
    return WordPressConfig(
        site_url=_URL or "",
        username=_USER or "",
        app_password_env=_APP_PASS_ENV,
        post_type=_POST_TYPE,
        post_status=post_status,
        multilingual_plugin="polylang",
        default_language="nl",
        languages=["nl", "fr"],
    )


def _assert_gtin_not_a_real_product() -> None:
    """Refuse a GTIN that the export knows as a real product.

    The two other gates are jointly insufficient, and this is not theoretical — verified
    against `02987653000374` (*Kledingroller*), which passes both: it sits in the 2987653
    prefix, and the live pre-flight finds nothing, because its real page (id 293,
    *"Roll off"*) uses a human slug and carries **no** ``meta.gtin``. Neither the slug
    lookup nor the meta lookup can see it, so the suite would cheerfully create a second,
    duplicate page for a product that already has one. Only the product list catches that.

    Resolved at import, before any test chdirs. Fails loudly when the file is missing
    rather than skipping the check: a guard whose absence you cannot detect is not a guard.
    """
    if not _PRODUCTS_JSON.is_file():
        pytest.fail(
            f"{_PRODUCTS_JSON} not found, so STAGING_GTIN cannot be checked against the "
            f"real product list. Run parse_export, or point STAGING_PRODUCTS_JSON at it. "
            f"Refusing to run without it — the prefix and pre-flight gates both pass a "
            f"real product whose page uses a human slug and has no meta.gtin."
        )
    records = json.loads(_PRODUCTS_JSON.read_text(encoding="utf-8"))
    real = {str(r["gtin"]).zfill(14) for r in records}
    assert _GTIN and _GTIN.zfill(14) not in real, (
        f"STAGING_GTIN={_GTIN!r} is a real product in the export ({len(real)} products). "
        f"Refusing to run: this suite writes to it and then deletes it. Use a GTIN that is "
        f"not an active product."
    )


def _assert_gtin_prefix() -> None:
    """Refuse a GTIN outside Democlient's company prefix, before any HTTP happens.

    Pure and client-free on purpose: it runs before a client is even constructed, so a
    misconfigured GTIN never reaches the network at all.
    """
    # zfill(14)[1:8] is the company prefix of a GTIN-13 carrying indicator digit 0. A
    # GTIN-14 with a non-zero indicator (a trade-item grouping) is rejected here; that is
    # fine for product pages and not worth debugging twice.
    assert _GTIN and _GTIN.zfill(14)[1:8] == _GTIN_PREFIX, (
        f"STAGING_GTIN={_GTIN!r} is not in the {_GTIN_PREFIX} company prefix; refusing to "
        f"write to a GTIN Democlient may not own (override with STAGING_GTIN_PREFIX)"
    )


def _assert_no_foreign_page(wp: WordPressClient) -> None:
    """Refuse to run if the GTIN already has a page this suite did not create.

    The prefix check alone does not make a GTIN disposable — every real Democlient product
    shares that prefix too. This is the check that catches a real saleable product.

    It deliberately reuses ``_lookup_existing``, the *same* resolution ``upsert_page`` will
    perform, rather than a lookup of its own: a guard that disagrees with the write about
    which page is at stake is not a guard. If a page comes back that this suite did not
    title, the GTIN belongs to real content — the upsert would adopt and overwrite it, and
    teardown would then delete it with every ownership guard passing, because the GTIN
    really would match.
    """
    existing = wp._lookup_existing(_POST_TYPE, _SLUG, _GTIN, None, "nl")  # noqa: SLF001
    if existing is not None and existing.get("title", {}).get("rendered") != _SMOKE_TITLE:
        pytest.fail(
            f"STAGING_GTIN={_GTIN!r} already has WordPress page {existing.get('id')} that "
            f"this suite did not create — it looks like real content. Refusing to run: the "
            f"upsert would overwrite it and teardown would delete it. Use a GTIN that is "
            f"not an active product."
        )


def _delete_smoke_page(wp: WordPressClient) -> None:
    """Force-delete the page this suite created, if it got as far as creating one.

    Found by its deterministic slug rather than a captured id: the fixture never sees the
    ids the tests created, and a slug lookup also clears leftovers from an earlier run
    that crashed before its own teardown.
    """
    page = wp.find_by_slug(_POST_TYPE, _SLUG, "nl")
    if page is not None:
        wp.delete_page(_POST_TYPE, page["id"], gtin=_GTIN or "")


@pytest.fixture(scope="module")
def client() -> Iterator[WordPressClient]:
    # Both gates before the client exists, so a bad GTIN issues no HTTP at all.
    _assert_gtin_prefix()
    _assert_gtin_not_a_real_product()
    wp = WordPressClient(_config(post_status="draft"))
    try:
        # Pre-flight before the cleanup path is armed: if the GTIN turns out to address
        # real content we must neither write to it nor delete it, so an abort here has to
        # skip the teardown below, not just the tests.
        _assert_no_foreign_page(wp)
        try:
            yield wp
        finally:
            _delete_smoke_page(wp)
    finally:
        wp.close()


def _upsert_smoke_page(client: WordPressClient) -> WordPressPage:
    return client.upsert_page(
        _POST_TYPE,
        _SLUG,
        _SMOKE_TITLE,
        "<p>Smoke test content.</p>",
        "nl",
        meta={"gtin": _GTIN, "brand": "SmokeTest"},
    )


def test_detects_polylang(client: WordPressClient) -> None:
    # DoD: multilingual detection returns the correct value on Polylang staging.
    assert client.detect_multilingual_plugin() == "polylang"


def test_upsert_is_idempotent(client: WordPressClient) -> None:
    # DoD §6.1: two identical upserts converge on the same page id.
    first = _upsert_smoke_page(client)
    second = _upsert_smoke_page(client)
    assert first["id"] == second["id"]


def test_upload_media_is_idempotent(client: WordPressClient, tmp_path: Path) -> None:
    # DoD §6.2: uploading the same file twice yields one media asset.
    img = tmp_path / "smoke.png"
    img.write_bytes(_PNG_1X1)
    uploaded: list[int] = []
    try:
        uploaded.append(client.upload_media(img, title="Smoke test image"))
        uploaded.append(client.upload_media(img, title="Smoke test image"))
        assert uploaded[0] == uploaded[1]
    finally:
        for media_id in dict.fromkeys(uploaded):  # deduped: both ids match when it passes
            client.delete_media(media_id)


def test_published_page_resolves(client: WordPressClient) -> None:
    """DoD exit gate: the published page lives at its URL and returns 200/3xx.

    The one test here that cannot use a draft — ``verify_url`` issues an *unauthenticated*
    HEAD, which a draft answers with 404, so publishing is precisely what is under test.
    It therefore gets its own publishing client, and the page is live only for the one
    HEAD between the upsert and the ``finally``. The module fixture's teardown would catch
    the page anyway (same slug); deleting it here just closes the window sooner.
    """
    with WordPressClient(_config(post_status="publish")) as wp:
        page: WordPressPage | None = None
        try:
            page = _upsert_smoke_page(wp)
            link = page.get("link")
            assert isinstance(link, str) and link
            assert wp.verify_url(link) is True
        finally:
            if page is not None:
                wp.delete_page(_POST_TYPE, page["id"], gtin=_GTIN or "")
