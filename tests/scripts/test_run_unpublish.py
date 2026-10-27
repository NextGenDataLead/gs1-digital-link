"""Tests for taking a published product back down (scripts/run_unpublish).

The two properties worth guarding are both about *order* and *honesty*: GS1 is retracted
before the pages are drafted (the reverse leaves an enabled Digital Link resolving to a
404), and state records the result, so the next run does not put the product back up.

The clients are patched with recording fakes rather than mocked at the HTTP layer —
``run_unpublish`` orchestrates, and what matters here is the sequence of calls it makes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    QRConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.errors import GtinMismatchError
from lib.records import State, StateEntry
from lib.state import load_state, save_state
from scripts import run_unpublish

GTIN_A = "02987653000527"  # the pilot
GTIN_B = "02987653007360"

_HASH_LEN = 64


def _make_config(**overrides: Any) -> ClientConfig:
    params: dict[str, Any] = {
        "client_id": "acme",
        "display_name": "Acme BV",
        "gs1": GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
            digital_link_url_pattern="https://id.gs1.org/01/{gtin14}",
        ),
        "export": ExportConfig(path="input/acme.xlsx"),
        "wordpress": WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl", "fr"],
        ),
        "template": TemplateConfig(override_dir=None),
        "qr": QRConfig(formats=["svg"], size_mm=20, error_correction="M", dpi=300),
    }
    params.update(overrides)
    return ClientConfig(**params)


def _entry(page_id: int, language: str) -> StateEntry:
    prefix = "" if language == "nl" else f"/{language}"
    return StateEntry(
        wp_page_id=page_id,
        wp_url=f"https://wp.test{prefix}/product/p-{GTIN_A}/",
        wp_featured_media_id=None,
        content_hash="c" * _HASH_LEN,
        gs1_link_set_hash="g" * _HASH_LEN,
        last_run=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        title="Microvezeldoek stof",
    )


def _seed_state(gtin: str = GTIN_A) -> None:
    """Write a published-looking state for ``gtin`` in both languages."""
    save_state(
        State(
            client_id="acme",
            entries={gtin: {"nl": _entry(1447, "nl"), "fr": _entry(1448, "fr")}},
        )
    )


class _Recorder:
    def __init__(self) -> None:
        #: Every call, in order, as ("retract"|"status", detail) — the order is the point.
        self.calls: list[tuple[str, Any]] = []


def _install(  # noqa: PLR0913 — one knob per failure mode; bundling them only hides them
    monkeypatch: pytest.MonkeyPatch,
    cfg: ClientConfig,
    *,
    has_record: bool = True,
    wp_error: Exception | None = None,
    wp_error_languages: tuple[str, ...] = (),
    page_gone: tuple[str, ...] = (),
) -> _Recorder:
    rec = _Recorder()

    class FakeWP:
        def __init__(self, config: WordPressConfig) -> None:
            pass

        def __enter__(self) -> FakeWP:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def set_page_status(
            self, post_type: str, page_id: int, *, gtin: str, status: str
        ) -> dict[str, Any] | None:
            rec.calls.append(("status", (post_type, page_id, gtin, status)))
            language = "nl" if page_id == 1447 else "fr"
            if wp_error is not None and language in wp_error_languages:
                raise wp_error
            if language in page_gone:
                return None
            return {"id": page_id, "status": status}

    class FakeGS1:
        def __init__(self, config: object) -> None:
            pass

        def __enter__(self) -> FakeGS1:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def retract(self, gtin: str) -> bool:
            rec.calls.append(("retract", gtin))
            return has_record

    monkeypatch.setattr(run_unpublish, "get_client", lambda _cid: cfg)
    monkeypatch.setattr(run_unpublish, "WordPressClient", FakeWP)
    monkeypatch.setattr(run_unpublish, "GS1DigitalLinkClient", FakeGS1)
    return rec


# --- The happy path ----------------------------------------------------------


def test_retracts_gs1_before_drafting_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GS1 first. Drafting first would leave an enabled Digital Link pointing at a 404.

    A drafted page is not publicly reachable (``verify_url`` proved it answers 404), so
    every QR scanned in the window between the two writes would hit a dead end. Retracting
    first degrades to "the QR does nothing", which is the intended end state anyway.
    """
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_A])

    assert code == 0
    assert rec.calls[0] == ("retract", GTIN_A)  # the ordering that matters
    # Both pages follow, in a deterministic (language-sorted) order.
    assert rec.calls[1:] == [
        ("status", ("product", 1448, GTIN_A, "draft")),  # fr
        ("status", ("product", 1447, GTIN_A, "draft")),  # nl
    ]


