"""Where a scan lands, per language, once a links run has written the record.

A successful GS1 write does not say which page a phone reaches. The resolver picks the page from
the phone's language, and *how* it picks is GS1's to decide, not this tool's: the 38 records
written on 2026-10-07/08 sent every French phone to the Dutch page, and the 25 written on
2026-10-09 — the same payload — sent it to the French one. Nothing here noticed; an operator
did, by scanning. Rewriting the 38 on 2026-10-25 fixed them, and that is the point of this
module: after every links run, ask the public resolver, the way a phone does, and say so when a
language lands somewhere other than its own page.

Two things make the asking awkward, and both are handled here rather than by the caller:

* **GET, never HEAD** — ``id.gs1.org`` answers HEAD with 404 and GET with the 307 a phone follows.
* **A fresh write takes a while to show.** The public resolver served the old record for about
  half a minute after GS1 NL accepted the new one. An immediate check would report a correct
  run as wrong, so :func:`check_until_settled` asks again until every landing is right or the
  wait runs out, and only then reports what is still wrong.

The result is a warning, never a failure: the record was written and the page serves. What it
asks for is a person's look — or the next run, when GS1 has caught up.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx

#: Seconds to keep asking before reporting what is still wrong. Measured lag was ~30 s.
SETTLE_TIMEOUT_SECONDS: Final = 120.0
#: Seconds between rounds while a landing is still wrong.
SETTLE_INTERVAL_SECONDS: Final = 15.0

_TIMEOUT: Final = httpx.Timeout(15.0)

#: ``(status, location)`` for one GET of a Digital Link URI in one language.
Fetch = Callable[[str, str], tuple[int | None, str | None]]


@dataclass(frozen=True)
class Landing:
    """Where one language's scan of one GTIN went, against where the run pointed it."""

    gtin: str
    language: str
    expected: str
    status: int | None
    location: str | None

    @property
    def ok(self) -> bool:
        """Whether the resolver redirected this language to its own page."""
        return self.location is not None and same_page(self.location, self.expected)

    def describe(self) -> str:
        """One line for the operator: which product, which language, where it went instead."""
        if self.location is None:
            got = f"no redirect (status {self.status})" if self.status else "no answer"
        else:
            got = self.location
        return f"{self.gtin} ({self.language}): expected {self.expected}, scan went to {got}"


def same_page(a: str, b: str) -> bool:
    """Whether two addresses name one page: scheme, host case, query and trailing slash aside."""

    def key(url: str) -> tuple[str, str]:
        parts = urlsplit(url.strip())
        return ((parts.hostname or "").lower(), parts.path.rstrip("/") or "/")

    return key(a) == key(b)


def accept_language(language: str) -> str:
    """The header a phone set to ``language`` sends: the regional form first, then the plain one.

    That is what phones send, and it is the form the resolver matches — a bare regional code
    (``fr-BE`` alone) falls through to the default link. Checking the plain form only would pass
    a record a real phone does not reach.
    """
    return f"{language}-BE,{language};q=0.9"


def fetch_landing(uri: str, language: str) -> tuple[int | None, str | None]:
    """GET ``uri`` as a phone set to ``language`` would, without following the redirect."""
    try:
        response = httpx.get(
            uri,
            headers={"Accept-Language": accept_language(language)},
            follow_redirects=False,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        return None, None
    return response.status_code, response.headers.get("location")


def check_once(
    targets: Mapping[str, Mapping[str, str]],
    uri_for: Callable[[str], str],
    fetch: Fetch,
) -> list[Landing]:
    """Scan every ``(gtin, language)`` in ``targets`` once.

    Args:
        targets: GTIN → language → the page the run pointed that language at.
        uri_for: The Digital Link URI for a GTIN (the one printed in its QR).
        fetch: ``(uri, language) -> (status, location)``; :func:`fetch_landing` in a real run.
    """
    return [
        Landing(gtin, language, expected, *fetch(uri_for(gtin), language))
        for gtin, pages in sorted(targets.items())
        for language, expected in sorted(pages.items())
    ]


def check_until_settled(  # noqa: PLR0913 — the clock and the sleep are injected for the tests
    targets: Mapping[str, Mapping[str, str]],
    uri_for: Callable[[str], str],
    fetch: Fetch,
    *,
    timeout: float = SETTLE_TIMEOUT_SECONDS,
    interval: float = SETTLE_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[Landing]:
    """Scan until every landing is right or ``timeout`` passes; return the last answer for each.

    Only the wrong ones are asked again, so a large run settles in a handful of requests per
    round rather than one per product.
    """
    deadline = clock() + timeout
    settled: dict[tuple[str, str], Landing] = {}
    pending: dict[str, dict[str, str]] = {g: dict(p) for g, p in targets.items() if p}
    while True:
        for landing in check_once(pending, uri_for, fetch):
            settled[(landing.gtin, landing.language)] = landing
        pending = {}
        for (gtin, language), landing in settled.items():
            if not landing.ok:
                pending.setdefault(gtin, {})[language] = landing.expected
        if not pending or clock() >= deadline:
            return [settled[key] for key in sorted(settled)]
        sleep(interval)


def wrong(landings: Sequence[Landing]) -> list[Landing]:
    """The landings that did not reach their own page."""
    return [landing for landing in landings if not landing.ok]
