"""Headless Anthropic Messages API backend for the content generator (generator SPEC, commit 6).

:class:`AnthropicClient` implements the :class:`lib.generator.LLMClient` protocol over the Messages
API using **sync httpx** (IMPLEMENTATION_SPEC §1: sync httpx only — no ``anthropic`` SDK), so
``scripts/run_generate.py --backend api`` can write this run's copy unattended. It shares the
results file and the contract seam with the in-session producer: same
:class:`~lib.generator.GenerationRequest` in, same :class:`~lib.generator.GenerationResult` out,
same versioned voice template (``prompts/{client}/generation.{prompt_version}.md``). Determinism
comes from thinking being off, a pinned model, and the versioned prompt; the input fingerprint
excludes the model, so the two producers are interchangeable.

**Sampling parameters are deliberately absent.** This sent ``temperature: 0`` until the configured
model became ``claude-sonnet-5``, which — like every Opus 4.7-and-later model — rejects a
non-default ``temperature``/``top_p``/``top_k`` with a 400. The request never reached the model,
which is why this backend had never produced a line of copy.

The model answers through one forced tool call (:data:`_PRODUCE_COPY_TOOL`), so the result is a
strict, parseable ``{"usps": [...], "translations"?: {...}, "inferences"?: [...]}`` rather than
free text — the same three fields :class:`~lib.generator.GenerationResult` carries, because a
producer that cannot fill one of them is not interchangeable with the other. The API key is
read lazily from the env var named in the client's :class:`~lib.config.GeneratorConfig`, raising
:class:`~lib.errors.MissingCredentialError` only when a call is made (as ``lib.wp_client`` does).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final, cast

import httpx
from pydantic import ValidationError

from lib import client_files
from lib.config import GeneratorConfig
from lib.errors import GeneratorError, LLMAPIError, MissingCredentialError
from lib.generator import MODE_TIGHTEN, MODE_TRANSLATE, GenerationRequest, GenerationResult
from lib.language_check import (
    VERDICT_DIFFERS,
    VERDICT_WRONG_LANGUAGE,
    CheckRequest,
    CheckResult,
    Finding,
)

_log = logging.getLogger(__name__)

#: The Anthropic Messages endpoint and the pinned API version header.
MESSAGES_URL: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2023-06-01"
_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

#: Retry budget for 429/5xx, mirroring ``lib.wp_client``/``lib.gs1_dl_client`` at a smaller scale.
_RETRY_MAX_ATTEMPTS: Final = 4
_RETRY_BASE_SECONDS: Final = 1.0
_RETRY_MAX_SECONDS: Final = 30.0

#: Longest error body kept on a raised :class:`LLMAPIError` (bodies are unbounded otherwise).
_MAX_ERROR_BODY: Final = 500

#: The forced tool the model answers through — a strict schema keeps the result parseable.
_TOOL_NAME: Final = "produce_copy"
_PRODUCE_COPY_TOOL: Final[dict[str, Any]] = {
    "name": _TOOL_NAME,
    "description": (
        "Return the product's ranked USP list: usps[0] is the tagline, usps[1:] are the "
        "Eigenschappen benefit bullets. Include translations only for the fields the request "
        "lists as missing in this language. Declare in `inferences` every claim you made that "
        "the feed does not literally state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "usps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Ranked USPs; [0] the tagline, the rest Eigenschappen bullets.",
            },
            "translations": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Field name → that field's value rendered in this request's language. One "
                    "key per field the request lists as missing, and no others. Translate the "
                    "source text given; do not invent a value or elaborate on it."
                ),
            },
            "inferences": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One entry per claim in `usps` that the inputs do not literally state, each "
                    'naming the claim and what it was inferred from — e.g. "Herbruikbaar: '
                    'eigenschap van microvezel, niet in 1083." Product knowledge a buyer would '
                    "take for granted still counts: the test is whether the words are in the "
                    "feed, not whether the claim is true. Say nothing here about wording drawn "
                    "straight from the inputs. Empty list if every claim is literal."
                ),
            },
        },
        "required": ["usps"],
    },
}


#: The forced tool the cross-language check answers through (:mod:`lib.language_check`).
_COMPARE_TOOL_NAME: Final = "report_language_disagreements"
#: The check's own output budget: one note per finding across up to six fields outgrows the
#: copy generator's 1024, and a cut-off answer must never be stored as a complete one.
_COMPARE_MAX_TOKENS: Final = 4096
_COMPARE_TOOL: Final[dict[str, Any]] = {
    "name": _COMPARE_TOOL_NAME,
    "description": (
        "Report every field whose languages disagree, and every value that is not written in "
        "the language of its slot. Return an empty list when everything agrees."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "The field name as given."},
                        "language": {
                            "type": "string",
                            "description": (
                                "The language slot the finding is about: the one that says "
                                "something else than the reference language, or the one "
                                "holding text in another language."
                            ),
                        },
                        "verdict": {"type": "string", "enum": ["differs", "wrong_language"]},
                        "note": {
                            "type": "string",
                            "description": (
                                "One plain English sentence a product manager can act on: what "
                                "this slot says that the reference does not, or which language "
                                "it is actually in."
                            ),
                        },
                    },
                    "required": ["field", "language", "verdict", "note"],
                },
            }
        },
        "required": ["findings"],
    },
}

#: The system prompt for the check. Fixed in code rather than a client file: it is a review
#: instruction, not a voice, and it is versioned by :data:`lib.language_check.CHECK_VERSION`.
_COMPARE_SYSTEM: Final = (
    "You review product master data that a brand owner entered separately per language in GS1. "
    "For each field you get the value in every language. The reference language is the one the "
    "data is authored in.\n"
    "Report `differs` when a language's value states something the reference does not, or "
    "leaves out something that changes what a buyer would understand: another product, another "
    "use, another target (people vs. pets), another quantity, a claim the reference does not "
    "make. A faithful translation, a shorter or longer wording of the same meaning, or ordinary "
    "marketing tone is NOT a difference. Names that are brand or model names (the same in every "
    "language) are not a difference either.\n"
    "Report `wrong_language` when a value is written in another language than its slot — e.g. "
    "English or Dutch in a French slot. Judge each value's language on its own, before its "
    "meaning: a Dutch word in a French slot is `wrong_language` even when it means the same as "
    "the reference. A name spelled identically in every language is a brand or model name, "
    "never `wrong_language`.\n"
    "Be strict about meaning and lenient about wording. When unsure, do not report."
)


# --- Voice template ----------------------------------------------------------


def voice_template_path(client_id: str, prompt_version: str) -> Path:
    """Return the versioned voice template path for a client.

    The data folder's ``input/{client}/prompts/`` first, then the release's ``prompts/{client}/``
    (see :mod:`lib.client_files`). When neither has the file, the data folder's path is returned,
    so the error names where it belongs.
    """
    candidates = client_files.voice_prompt_candidates(client_id, prompt_version)
    return next((path for path in candidates if path.is_file()), candidates[0])


def load_voice_template(client_id: str, prompt_version: str) -> str:
    """Load a client's few-shot voice template for a prompt version.

    Args:
        client_id: The client whose voice to load.
        prompt_version: The active prompt version (selects the file).

    Returns:
        The template text (used as the Messages API system prompt).

    Raises:
        GeneratorError: If the template for this version is missing or empty. A version bump
            without its voice file must fail loudly, not silently fall back to a stale voice.
    """
    path = voice_template_path(client_id, prompt_version)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        tried = " or ".join(
            str(p) for p in client_files.voice_prompt_candidates(client_id, prompt_version)
        )
        raise GeneratorError(
            f"no voice template for {client_id!r} prompt_version {prompt_version!r}; "
            f"looked for {tried}"
        ) from exc
    if not text.strip():
        raise GeneratorError(f"voice template at {path} is empty")
    return text


# --- Payload & parsing (pure) ------------------------------------------------


def _render_request(request: GenerationRequest) -> str:
    """Render one request into the deterministic user message (fixed field order)."""
    inputs = request.inputs
    lines = [f"GTIN {request.gtin}, language: {request.language}.", f"Mode: {request.mode}."]
    if request.mode == MODE_TRANSLATE:
        lines.append(
            f"Translate this {request.source_language} copy into {request.language}, line for "
            f"line: return exactly {len(request.source_usps)} usps, in the same order, the first "
            "being the tagline. Keep each line's meaning exactly — add nothing, drop nothing. "
            "The inputs below are context for the translation, not material for new claims:"
        )
        lines.extend(f"  - {line}" for line in request.source_usps)
    elif request.mode == MODE_TIGHTEN:
        lines.append("Shorten and rank these existing feature/benefit lines (keep their meaning):")
        lines.extend(f"  - {candidate}" for candidate in request.candidates)
    else:
        lines.append(
            "Write from the marketing message below; if it is blank, write minimally from the "
            "functional name."
        )
    if request.translations:
        lines.append(
            f"The feed carries these values in another language but not in {request.language}. "
            f"Return one `translations` key per field, translated into {request.language}, "
            "keeping the source's meaning exactly — these go back into the product datapool:"
        )
        lines.extend(
            f"  - {gap.field} (from {gap.source_language}): {gap.source_value}"
            for gap in request.translations
        )
    lines.append("Inputs:")
    lines.append(f"  - functional name: {inputs.functional_name or '(none)'}")
    lines.append(f"  - marketing message (1083): {inputs.marketing_message or '(none)'}")
    if inputs.feature_benefit:
        lines.append(f"  - feature/benefit (1067): {inputs.feature_benefit}")
    context: list[str] = []
    if inputs.net_content:
        context.append(f"net content {inputs.net_content}")
    dims = [inputs.dim_height, inputs.dim_width, inputs.dim_depth]
    if all(dims):
        context.append(f"dimensions {inputs.dim_height} × {inputs.dim_width} × {inputs.dim_depth}")
    if inputs.material:
        context.append(f"material {inputs.material}")
    if context:
        lines.append(
            "Context (do NOT put these in usps — the specs block is added separately): "
            + ", ".join(context)
        )
    return "\n".join(lines)


def _build_payload(
    config: GeneratorConfig, voice: str, request: GenerationRequest
) -> dict[str, Any]:
    """Assemble the Messages API request body for one generation (no sampling, forced tool).

    Two deliberate omissions and one deliberate addition, all of them model-surface facts rather
    than preferences:

    - **No ``temperature``/``top_p``/``top_k``.** Non-default sampling parameters are rejected with
      a 400 on ``claude-sonnet-5`` and every Opus 4.7-and-later model.
    - **``thinking`` is disabled explicitly, not omitted.** Omitting it means *adaptive thinking
      on* for these models, and ``max_tokens`` caps thinking and response together — so the 1024
      that comfortably holds a tagline plus three bullets could be spent reasoning instead, and
      the forced tool call would truncate. Forced ``tool_choice`` alongside disabled thinking is
      fine on the first-party API; only Bedrock couples the two.
    """
    return {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "thinking": {"type": "disabled"},
        "system": voice,
        "messages": [{"role": "user", "content": _render_request(request)}],
        "tools": [_PRODUCE_COPY_TOOL],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def _parse_result(data: dict[str, Any]) -> GenerationResult:
    """Extract the ``produce_copy`` tool call from a Messages API response.

    Raises:
        LLMAPIError: If the response carries no ``produce_copy`` tool call, or its input fails the
            :class:`~lib.generator.GenerationResult` contract (e.g. empty ``usps``).
    """
    for block in data.get("content", []):
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == _TOOL_NAME
        ):
            payload = block.get("input") or {}
            try:
                return GenerationResult(
                    usps=payload["usps"],
                    translations=payload.get("translations") or {},
                    inferences=payload.get("inferences") or [],
                )
            except (KeyError, ValidationError) as exc:
                raise LLMAPIError(
                    HTTPStatus.OK,
                    json.dumps(payload)[:_MAX_ERROR_BODY],
                    f"malformed produce_copy input: {exc}",
                ) from exc
    raise LLMAPIError(
        HTTPStatus.OK,
        json.dumps(data)[:_MAX_ERROR_BODY],
        "Anthropic response had no produce_copy tool call",
    )


def _render_check(request: CheckRequest) -> str:
    """Render one cross-language check into the user message (config order, reference first).

    The reference is named per field: the default language where the field has a value in it,
    otherwise the first language that does — so "differs" always has something to differ from.
    """
    lines = [f"GTIN {request.gtin}. Reference language: {request.default_language}.", "Fields:"]
    for field in request.fields:
        reference = (
            request.default_language
            if request.default_language in field.values
            else next(iter(field.values))
        )
        lines.append(f"- {field.field} ({field.label}), reference {reference}:")
        ordered = sorted(field.values.items(), key=lambda kv: kv[0] != reference)
        lines.extend(f"    {lang}: {value}" for lang, value in ordered)
    return "\n".join(lines)


def _build_check_payload(config: GeneratorConfig, request: CheckRequest) -> dict[str, Any]:
    """The Messages API body for one check — the same model surface as :func:`_build_payload`."""
    return {
        "model": config.model,
        "max_tokens": max(config.max_tokens, _COMPARE_MAX_TOKENS),
        "thinking": {"type": "disabled"},
        "system": _COMPARE_SYSTEM,
        "messages": [{"role": "user", "content": _render_check(request)}],
        "tools": [_COMPARE_TOOL],
        "tool_choice": {"type": "tool", "name": _COMPARE_TOOL_NAME},
    }


def _findings_list(payload: dict[str, Any]) -> list[Any]:
    """The ``findings`` array, also when the model sent it JSON-encoded as a string.

    Seen live: one answer in 49 carried ``"findings": "{\"findings\": [...]}"`` — the whole
    object serialised into the field. Decoding it recovers a well-formed answer; anything else
    that is not a list raises, and the caller turns that into an :class:`LLMAPIError`.
    """
    findings = payload["findings"]
    if isinstance(findings, str):
        findings = json.loads(findings)
        if isinstance(findings, dict):
            findings = findings["findings"]
    if not isinstance(findings, list):
        raise TypeError(f"findings is a {type(findings).__name__}, not a list")
    return findings


def _parse_check(data: dict[str, Any]) -> CheckResult:
    """Extract the check's tool call. An unknown verdict is a malformed answer, not a finding.

    Raises:
        LLMAPIError: No tool call, an answer cut off at ``max_tokens`` (a shorter list of
            findings that would be kept as the whole judgment), or input that fails the
            :class:`CheckResult` contract.
    """
    if data.get("stop_reason") == "max_tokens":
        raise LLMAPIError(
            HTTPStatus.OK,
            json.dumps(data)[:_MAX_ERROR_BODY],
            f"{_COMPARE_TOOL_NAME} answer was cut off at max_tokens",
        )
    for block in data.get("content", []):
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == _COMPARE_TOOL_NAME
        ):
            payload = block.get("input") or {}
            try:
                findings = [Finding.model_validate(item) for item in _findings_list(payload)]
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise LLMAPIError(
                    HTTPStatus.OK,
                    json.dumps(payload)[:_MAX_ERROR_BODY],
                    f"malformed {_COMPARE_TOOL_NAME} input: {exc}",
                ) from exc
            unknown = {f.verdict for f in findings} - {VERDICT_DIFFERS, VERDICT_WRONG_LANGUAGE}
            if unknown:
                raise LLMAPIError(
                    HTTPStatus.OK,
                    json.dumps(payload)[:_MAX_ERROR_BODY],
                    f"unknown verdict(s) {sorted(unknown)} in {_COMPARE_TOOL_NAME} input",
                )
            return CheckResult(findings=findings)
    raise LLMAPIError(
        HTTPStatus.OK,
        json.dumps(data)[:_MAX_ERROR_BODY],
        f"Anthropic response had no {_COMPARE_TOOL_NAME} tool call",
    )


def _is_retryable(status: int) -> bool:
    """Whether an HTTP status warrants a retry (429 or any 5xx)."""
    return status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR


def _backoff(attempt: int) -> float:
    """Exponential backoff seconds for a 1-indexed attempt, capped."""
    return min(_RETRY_BASE_SECONDS * 2.0 ** (attempt - 1), _RETRY_MAX_SECONDS)


def _require_env(name: str) -> str:
    """Read an environment variable, raising a typed error if it is unset **or blank**.

    Blank counts as unset here, unlike the sibling copies in ``lib.wp_client`` and
    ``lib.gs1_dl_client``, because this is the one credential ``.env`` ships a scaffold line for:
    ``ANTHROPIC_API_KEY=`` is written by ``.env.example`` and left in place by an operator who has
    not filled it in. ``os.environ[name]`` succeeds on that, so without this the client would send
    an empty ``x-api-key``, spend a round trip, and report **401 from Anthropic** — an error about
    a rejected key rather than about a key nobody set. ``ui.env_edit.describe`` already treats the
    two as one failure, for the same reason: they are the same failure to the person who has to
    fix it.
    """
    value = os.environ.get(name, "")
    if not value.strip():
        raise MissingCredentialError(f"Environment variable {name!r} is not set")
    return value


# --- The client --------------------------------------------------------------


class AnthropicClient:
    """Synchronous Anthropic Messages API producer implementing :class:`lib.generator.LLMClient`.

    Args:
        config: The client's generator config (model, key env var, max tokens).
        voice_template: The system prompt / few-shot voice for the active prompt version.
        timeout: Override the default httpx timeouts (tests).
        sleep: Injectable sleep so retry backoff is instant in tests.
        base_url: Override the Messages endpoint (tests).
    """

    def __init__(
        self,
        config: GeneratorConfig,
        voice_template: str,
        *,
        timeout: httpx.Timeout | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = MESSAGES_URL,
    ) -> None:
        self.config = config
        self._voice = voice_template
        self._http = httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT)
        self._sleep = sleep
        self._url = base_url

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> AnthropicClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def generate_copy(self, request: GenerationRequest) -> GenerationResult:
        """Produce copy for one ``(gtin, language)`` via one forced-tool Messages call.

        Raises:
            MissingCredentialError: The configured API-key env var is unset.
            LLMAPIError: The call failed (transport, non-success status) or returned no usable
                ``produce_copy`` result.
        """
        payload = _build_payload(self.config, self._voice, request)
        data = self._request(payload)
        return _parse_result(data)

    def compare_languages(self, request: CheckRequest) -> CheckResult:
        """Judge whether one product's languages say the same (:mod:`lib.language_check`).

        Raises:
            MissingCredentialError: The configured API-key env var is unset.
            LLMAPIError: The call failed or returned no usable answer.
        """
        return _parse_check(self._request(_build_check_payload(self.config, request)))

    def _headers(self) -> dict[str, str]:
        """Build the request headers; the key is read lazily so a missing secret surfaces here."""
        return {
            "x-api-key": _require_env(self.config.api_key_env),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to the Messages API with retry on 429/5xx and transport errors."""
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                response = self._http.post(self._url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                if attempt >= _RETRY_MAX_ATTEMPTS:
                    raise LLMAPIError(
                        0,
                        str(exc)[:_MAX_ERROR_BODY],
                        f"Anthropic API request failed: {exc}",
                        call=f"POST {self._url}",
                    ) from exc
                self._sleep(_backoff(attempt))
                continue
            if response.status_code == HTTPStatus.OK:
                try:
                    return cast("dict[str, Any]", response.json())
                except ValueError as exc:
                    raise LLMAPIError(
                        HTTPStatus.OK,
                        response.text[:_MAX_ERROR_BODY],
                        "Anthropic returned a 200 whose body is not JSON",
                    ) from exc
            if _is_retryable(response.status_code) and attempt < _RETRY_MAX_ATTEMPTS:
                _log.warning(
                    "Anthropic API %s on attempt %d; retrying", response.status_code, attempt
                )
                self._sleep(_backoff(attempt))
                continue
            raise LLMAPIError(
                response.status_code,
                response.text[:_MAX_ERROR_BODY],
                call=f"POST {self._url}",
            )
        raise LLMAPIError(0, "", "Anthropic API retries exhausted")  # pragma: no cover
