"""Live end-to-end run_execute DoD check (IMPLEMENTATION_SPEC §12 Phase 6, §6.5).

This is the executable form of the Phase 6 exit gate — "``run_execute.py`` completes for
one GTIN end-to-end against staging" — plus the live §6.5 idempotency contract. It drives
``scripts.run_execute.main`` against a **real WordPress** site and the **GS1 production**
environment (the GS1 sandbox account has no Digital Link contract, so production is the
only environment that resolves — a project decision). It is marked ``staging`` and skipped
unless both targets are configured, so CI stays green on mocks.

**Read this before running it.**

The WordPress page **is published, not drafted** — ``verify_url`` issues an
*unauthenticated* HEAD, which a draft answers with 404, so the run would fail rather than
be made safe by drafting. The page is therefore live between its creation and the
force-delete in the teardown, and is cleaned up whatever the test did or failed at.

**The GS1 production entry cannot be deleted.** The v2 API has no DELETE for a Digital
Link. Teardown retracts it — disables it via ``activationStatus``, deliberately leaving
its links in place (see :meth:`lib.gs1_dl_client.GS1DigitalLinkClient.retract`) — which is
the most that can be done, and leaves a dead, disabled record on Democlient's account
**forever**. That is a property of the GS1 API, not of this code.

For that reason ``STAGING_GTIN`` has **no default** and must be a GTIN dedicated to smoke
testing and nothing else. Two guards enforce it, and neither suffices alone: the GTIN must
sit in Democlient's company prefix, *and* a pre-flight refuses to run if a page already
exists for it that this test did not create. Without the second, a real product's GTIN
would let the upsert adopt its live page, overwrite it with the smoke content, and let
teardown delete it — with every ownership guard passing, because the GTIN would genuinely
match.

Nothing auto-loads ``.env``, so export the variables (``set -a; source .env; set +a``)
before running. **Single-quote ``DEMOCLIENT_WP_APP_PASS``**: WordPress app passwords
contain spaces, so an unquoted value breaks ``source`` at the first space and loads
*empty* — the run then fails with blank credentials rather than a clear error. The secrets
reuse the canonical names from ``.env.example``; the rest is non-secret runner config::

    WP_STAGING_URL=https://staging.democlient.nl \\
    WP_STAGING_USER=gs1-publisher \\
    DEMOCLIENT_WP_APP_PASS='xxxx xxxx xxxx xxxx' \\
    GS1_PROD_ACCOUNT=87123XXXXXXXX \\
    DEMOCLIENT_GS1_CLIENT_ID='...' DEMOCLIENT_GS1_CLIENT_SECRET='...' \\
    STAGING_GTIN=02987653XXXXXX \\
    pytest -m staging

Optional overrides: ``WP_STAGING_POST_TYPE`` (default ``democlient``),
``WP_STAGING_APP_PASS_ENV`` (default ``DEMOCLIENT_WP_APP_PASS``), ``STAGING_GTIN_PREFIX``
(default ``2987653``), ``GS1_PROD_CLIENT_ID_ENV`` / ``GS1_PROD_CLIENT_SECRET_ENV`` (the
env-var *names* holding the GS1 production secrets, not the secrets themselves; default
``DEMOCLIENT_GS1_CLIENT_ID`` / ``DEMOCLIENT_GS1_CLIENT_SECRET``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    GS1LinkConfig,
    QRConfig,
    WordPressConfig,
)
from lib.gs1_dl_client import GS1DigitalLinkClient
from lib.records import LocalisedText, Plan, PlanClassification, PlanRow, ProductRecord
from lib.state import load_state
from lib.wp_client import WordPressClient
from scripts import run_execute

_WP_URL = os.environ.get("WP_STAGING_URL")
_WP_USER = os.environ.get("WP_STAGING_USER")
_WP_APP_PASS_ENV = os.environ.get("WP_STAGING_APP_PASS_ENV", "DEMOCLIENT_WP_APP_PASS")
_WP_POST_TYPE = os.environ.get("WP_STAGING_POST_TYPE", "democlient")

_GS1_ACCOUNT = os.environ.get("GS1_PROD_ACCOUNT")
# Reuse the canonical secret env-var names from .env.example (overridable).
_GS1_CLIENT_ID_ENV = os.environ.get("GS1_PROD_CLIENT_ID_ENV", "DEMOCLIENT_GS1_CLIENT_ID")
_GS1_CLIENT_SECRET_ENV = os.environ.get(
    "GS1_PROD_CLIENT_SECRET_ENV", "DEMOCLIENT_GS1_CLIENT_SECRET"
)

#: No default: an unset GTIN skips the test rather than writing to an arbitrary one — and
#: this test's GS1 write cannot be undone.
_GTIN = os.environ.get("STAGING_GTIN")

#: Democlient's GS1 company prefix. Overridable for a pilot on another prefix.
_GTIN_PREFIX = os.environ.get("STAGING_GTIN_PREFIX", "2987653")

#: The export's own product list — the authoritative answer to "is this a real product?".
#: Written by parse_export and gitignored, so it may be absent; resolved at import because
#: this test chdirs. See _assert_gtin_not_a_real_product for why this gate is not optional.
_PRODUCTS_JSON = Path(
    os.environ.get("STAGING_PRODUCTS_JSON", "output/democlient/data/products.json")
).resolve()

_SLUG = f"p-{_GTIN}"

#: The title this test gives its page. The pre-flight uses it to tell our own leftovers
#: (safe to reuse and delete) from someone else's page (abort, touch nothing).
_SMOKE_TITLE = "Smoke test product"

_STAGING_READY = bool(
    _WP_URL
    and _WP_USER
    and os.environ.get(_WP_APP_PASS_ENV)
    and _GS1_ACCOUNT
    and os.environ.get(_GS1_CLIENT_ID_ENV)
    and os.environ.get(_GS1_CLIENT_SECRET_ENV)
    and _GTIN
)

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        not _STAGING_READY,
        reason="staging WP + GS1 production not configured (set WP_STAGING_URL, "
        "WP_STAGING_USER, DEMOCLIENT_WP_APP_PASS, GS1_PROD_ACCOUNT, DEMOCLIENT_GS1_CLIENT_ID, "
        "DEMOCLIENT_GS1_CLIENT_SECRET, and STAGING_GTIN)",
    ),
]

_CLIENT_ID = "staging"


def _config() -> ClientConfig:
    return ClientConfig(
        client_id=_CLIENT_ID,
        display_name="Staging Smoke",
        gs1=GS1Config(
            account_number_test="0",
            account_number_production=_GS1_ACCOUNT,
            client_id_env_test="UNUSED",
            client_secret_env_test="UNUSED",
            client_id_env_production=_GS1_CLIENT_ID_ENV,
            client_secret_env_production=_GS1_CLIENT_SECRET_ENV,
            environment="production",
        ),
        export=ExportConfig(path="input/staging.xlsx"),
        wordpress=WordPressConfig(
            site_url=_WP_URL or "",
            username=_WP_USER or "",
            app_password_env=_WP_APP_PASS_ENV,
            post_type=_WP_POST_TYPE,
            multilingual_plugin="polylang",
            default_language="nl",
            languages=["nl"],
        ),
        qr=QRConfig(formats=["svg"], size_mm=20, error_correction="M", dpi=300),
        gs1_links=[GS1LinkConfig(link_type="pip", default=True, title_pattern="{product_name}")],
    )


def _assert_gtin_not_a_real_product() -> None:
    """Refuse a GTIN that the export knows as a real product.

    The two other gates are jointly insufficient, and this is not theoretical — verified
    against `02987653000374` (*Kledingroller*), which passes both: it sits in the 2987653
    prefix, and the live pre-flight finds nothing, because its real page (id 293,
    *"Roll off"*) uses a human slug and carries **no** ``meta.gtin``. Neither the slug
    lookup nor the meta lookup can see it, so the run would cheerfully create a second,
    duplicate page for a product that already has one — and point a permanent GS1
    production entry at it. Only the product list catches that.

    Resolved at import, before the test chdirs. Fails loudly when the file is missing
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
        f"Refusing to run: this test writes a GS1 production entry for it that can never "
        f"be deleted. Use a GTIN that is not an active product."
    )


