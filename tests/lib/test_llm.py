"""Tests for the Anthropic API backend (``lib.llm``).

The client is exercised over a mocked Messages API (``pytest-httpx``): the request shape (model,
temperature, forced tool, auth headers, rendered inputs) is asserted, canned tool results are parsed
into :class:`~lib.generator.GenerationResult`, and the failure paths (missing key, HTTP error,
missing/malformed tool call, retry) each raise the expected typed error. No real network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from lib import client_files
from lib.config import GeneratorConfig
from lib.errors import GeneratorError, LLMAPIError, MissingCredentialError
from lib.generator import MODE_TIGHTEN, GenerationInputs, GenerationRequest, TranslationGap
from lib.llm import ANTHROPIC_VERSION, AnthropicClient, load_voice_template

_KEY_ENV = "TEST_ANTHROPIC_KEY"
_VOICE = "You are Democlient's copywriter. Write terse Dutch taglines."


def _config(**overrides: Any) -> GeneratorConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "model": "claude-sonnet-5",
        "prompt_version": "v1",
        "api_key_env": _KEY_ENV,
        "max_tokens": 512,
    }
    params.update(overrides)
    return GeneratorConfig(**params)


def _request(
    *,
    mode: str = "generate",
    translations: list[TranslationGap] | None = None,
    candidates: list[str] | None = None,
) -> GenerationRequest:
    return GenerationRequest(
        gtin="00292222002959",
        language="nl",
        inputs=GenerationInputs(
            functional_name="voegstrijker",
            marketing_message="Het perfecte gereedschap voor alle elastische voegen",
            net_content="4 H87",
        ),
        input_fingerprint="fp-1",
        translations=translations or [],
        mode=mode,
        candidates=candidates or [],
    )


def _gap(field: str, source_value: str) -> TranslationGap:
    return TranslationGap(
        field=field,
        source_language="fr",
        source_value=source_value,
        source_label="TradeItemDescription attr 3301",
    )


def _tool_response(
    usps: list[str],
    translations: dict[str, str] | None = None,
    inferences: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"usps": usps}
    if translations is not None:
        payload["translations"] = translations
    if inferences is not None:
        payload["inferences"] = inferences
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "produce_copy", "input": payload}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# --- happy path & request shape ----------------------------------------------


def test_generate_copy_parses_tool_result_and_sends_expected_request(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test-123")
    httpx_mock.add_response(json=_tool_response(["Voor gladde voegen", "Op alle voegen"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request())

    assert result.usps == ["Voor gladde voegen", "Op alle voegen"]

    request = httpx_mock.get_requests()[-1]
    assert request.headers["x-api-key"] == "sk-test-123"
    assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5"
    assert body["max_tokens"] == 512
    assert body["system"] == _VOICE
    assert body["tool_choice"] == {"type": "tool", "name": "produce_copy"}
    assert body["tools"][0]["name"] == "produce_copy"
    # the request inputs are rendered into the user message
    user_message = body["messages"][0]["content"]
    assert "Het perfecte gereedschap voor alle elastische voegen" in user_message
    assert "00292222002959" in user_message


def test_the_payload_carries_no_sampling_parameters(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-default ``temperature``/``top_p``/``top_k`` is a 400 on the configured model.

    This sent ``temperature: 0`` from the day it was written, which is why the backend had never
    produced a line of copy against ``claude-sonnet-5`` — the request was rejected before the
    model saw it. Pinned as an assertion rather than a comment because the parameter reads like
    an obvious way to ask for determinism, and re-adding it would break the same way, silently,
    on a path that only runs when an operator is publishing.
    """
    monkeypatch.setenv(_KEY_ENV, "sk-test-123")
    httpx_mock.add_response(json=_tool_response(["Voor gladde voegen"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        client.generate_copy(_request())

    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body


def test_thinking_is_disabled_explicitly_rather_than_left_out(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting ``thinking`` means adaptive thinking *on*, and it shares ``max_tokens``.

    So leaving the field out would let a 1024-token budget go on reasoning nobody reads and
    truncate the forced tool call — a failure that surfaces as a malformed result rather than as
    a missing setting.
    """
    monkeypatch.setenv(_KEY_ENV, "sk-test-123")
    httpx_mock.add_response(json=_tool_response(["Voor gladde voegen"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        client.generate_copy(_request())

    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["thinking"] == {"type": "disabled"}


def test_tighten_request_renders_candidates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(json=_tool_response(["Kort", "Bullet"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        client.generate_copy(
            _request(mode=MODE_TIGHTEN, candidates=["Een hele lange zin die ingekort moet worden"])
        )

    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert "Een hele lange zin die ingekort moet worden" in body["messages"][0]["content"]


def test_a_language_gap_is_rendered_into_the_prompt_with_its_source_text(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer translates in the same call it writes the copy, so it needs the source."""
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(json=_tool_response(["Slogan"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        client.generate_copy(_request(translations=[_gap("product_name", "Lisse-joints")]))

    user_message = json.loads(httpx_mock.get_requests()[-1].content)["messages"][0]["content"]
    assert "product_name (from fr): Lisse-joints" in user_message


def test_translations_are_parsed_off_the_tool_result(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(
        json=_tool_response(["Slogan"], translations={"product_name": "Voegstrijker"})
    )

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request(translations=[_gap("product_name", "Lisse-joints")]))

    assert result.translations == {"product_name": "Voegstrijker"}


def test_the_tool_asks_for_inferences(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared shape must match ``GenerationResult``, or a producer swap loses a field.

    ``inferences`` is how a producer declares the claims it wrote that the feed does not state,
    and they become the ``generation_inference`` findings in §2 of the data-quality report — the
    section the operator forwards to the client. The tool schema is the only place this producer
    can learn the field exists: it never sees the ``content-generator`` skill, which is where the
    in-session producer is told. Omit it here and §2 goes empty on an API-generated batch, which
    reads as "nothing was inferred" rather than "nobody was asked".
    """
    monkeypatch.setenv(_KEY_ENV, "sk-test-123")
    httpx_mock.add_response(json=_tool_response(["Slogan"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        client.generate_copy(_request())

    tool = json.loads(httpx_mock.get_requests()[-1].content)["tools"][0]
    assert "inferences" in tool["input_schema"]["properties"]
    assert "inferences" not in tool["input_schema"]["required"]


def test_inferences_are_carried_off_the_tool_result(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking is half of it; the parser dropped them on the floor for the field's whole life."""
    monkeypatch.setenv(_KEY_ENV, "sk-test-123")
    httpx_mock.add_response(
        json=_tool_response(
            ["Microvezeldoekjes voor het hele huis", "Herbruikbaar en uitwasbaar"],
            inferences=["Herbruikbaar: eigenschap van microvezel, niet in 1083."],
        )
    )

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request())

    assert result.inferences == ["Herbruikbaar: eigenschap van microvezel, niet in 1083."]


def test_a_result_with_no_inferences_is_an_empty_list_not_an_error(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy drawn straight from the feed infers nothing, and that is the ordinary case."""
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(json=_tool_response(["Slogan"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request())

    assert result.inferences == []


def test_a_result_with_no_translations_is_an_empty_mapping_not_an_error(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Most units have no language gap at all; the key is optional in the tool schema.
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(json=_tool_response(["Slogan"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request())

    assert result.translations == {}


# --- failure paths -----------------------------------------------------------


def test_a_blank_key_is_missing_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` ships `ANTHROPIC_API_KEY=` as a scaffold, so blank is the likeliest way to be unset.

    ``os.environ[name]`` succeeds on an empty value, so without this the client sends an empty
    ``x-api-key`` and the operator is told Anthropic rejected their key — when in fact nobody set
    one. No HTTP request may be made: ``pytest-httpx`` fails the test if one is, which is the
    assertion that matters here.
    """
    monkeypatch.setenv(_KEY_ENV, "   ")

    with (
        AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client,
        pytest.raises(MissingCredentialError, match=_KEY_ENV),
    ):
        client.generate_copy(_request())


def test_missing_key_raises_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_KEY_ENV, raising=False)
    client = AnthropicClient(_config(), _VOICE, sleep=lambda _: None)

    with pytest.raises(MissingCredentialError, match=_KEY_ENV):
        client.generate_copy(_request())


def test_http_error_raises_llm_api_error(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(status_code=400, text="bad request")

    with (
        AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client,
        pytest.raises(LLMAPIError) as exc_info,
    ):
        client.generate_copy(_request())

    assert exc_info.value.status_code == 400


def test_response_without_tool_use_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(
        json={"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}
    )

    with (
        AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client,
        pytest.raises(LLMAPIError, match="no produce_copy tool call"),
    ):
        client.generate_copy(_request())


def test_malformed_tool_input_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(json=_tool_response([]))  # empty usps violates min_length=1

    with (
        AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client,
        pytest.raises(LLMAPIError, match="malformed produce_copy input"),
    ):
        client.generate_copy(_request())


def test_retries_on_429_then_succeeds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KEY_ENV, "sk-test")
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json=_tool_response(["Tagline"]))

    with AnthropicClient(_config(), _VOICE, sleep=lambda _: None) as client:
        result = client.generate_copy(_request())

    assert result.usps == ["Tagline"]
    assert len(httpx_mock.get_requests()) == 2


# --- voice template loader ---------------------------------------------------


def _voice(root: Path, *parts: str, text: str) -> None:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_load_voice_template_reads_the_release_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_files, "CODE_ROOT", tmp_path / "code")
    monkeypatch.chdir(tmp_path)
    _voice(tmp_path / "code", "prompts", "acme", "generation.v3.md", text="voice text")

    assert load_voice_template("acme", "v3") == "voice text"


def test_the_data_folder_copy_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The data folder's prompt travels in a handover pack; the release's is only the fallback."""
    monkeypatch.setattr(client_files, "CODE_ROOT", tmp_path / "code")
    monkeypatch.chdir(tmp_path)
    _voice(tmp_path / "code", "prompts", "acme", "generation.v3.md", text="release")
    _voice(tmp_path, "input", "acme", "prompts", "generation.v3.md", text="data folder")

    assert load_voice_template("acme", "v3") == "data folder"


def test_a_prompts_folder_at_the_data_root_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``input/{client}/prompts`` — a stray top-level ``prompts/`` is not the client's."""
    monkeypatch.setattr(client_files, "CODE_ROOT", tmp_path / "code")
    monkeypatch.chdir(tmp_path)
    _voice(tmp_path, "prompts", "acme", "generation.v3.md", text="wrong")
    with pytest.raises(GeneratorError, match="no voice template"):
        load_voice_template("acme", "v3")


def test_load_voice_template_missing_names_both_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_files, "CODE_ROOT", tmp_path / "code")
    monkeypatch.chdir(tmp_path)
    # Either separator: Windows prints the path with backslashes.
    sep = r"[/\\]"
    where = rf"input{sep}acme{sep}prompts{sep}generation\.v9\.md or .*code"
    with pytest.raises(GeneratorError, match=where):
        load_voice_template("acme", "v9")
