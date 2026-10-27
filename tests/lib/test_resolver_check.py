"""Tests for lib.resolver_check — where a scan lands, per language, after a links run."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lib import resolver_check
from lib.resolver_check import Landing, check_until_settled, same_page

NL = "https://shop.test/p-1/"
FR = "https://shop.test/fr/p-1/"
TARGETS = {"02987653000961": {"nl": NL, "fr": FR}}


def _uri(gtin: str) -> str:
    return f"https://id.gs1.org/01/{gtin}"


class _Clock:
    """Moves on by ``step`` each read, so a timeout is reached in a known number of rounds."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_same_page_ignores_trailing_slash_host_case_and_query() -> None:
    assert same_page("https://Shop.test/fr/p-1?linkType=gs1:pip", FR)
    assert not same_page(NL, FR)


def test_a_phone_asks_for_the_regional_form_then_the_plain_one() -> None:
    """A bare ``fr-BE`` falls through to the default link; phones also list plain ``fr``."""
    assert resolver_check.accept_language("fr") == "fr-BE,fr;q=0.9"


def test_settled_on_the_first_round_asks_once_per_language_and_never_waits() -> None:
    asked: list[str] = []
    slept: list[float] = []

    def fetch(uri: str, language: str) -> tuple[int | None, str | None]:
        asked.append(language)
        return 307, TARGETS["02987653000961"][language]

    landings = check_until_settled(TARGETS, _uri, fetch, sleep=slept.append, clock=_Clock(1))

    assert all(landing.ok for landing in landings)
    assert sorted(asked) == ["fr", "nl"]
    assert slept == []


def test_a_record_still_propagating_is_asked_again_until_it_is_right() -> None:
    """GS1's public resolver served the old record for ~30 s after a write; that is not a fault."""
    rounds = {"fr": 0}

    def fetch(uri: str, language: str) -> tuple[int | None, str | None]:
        if language == "nl":
            return 307, NL
        rounds["fr"] += 1
        return 307, (NL if rounds["fr"] < 3 else FR)  # Dutch twice, then French

    asked: list[tuple[str, str]] = []

    def recording(uri: str, language: str) -> tuple[int | None, str | None]:
        asked.append((uri, language))
        return fetch(uri, language)

    landings = check_until_settled(TARGETS, _uri, recording, sleep=lambda _s: None, clock=_Clock(1))

    assert resolver_check.wrong(landings) == []
    assert [lang for _, lang in asked].count("nl") == 1  # only the wrong one is re-asked
    assert [lang for _, lang in asked].count("fr") == 3


def test_a_landing_still_wrong_when_the_wait_runs_out_is_reported() -> None:
    def fetch(uri: str, language: str) -> tuple[int | None, str | None]:
        return 307, NL

    landings = check_until_settled(
        TARGETS, _uri, fetch, timeout=60, interval=15, sleep=lambda _s: None, clock=_Clock(30)
    )

    bad = resolver_check.wrong(landings)
    assert [(b.language, b.location) for b in bad] == [("fr", NL)]
    assert bad[0].describe() == f"02987653000961 (fr): expected {FR}, scan went to {NL}"


def test_no_redirect_is_described_by_its_status() -> None:
    landing = Landing("02987653000961", "fr", FR, 404, None)
    assert not landing.ok
    assert landing.describe().endswith("no redirect (status 404)")
    assert Landing("02987653000961", "fr", FR, None, None).describe().endswith("no answer")


def test_fetch_uses_get_without_following_the_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET, never HEAD: id.gs1.org answers HEAD with 404 and GET with the 307 a phone follows."""
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kw: Any) -> httpx.Response:
        seen.update(url=url, **kw)
        return httpx.Response(307, headers={"location": FR})

    monkeypatch.setattr(resolver_check.httpx, "get", fake_get)

    assert resolver_check.fetch_landing(_uri("02987653000961"), "fr") == (307, FR)
    assert seen["follow_redirects"] is False
    assert seen["headers"] == {"Accept-Language": "fr-BE,fr;q=0.9"}


def test_a_network_failure_is_no_answer_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(resolver_check.httpx, "get", boom)

    assert resolver_check.fetch_landing(_uri("02987653000961"), "fr") == (None, None)