def _assert_gtin_prefix() -> None:
    """Refuse a GTIN outside Democlient's company prefix, before any HTTP happens.

    Pure and client-free on purpose: it runs before a client is even constructed, so a
    misconfigured GTIN never reaches the network — least of all the GS1 write, which
    cannot be undone.
    """
    # zfill(14)[1:8] is the company prefix of a GTIN-13 carrying indicator digit 0. A
    # GTIN-14 with a non-zero indicator (a trade-item grouping) is rejected here; that is
    # fine for product pages and not worth debugging twice.
    assert _GTIN and _GTIN.zfill(14)[1:8] == _GTIN_PREFIX, (
        f"STAGING_GTIN={_GTIN!r} is not in the {_GTIN_PREFIX} company prefix; refusing to "
        f"write to a GTIN Democlient may not own (override with STAGING_GTIN_PREFIX)"
    )


def _assert_no_foreign_page(wp: WordPressClient) -> None:
    """Refuse to run if the GTIN already has a page this test did not create.

    The prefix check alone does not make a GTIN disposable — every real Democlient product
    shares that prefix too. This is the check that catches a real saleable product.

    It deliberately reuses ``_lookup_existing``, the *same* resolution ``upsert_page`` will
    perform, rather than a lookup of its own: a guard that disagrees with the write about
    which page is at stake is not a guard. If a page comes back that this test did not
    title, the GTIN belongs to real content — the run would adopt and overwrite it, and
    teardown would then delete it with every ownership guard passing, because the GTIN
    really would match.
    """
    existing = wp._lookup_existing(_WP_POST_TYPE, _SLUG, _GTIN, None, "nl")  # noqa: SLF001
    if existing is not None and existing.get("title", {}).get("rendered") != _SMOKE_TITLE:
        pytest.fail(
            f"STAGING_GTIN={_GTIN!r} already has WordPress page {existing.get('id')} that "
            f"this test did not create — it looks like real content. Refusing to run: the "
            f"run would overwrite it and teardown would delete it. Use a GTIN that is not "
            f"an active product."
        )