def test_records_the_takedown_in_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    _seed_state()

    run_unpublish.main(["acme", "--gtin", GTIN_A])

    entries = load_state("acme").entries[GTIN_A]
    assert [e.wp_status for e in entries.values()] == ["draft", "draft"]
    assert all(e.retracted for e in entries.values())
    # The link-set hash goes with the retraction, because retraction deletes the links.
    # A stale hash here is what made `--revive --only pages` classify UNCHANGED and leave
    # the resolver record retracted for good.
    assert not any(e.gs1_link_set_hash for e in entries.values())
    # The page ids survive: a held product must still be findable to revive it.
    assert entries["nl"].wp_page_id == 1447


def test_drafts_pages_even_when_there_is_no_resolver_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # retract() returns False when there was nothing to retract. That is not a failure,
    # and it must not stop the pages coming down.
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), has_record=False)
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_A])

    assert code == 0
    assert [c for c in rec.calls if c[0] == "status"]


def test_is_idempotent_across_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    _seed_state()

    assert run_unpublish.main(["acme", "--gtin", GTIN_A]) == 0
    assert run_unpublish.main(["acme", "--gtin", GTIN_A]) == 0

    entries = load_state("acme").entries[GTIN_A]
    assert [e.wp_status for e in entries.values()] == ["draft", "draft"]


# --- Failure and refusal -----------------------------------------------------


def test_one_language_failing_does_not_strand_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike run_execute, a sibling failure must NOT block the GTIN: the resolver is
    # already retracted by then, so stopping would leave a *published* page whose QR is
    # dead. Getting the other language down is strictly better than getting neither.
    monkeypatch.chdir(tmp_path)
    _install(
        monkeypatch,
        _make_config(),
        wp_error=RuntimeError("boom"),
        wp_error_languages=("nl",),
    )
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_A])

    assert code == 1  # the failure is reported, not swallowed
    entries = load_state("acme").entries[GTIN_A]
    assert entries["nl"].wp_status == "publish"  # untouched — state does not claim otherwise
    assert entries["fr"].wp_status == "draft"


def test_gtin_mismatch_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # E8: a stale page id in state points at another product's page. The guard lives in
    # the client; what matters here is that run_unpublish surfaces it as an error.
    monkeypatch.chdir(tmp_path)
    _install(
        monkeypatch,
        _make_config(),
        wp_error=GtinMismatchError(GTIN_A, "999", 1447),
        wp_error_languages=("nl", "fr"),
    )
    _seed_state()

    assert run_unpublish.main(["acme", "--gtin", GTIN_A]) == 1


def test_unknown_gtin_is_a_config_error_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without state there are no page ids, so the WordPress half cannot run. Succeeding
    # here would mean a retracted resolver still pointing at live pages.
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_B])

    assert code == 2
    assert rec.calls == []


def test_page_already_gone_leaves_status_unclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The id 404s. `retracted` still flips (the resolver was retracted), but wp_status
    # must not claim "draft" for a page that no longer exists.
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config(), page_gone=("nl",))
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_A])

    assert code == 0
    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.wp_status == "publish"
    assert entry.retracted is True


# --- Dry run -----------------------------------------------------------------


def test_dry_run_touches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    _seed_state()

    code = run_unpublish.main(["acme", "--gtin", GTIN_A, "--dry-run"])

    assert code == 0
    assert rec.calls == []
    assert load_state("acme").entries[GTIN_A]["nl"].wp_status == "publish"


def test_requires_a_gtin() -> None:
    with pytest.raises(SystemExit):  # --gtin is required
        run_unpublish.main(["acme"])
