"""Unit tests for the API error messages (IMPLEMENTATION_SPEC §5.2, issue #60).

``run_execute`` records a failed row as ``repr(exc)``, so the message *is* the run log. These
tests pin the three things that were missing when a live ``403`` was recorded as
``WordPressAPIError('WordPress API error 403')``: the failing call, the server's answer, and
the bound and scrubbing that make it safe to carry either.
"""

from __future__ import annotations

import json

from lib.errors import ERROR_BODY_LIMIT, GS1APIError, LLMAPIError, WordPressAPIError
from lib.logging_setup import REDACTED

_MEDIA_CALL = "POST /wp-json/wp/v2/media (upload media hero-a1b2c3d4e5f6)"


def test_wordpress_error_names_the_call_and_quotes_the_body() -> None:
    # The failure from issue #60: a security plugin's HTML block page, not WordPress REST JSON.
    error = WordPressAPIError(403, "<!DOCTYPE html><title>403 Forbidden</title>", call=_MEDIA_CALL)

    recorded = repr(error)  # exactly what run_execute writes to runs/*.jsonl

    assert "WordPress API error 403" in recorded
    assert _MEDIA_CALL in recorded
    assert "403 Forbidden" in recorded
    assert error.call == _MEDIA_CALL


def test_wordpress_error_without_a_call_or_body_is_unchanged() -> None:
    # Guards (E11) and bodyless responses must not gain a dangling " on " or ": ".
    assert str(WordPressAPIError(409, "")) == "WordPress API error 409"


def test_body_excerpt_is_scrubbed_but_response_body_is_raw() -> None:
    # The scrub is not optional on this path: the message reaches the run log, which is a file.
    body = json.dumps({"code": "rest_invalid", "token": "leaked-in-body"})

    error = WordPressAPIError(400, body, call="POST /wp-json/wp/v2/pages (create page p-1)")

    assert "leaked-in-body" not in str(error)
    assert REDACTED in str(error)
    # Still available unscrubbed for programmatic inspection — that contract is unchanged.
    assert error.response_body == body


def test_long_body_is_truncated_and_says_so() -> None:
    error = WordPressAPIError(500, "x" * (ERROR_BODY_LIMIT * 2))

    message = str(error)

    assert message.endswith("…")
    # The bound applies to the excerpt, not to the whole message.
    assert len(message) < ERROR_BODY_LIMIT * 2


def test_multiline_body_is_collapsed_to_one_line() -> None:
    # An HTML block page is many indented lines; a run log line must stay one line, and the
    # identifying part must survive the bound rather than be spent on whitespace.
    error = WordPressAPIError(403, "<html>\n  <head>\n    <title>Blocked</title>\n")

    message = str(error)

    assert "\n" not in message
    assert "<title>Blocked</title>" in message


def test_gs1_error_keeps_request_id_alongside_the_call() -> None:
    error = GS1APIError(
        400,
        '[{"identifier": "087", "errors": [{"code": "21011", "message": "no contract"}]}]',
        request_id="req-7",
        call="POST /digitallinkv2/v2/digitalLink (gtin 02987653007717)",
    )

    message = str(error)

    assert "request_id=req-7" in message
    assert "gtin 02987653007717" in message
    assert "21011" in message


def test_llm_error_keeps_its_message_override() -> None:
    error = LLMAPIError(200, json.dumps({"usps": []}), "malformed produce_copy input: usps")

    message = str(error)

    assert message.startswith("malformed produce_copy input: usps")
    assert "usps" in message