def _cleanup(cfg: ClientConfig) -> None:
    """Retract the GS1 entry and delete the page, whatever the test did or failed at.

    GS1 first: the resolver is what points the outside world at the page, so it is retired
    before its target disappears. Deleting the page first would leave a live production
    entry aimed at a 404 for as long as the retract took.

    The page is found by its deterministic slug rather than read out of state.
    ``_execute_row`` writes state only on its success path, after ``verify_url`` and
    ``safe_upsert``, and swallows everything before that in a blanket ``except`` — so
    state is empty in exactly the failure cases that leave a page behind.

    Both halves are always attempted and their failures collected: a GS1 hiccup must not
    leave the WordPress page live. Any failure is raised, because a cleanup that fails
    quietly is production residue nobody knows about.
    """
    errors: list[Exception] = []
    try:
        with GS1DigitalLinkClient(cfg.gs1.resolve()) as gs1:
            gs1.retract(_GTIN or "")
    except Exception as exc:  # noqa: BLE001 — report it, but still try the page
        errors.append(exc)
    try:
        with WordPressClient(cfg.wordpress) as wp:
            page = wp.find_by_slug(cfg.wordpress.post_type, _SLUG, "nl")
            if page is not None:
                wp.delete_page(cfg.wordpress.post_type, page["id"], gtin=_GTIN or "")
    except Exception as exc:  # noqa: BLE001 — collected, raised below
        errors.append(exc)
    if errors:
        raise ExceptionGroup("staging cleanup failed", errors)


@pytest.fixture(autouse=True)
def _guarded_staging_target() -> Iterator[None]:
    """Pre-flight the target, then guarantee cleanup.

    The pre-flight runs outside the ``try`` on purpose: if the GTIN turns out to address
    real content we must neither write to it nor delete it, so an abort has to skip the
    teardown as well as the test.
    """
    # Both gates before any client exists, so a bad GTIN issues no HTTP at all.
    _assert_gtin_prefix()
    _assert_gtin_not_a_real_product()
    cfg = _config()
    with WordPressClient(cfg.wordpress) as wp:
        _assert_no_foreign_page(wp)
    try:
        yield
    finally:
        _cleanup(cfg)


def _plan_file(tmp_path: Path) -> Path:
    product = ProductRecord(
        gtin=_GTIN or "",
        brand="SmokeTest",
        product_name=LocalisedText(values={"nl": _SMOKE_TITLE}),
    )
    row = PlanRow(
        gtin=_GTIN or "",
        language="nl",
        classification=PlanClassification.NEW,
        title=_SMOKE_TITLE,
        slug=_SLUG,
        content_hash="staging-smoke",
        target_url=f"{_WP_URL}/{_WP_POST_TYPE}/{_SLUG}/",
        product=product,
    )
    plan = Plan(
        client_id=_CLIENT_ID,
        generated_at=datetime(2026, 7, 12, tzinfo=UTC),
        total=1,
        counts={PlanClassification.NEW: 1},
        rows=[row],
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan.model_dump(mode="json")), encoding="utf-8")
    return path


def _entry_without_timestamp(client_id: str) -> dict[str, object]:
    entry = load_state(client_id).entries[_GTIN or ""]["nl"].model_dump(mode="json")
    entry.pop("last_run")
    return entry


def test_run_execute_end_to_end_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_execute, "get_client", lambda _cid: _config())
    plan = _plan_file(tmp_path)

    # DoD: completes end-to-end for one GTIN against staging WP + GS1 production.
    assert run_execute.main([_CLIENT_ID, "--plan", str(plan)]) == 0
    first = _entry_without_timestamp(_CLIENT_ID)
    assert first["wp_page_id"]

    logs = sorted((tmp_path / "output" / _CLIENT_ID / "runs").glob("*.jsonl"))
    outcomes = [json.loads(line) for line in logs[-1].read_text().splitlines()]
    assert [o["status"] for o in outcomes] == ["ok"]

    # §6.5: a second identical run leaves the meaningful state unchanged.
    assert run_execute.main([_CLIENT_ID, "--plan", str(plan)]) == 0
    assert _entry_without_timestamp(_CLIENT_ID) == first
