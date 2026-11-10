"""Tests for the translation step of a ``translate_copy`` run (``lib.copy_translation_run``).

Pinned: a translation translates the Dutch this run writes, or else the Dutch page as it is live —
never anything else; one unit's failure leaves that page alone and the others are written; a
refusal that fails every call stops the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.config import ClientConfig, ExportConfig, GS1Config, WordPressConfig
from lib.copy_translation_run import translate_units
from lib.errors import LLMAPIError, WordPressAPIError
from lib.generator import (
    GenerationContext,
    GenerationRequest,
    GenerationResult,
    ResultItem,
    ResultsFile,
    pending_requests,
)
from lib.records import LocalisedText, ProductRecord, State, StateEntry
from lib.state import save_state

GTIN = "08713195006109"
CONTEXT = GenerationContext(
    languages=["nl", "fr"], default_language="nl", prompt_version="v1", translate_copy=True
)


def _cfg() -> ClientConfig:
    return ClientConfig(
        client_id="democlient",
        display_name="Democlient",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(path="x.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test", username="bot", app_password_env="WP_PASS"
        ),
    )


def _product() -> ProductRecord:
    return ProductRecord(
        gtin=GTIN,
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Pet spa", "fr": "Pet spa"}),
        description_short=LocalisedText(values={"nl": "Borstel", "fr": "Brosse"}),
    )


def _dutch_written(usps: list[str]) -> ResultsFile:
    request = pending_requests([_product()], CONTEXT)[0]
    item = ResultItem(
        gtin=GTIN, language="nl", usps=usps, input_fingerprint=request.input_fingerprint
    )
    return ResultsFile(client_id="democlient", results=[item])


class _Translator:
    """Prefixes each source line, so what was translated is visible in the result."""

    def __init__(self, *, fail: int | None = None, drop_a_line: bool = False) -> None:
        self.fail, self.drop_a_line = fail, drop_a_line
        self.asked: list[GenerationRequest] = []

    def generate_copy(self, request: GenerationRequest) -> GenerationResult:
        self.asked.append(request)
        if self.fail is not None:
            raise LLMAPIError(self.fail, "no")
        usps = [f"FR {line}" for line in request.source_usps]
        return GenerationResult(usps=usps[:-1] if self.drop_a_line else usps)


def _no_site(_pages: Mapping[str, int]) -> dict[str, list[str]]:
    raise AssertionError("the site must not be read when this run writes the Dutch")


def test_this_runs_dutch_is_what_is_translated() -> None:
    run = translate_units(
        _cfg(),
        CONTEXT,
        [_product()],
        None,
        _dutch_written(["Slogan", "Punt"]),
        _Translator(),
        _no_site,
    )

    assert [(i.language, i.usps) for i in run.items] == [("fr", ["FR Slogan", "FR Punt"])]
    assert run.not_written == []


def test_a_live_dutch_page_left_alone_is_translated_as_it_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    entry = StateEntry(
        wp_page_id=42,
        wp_url="https://wp.test/p/",
        wp_featured_media_id=None,
        content_hash="h",
        gs1_link_set_hash="g",
        last_run=datetime(2026, 11, 1, tzinfo=UTC),
    )
    save_state(State(client_id="democlient", entries={GTIN: {"nl": entry}}))
    asked: list[Mapping[str, int]] = []

    def site(pages: Mapping[str, int]) -> dict[str, list[str]]:
        asked.append(dict(pages))
        return {GTIN: ["Live slogan"]}

    run = translate_units(
        _cfg(),
        CONTEXT,
        [_product()],
        {(GTIN, "fr")},
        ResultsFile(client_id="democlient"),
        _Translator(),
        site,
    )

    assert asked == [{GTIN: 42}]
    assert [i.usps for i in run.items] == [["FR Live slogan"]]


def test_a_site_that_cannot_be_read_writes_no_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def site(_pages: Mapping[str, int]) -> dict[str, list[str]]:
        raise WordPressAPIError(503, "down")

    run = translate_units(
        _cfg(),
        CONTEXT,
        [_product()],
        {(GTIN, "fr")},
        ResultsFile(client_id="democlient"),
        _Translator(),
        site,
    )

    assert run.items == []
    assert [(g, lang) for g, lang, _ in run.not_written] == [(GTIN, "fr")]
    assert "could not be read" in run.not_written[0][2]


def test_a_translation_that_does_not_line_up_is_not_written() -> None:
    run = translate_units(
        _cfg(),
        CONTEXT,
        [_product()],
        None,
        _dutch_written(["Slogan", "Punt"]),
        _Translator(drop_a_line=True),
        _no_site,
    )

    assert run.items == []
    assert "1 lines for 2" in run.not_written[0][2]


class _Answers(_Translator):
    def __init__(self, usps: list[str]) -> None:
        super().__init__()
        self.usps = usps

    def generate_copy(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(usps=self.usps)


def test_a_blank_line_cannot_pass_the_count_and_a_blank_answer_cannot_stop_the_run() -> None:
    written = _dutch_written(["Slogan", "Punt", "Derde"])
    shifted = translate_units(
        _cfg(), CONTEXT, [_product()], None, written, _Answers(["A", "  ", "C"]), _no_site
    )
    blank = translate_units(_cfg(), CONTEXT, [_product()], None, written, _Answers([" "]), _no_site)

    assert shifted.items == []
    assert "2 lines for 3" in shifted.not_written[0][2]
    assert blank.items == []
    assert [(g, lang) for g, lang, _ in blank.not_written] == [(GTIN, "fr")]


def test_one_refused_call_is_reported_and_a_refusal_for_all_stops_the_run() -> None:
    written = _dutch_written(["Slogan"])
    run = translate_units(
        _cfg(), CONTEXT, [_product()], None, written, _Translator(fail=500), _no_site
    )

    assert [(g, lang) for g, lang, _ in run.not_written] == [(GTIN, "fr")]
    with pytest.raises(LLMAPIError):
        translate_units(
            _cfg(), CONTEXT, [_product()], None, written, _Translator(fail=401), _no_site
        )
