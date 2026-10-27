"""Tests for the multilingual adapters (IMPLEMENTATION_SPEC §4.5).

The adapters reuse a :class:`lib.wp_client.WordPressClient`'s transport, so these tests
use a tiny fake exposing just ``_request`` to capture the outbound call without any
HTTP, plus ``make_adapter`` type mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from lib.errors import ConfigError, WordPressAPIError
from lib.multilingual import (
    NoOpAdapter,
    PolylangAdapter,
    WPMLAdapter,
    make_adapter,
)

_WPML_PATH = "/wp-json/democlient/v1/translations"


class _FakeResponse:
    def __init__(self, body: object) -> None:
        self._body = body

    def json(self) -> object:
        return self._body


class _FakeClient:
    """Records ``_request`` calls the way an adapter would issue them."""

    def __init__(self, response: object = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def _request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "path": path, **kwargs})
        return _FakeResponse(self._response)


def _wpml(source: str = "nl") -> WPMLAdapter:
    return WPMLAdapter(_WPML_PATH, source)


def test_make_adapter_maps_each_plugin() -> None:
    assert isinstance(make_adapter("polylang"), PolylangAdapter)
    assert isinstance(
        make_adapter("wpml", wpml_helper_path=_WPML_PATH, source_language="nl"), WPMLAdapter
    )
    assert isinstance(make_adapter("none"), NoOpAdapter)


def test_make_adapter_wpml_without_helper_config_raises() -> None:
    with pytest.raises(ConfigError, match="wpml_helper_path"):
        make_adapter("wpml")


def test_noop_adapter_makes_no_request() -> None:
    fake = _FakeClient()
    NoOpAdapter().link_translations(fake, {"nl": 1, "fr": 2})  # type: ignore[arg-type]
    assert fake.calls == []


def test_polylang_links_translation_group() -> None:
    fake = _FakeClient()
    translations = {"nl": 10, "fr": 11}

    PolylangAdapter().link_translations(fake, translations)  # type: ignore[arg-type]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/wp-json/pll/v1/translations"
    assert call["json_body"] == {"translations": translations}


def test_polylang_single_language_is_noop() -> None:
    fake = _FakeClient()
    PolylangAdapter().link_translations(fake, {"nl": 10})  # type: ignore[arg-type]
    assert fake.calls == []


def test_wpml_links_translation_group_via_the_helper() -> None:
    translations = {"nl": 10, "fr": 11}
    fake = _FakeClient({"ok": True, "trid": 42, "translations": translations})

    _wpml().link_translations(fake, translations)  # type: ignore[arg-type]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == _WPML_PATH
    assert call["json_body"] == {"translations": translations, "source_language": "nl"}


def test_wpml_single_language_is_noop() -> None:
    fake = _FakeClient()
    _wpml().link_translations(fake, {"nl": 10})  # type: ignore[arg-type]
    assert fake.calls == []


def test_wpml_source_language_absent_raises() -> None:
    fake = _FakeClient()
    with pytest.raises(ConfigError, match="source language"):
        _wpml("de").link_translations(fake, {"nl": 1, "fr": 2})  # type: ignore[arg-type]
    assert fake.calls == []  # nothing sent — the group could not be formed


def test_wpml_accepts_string_ids_from_the_helper() -> None:
    """PHP hands back JSON object keys as strings; the ids must still compare equal."""
    fake = _FakeClient({"ok": True, "trid": 42, "translations": {"nl": "10", "fr": "11"}})
    _wpml().link_translations(fake, {"nl": 10, "fr": 11})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"ok": True, "trid": 42, "translations": {"nl": 10}}, "fr silently missing"),
        ({"ok": True, "trid": 42, "translations": {"nl": 10, "fr": 99}}, "wrong id linked"),
        ({"ok": True, "trid": 42, "translations": {}}, "empty group"),
        ({"ok": True, "trid": 42}, "no translations key"),
        ("not-json-object", "unexpected body shape"),
    ],
)
def test_wpml_raises_when_the_reported_group_is_not_the_requested_one(
    body: object, why: str
) -> None:
    """The helper reads the group back from WPML's tables, so a mismatch means WPML did not
    apply the link. Trusting the 200 would leave a page published but unreachable in its own
    language — the silent-success shape this integration is most prone to."""
    fake = _FakeClient(body)
    with pytest.raises(WordPressAPIError) as exc:
        _wpml().link_translations(fake, {"nl": 10, "fr": 11})  # type: ignore[arg-type]
    # The detail lives on response_body; str(WordPressAPIError) is only the status line.
    assert "the link was not applied" in exc.value.response_body
