"""Unit tests for secret/PII scrubbing (IMPLEMENTATION_SPEC §5.2)."""

from __future__ import annotations

import json

from lib.logging_setup import REDACTED, scrub_headers, scrub_response_body


def test_redacts_token_and_secret_values() -> None:
    body = json.dumps({"token": "super-secret", "clientSecret": "hunter2", "ok": True})

    scrubbed = json.loads(scrub_response_body(body))

    assert scrubbed["token"] == REDACTED
    assert scrubbed["clientSecret"] == REDACTED
    assert scrubbed["ok"] is True


def test_preserves_gs1_identification_key() -> None:
    # identificationKey / identificationKeyType hold the GTIN, not a secret.
    body = json.dumps({"identificationKey": "08712345678905", "identificationKeyType": "Gtin"})

    scrubbed = json.loads(scrub_response_body(body))

    assert scrubbed["identificationKey"] == "08712345678905"
    assert scrubbed["identificationKeyType"] == "Gtin"


def test_redacts_credential_key_names() -> None:
    body = json.dumps({"api_key": "abc", "subscription_key": "def"})

    scrubbed = json.loads(scrub_response_body(body))

    assert scrubbed["api_key"] == REDACTED
    assert scrubbed["subscription_key"] == REDACTED


def test_redacts_nested_meta_subtree() -> None:
    body = json.dumps({"id": 42, "meta": {"gtin": "08712345678905", "x": "y"}})

    scrubbed = json.loads(scrub_response_body(body))

    assert scrubbed["id"] == 42
    assert scrubbed["meta"] == REDACTED


def test_scrubs_nested_structures() -> None:
    body = json.dumps({"outer": {"password": "p", "list": [{"token": "t"}]}})

    scrubbed = json.loads(scrub_response_body(body))

    assert scrubbed["outer"]["password"] == REDACTED
    assert scrubbed["outer"]["list"][0]["token"] == REDACTED


def test_non_json_body_falls_back_to_regex() -> None:
    body = 'garbage "token": "leaked-value" trailing'

    scrubbed = scrub_response_body(body)

    assert "leaked-value" not in scrubbed
    assert REDACTED in scrubbed


def test_scrub_headers_redacts_authorization() -> None:
    headers = {"Authorization": "Bearer sekret", "Content-Type": "application/json"}

    scrubbed = scrub_headers(headers)

    assert scrubbed["Authorization"] == REDACTED
    assert scrubbed["Content-Type"] == "application/json"
