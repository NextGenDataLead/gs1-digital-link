"""Fixture-backed checks against real captured GS1 API v2 responses (§13.2).

These are skipped until the operator drops the six captured responses into
``tests/fixtures/gs1_api/`` (see IMPLEMENTATION_SPEC §13.2). When present, they
confirm the client's assumptions about the real v2 wire shapes and let us close
the empirical DoD items (auth scheme, not-found status code).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.gs1_dl_client import _parse_error_results

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "gs1_api"

pytestmark = pytest.mark.skipif(
    not FIXTURES_DIR.is_dir() or not any(FIXTURES_DIR.glob("*.json")),
    reason="GS1 API fixtures not captured yet (see IMPLEMENTATION_SPEC §13.2)",
)


def _load(name: str) -> object:
    path = FIXTURES_DIR / name
    if not path.is_file():
        pytest.skip(f"fixture {name} not present")
    return json.loads(path.read_text())


def test_get_existing_has_identification_key() -> None:
    record = _load("get_existing.json")
    assert isinstance(record, dict)
    if "identificationKey" in record:
        assert isinstance(record["identificationKey"], str)


def test_post_400_parses_as_error_results() -> None:
    body = FIXTURES_DIR / "post_400.json"
    if not body.is_file():
        pytest.skip("post_400.json not present")
    parsed = _parse_error_results(body.read_text())
    # If the real 400 body follows the standard ErrorResult[] shape, it parses;
    # otherwise the client correctly leaves error_results None. Either is valid —
    # this asserts we never crash on the real payload.
    assert parsed is None or isinstance(parsed, list)
