"""Tests for scripts/reconcile.py — the read-only live-vs-ledger comparison.

The comparison itself is covered in ``tests/lib/test_reconcile.py``. What matters here is the
orchestration around it, and one property in particular: **this must not write anything.** It is
a diagnostic, and a diagnostic that changes what the next run does is worse than no diagnostic —
which is why it reads state through ``peek_state`` rather than ``load_state``, and why the fake
client below records every call so a stray write would show up as a failure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    WordPressConfig,
)
from lib.records import State, StateEntry
from lib.state import save_state, state_path
from scripts import reconcile

GTIN_A = "02987653000001"
GTIN_B = "02987653000002"


def _make_config(languages: list[str] | None = None) -> ClientConfig:
    return ClientConfig(
        client_id="acme",
        display_name="Acme BV",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(path="input/acme.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            languages=languages or ["nl"],
            default_language="nl",
        ),
    )


class _FakeWP:
    """A WordPress client that only knows how to list, and records what it was asked."""

    pages: dict[str, list[dict[str, Any]]] = {}
    calls: list[tuple[str, str | None]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _FakeWP:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def list_pages_with_gtin(
        self, post_type: str, language: str | None = None
    ) -> list[dict[str, Any]]:
        type(self).calls.append((post_type, language))
        return type(self).pages.get(language or "", [])

    def __getattr__(self, name: str) -> object:  # pragma: no cover - the point is that it raises
        raise AssertionError(f"reconcile called {name}() — it must only read")


def _page(gtin: str, page_id: int, *, status: str = "publish") -> dict[str, Any]:
    return {
        "id": page_id,
        "slug": f"p-{gtin}",
        "status": status,
        "link": f"https://wp.test/p-{gtin}/",
        "meta": {"gtin": gtin},
    }


def _entry(page_id: int) -> StateEntry:
    return StateEntry(
        wp_page_id=page_id,
        wp_url=f"https://wp.test/p-{page_id}/",
        wp_featured_media_id=None,
        content_hash="c" * 64,
        gs1_link_set_hash="g" * 64,
        last_run=datetime.now(UTC),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    cfg: ClientConfig,
    pages: dict[str, list[dict[str, Any]]],
) -> None:
    _FakeWP.pages = pages
    _FakeWP.calls = []
    monkeypatch.setattr(reconcile, "get_client", lambda _cid: cfg)
    monkeypatch.setattr(reconcile, "WordPressClient", _FakeWP)


def test_a_site_that_matches_the_ledger_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="acme", entries={GTIN_A: {"nl": _entry(11)}}))
    _install(monkeypatch, _make_config(), {"nl": [_page(GTIN_A, 11)]})

    assert reconcile.main(["acme"]) == 0
    assert "they agree" in capsys.readouterr().err


def test_a_page_the_ledger_never_heard_of_exits_1_and_explains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The partial-failure case that prompted this: live page, error row, nothing recorded."""
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="acme", entries={}))
    _install(monkeypatch, _make_config(), {"nl": [_page(GTIN_A, 11)]})

    assert reconcile.main(["acme"]) == 1
    err = capsys.readouterr().err
    assert "live_not_recorded" in err
    assert GTIN_A in err
    assert "classify this product as NEW" in err


def test_every_configured_language_is_asked_for_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a WPML site an unscoped query answers with the default language only.

    A reconciliation that skipped this would report every French page as missing from the site.
    """
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="acme", entries={}))
    _install(monkeypatch, _make_config(["nl", "fr"]), {})

    reconcile.main(["acme"])

    assert _FakeWP.calls == [("product", "nl"), ("product", "fr")]


def test_json_mode_carries_the_findings_and_the_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="acme", entries={GTIN_B: {"nl": _entry(21)}}))
    _install(monkeypatch, _make_config(), {"nl": [_page(GTIN_A, 11)]})

    reconcile.main(["acme", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["agrees"] is False
    assert payload["live_pages"] == 1
    assert payload["state_entries"] == 1
    kinds = {f["kind"] for f in payload["findings"]}
    assert kinds == {"live_not_recorded", "recorded_not_live"}
    assert all(f["explanation"] for f in payload["findings"])


def test_a_corrupt_state_file_is_reported_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diagnostic must not quarantine the ledger (E19) — that would re-plan every row as NEW."""
    monkeypatch.chdir(tmp_path)
    path = state_path("acme")
    path.parent.mkdir(parents=True)
    path.write_text("{ not valid json", encoding="utf-8")
    _install(monkeypatch, _make_config(), {"nl": []})

    assert reconcile.main(["acme"]) == 2
    assert "will not parse" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == "{ not valid json"
    assert not list(path.parent.glob("state.json.corrupt.*"))


def test_it_writes_nothing_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The output tree must look identical afterwards. Read-only is the whole safety story."""
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="acme", entries={GTIN_A: {"nl": _entry(11)}}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _install(monkeypatch, _make_config(), {"nl": [_page(GTIN_B, 21, status="draft")]})

    reconcile.main(["acme"])

    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
