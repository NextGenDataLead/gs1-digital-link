"""Logging helpers, including PII/secret scrubbing for log output.

``docs/IMPLEMENTATION_SPEC.md`` §5.2 requires that response bodies and request
headers be scrubbed of secrets before they reach any log sink. This module
provides the scrubbers; the full run-log setup (``setup_logging`` writing to
``output/{client_id}/runs/{ts}.log``, §4.10) lands in a later phase.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

REDACTED: Final = "[REDACTED]"

# Substrings that mark a key's value as sensitive. These four never collide with
# GS1/WordPress domain field names, so a plain substring match is safe.
_SENSITIVE_SUBSTRINGS: Final = ("password", "secret", "token", "authorization")

# "key" is handled by an exact-name set rather than a substring match: the GS1
# API uses ``identificationKey`` / ``identificationKeyType`` for the (non-secret)
# GTIN, which a substring match on "key" would wrongly redact. Only genuine
# credential-style key names are listed here.
_SENSITIVE_KEY_NAMES: Final = frozenset(
    {
        "key",
        "apikey",
        "api_key",
        "access_key",
        "private_key",
        "secret_key",
        "subscription_key",
        "ocp-apim-subscription-key",
    }
)

# Keys whose entire nested value is redacted wholesale (WordPress ``meta.*``).
_REDACT_SUBTREE_KEYS: Final = frozenset({"meta"})

# Sensitive HTTP header names (lower-cased) whose values must never be logged.
_SENSITIVE_HEADERS: Final = frozenset({"authorization", "ocp-apim-subscription-key", "x-api-key"})

# Fallback for non-JSON bodies: redact ``"<sensitive-key>": "<value>"`` pairs.
_JSON_PAIR_RE: Final = re.compile(
    r'("(?:[^"\\]|\\.)*?(?:password|secret|token|authorization|api[_-]?key)'
    r'[^"\\]*?"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if any(sub in lowered for sub in _SENSITIVE_SUBSTRINGS):
        return True
    return lowered in _SENSITIVE_KEY_NAMES


def _scrub_value(key: str, value: object) -> object:
    """Return a scrubbed copy of ``value`` given its parent ``key``."""
    if key.lower() in _REDACT_SUBTREE_KEYS:
        return REDACTED
    if _is_sensitive_key(key):
        return REDACTED
    return _scrub_node(value)


def _scrub_node(node: object) -> object:
    if isinstance(node, dict):
        return {k: _scrub_value(str(k), v) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_node(item) for item in node]
    return node


def scrub_response_body(body: str) -> str:
    """Redact secret values from an HTTP response body before logging.

    Values of keys matching ``password``, ``secret``, ``token``, ``authorization``,
    or a credential-style ``key`` name are replaced with ``[REDACTED]``, as is any
    value nested under a ``meta`` key (WordPress). JSON bodies are parsed and
    scrubbed structurally; non-JSON bodies fall back to a regex over
    ``"key": "value"`` pairs.

    Args:
        body: The raw response body text.

    Returns:
        The body with sensitive values redacted. Structure is preserved for JSON;
        non-JSON input is returned with best-effort pair redaction.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _JSON_PAIR_RE.sub(r'\1"' + REDACTED + '"', body)
    return json.dumps(_scrub_node(parsed))


def scrub_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with sensitive header values redacted.

    Args:
        headers: The request/response headers.

    Returns:
        A new dict where the values of ``Authorization`` and other credential
        headers are replaced with ``[REDACTED]``. Header names are preserved.
    """
    return {
        name: (REDACTED if name.lower() in _SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }
