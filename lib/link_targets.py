"""Whether a page a GS1 record would point at actually loads — checked before anyone ticks it.

A links-only batch writes one thing per product: a Digital Link record, which can never be deleted,
pointing at a page that already exists. So the only thing that can be wrong with such a product is
its target — the address in the selection list's *Link naar site* column, or the page this tool
published for it. This module answers that question for the Data screen, per product, so a bad
address is an issue in a table *before* the batch is saved rather than a refusal halfway through a
run.

**It is not the run's check, and does not replace it.** ``run_execute --only links`` still refuses a
target that does not serve, with the WordPress client, immediately before each GS1 write — that is
the invariant, because a screen can be skipped and the run cannot. This one is stricter on purpose:
redirects are followed and the page at the end must answer 2xx, so an address that passes here
passes there.

No credentials: it reads what any visitor is served, which is what a scan of the QR code will reach.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from urllib.parse import urlsplit

import httpx

#: Per request. A page that takes longer than this to answer is not one to send a scan to anyway.
_TIMEOUT: Final = 10.0
#: How many addresses are checked at once — enough to make 40 quick, few enough to be polite.
_WORKERS: Final = 8
_HTTP_OK_MIN: Final = 200
_HTTP_OK_MAX: Final = 300
_METHOD_NOT_ALLOWED: Final = 405

#: ``url -> None`` when it loads, else the reason it does not, in words an operator can act on.
Problems = dict[str, str | None]

#: One address in, its problem (or ``None``) out. Injectable so the screen's tests need no network.
Checker = Callable[[str], str | None]


def host_problem(url: str, site_url: str) -> str | None:
    """Why ``url`` cannot be a record's target on ``site_url``'s host, or ``None`` when it can.

    The tool points GS1 at the client's own site, never at an address someone pasted from
    elsewhere — :func:`lib.process_list.load_listed_targets` refuses the same thing at run time.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "not a web address"
    site_host = (urlsplit(site_url).hostname or "").lower()
    if parts.hostname.lower() != site_host:
        return f"not on the client's site ({site_host})"
    return None


def check_url(url: str, *, client: httpx.Client | None = None) -> str | None:
    """Whether ``url`` loads for a visitor: ``None`` when it does, else why not."""
    own = client is None
    http = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        response = http.head(url)
        if response.status_code == _METHOD_NOT_ALLOWED:
            response = http.get(url)
    except httpx.TimeoutException:
        return "the page did not answer in time"
    except httpx.HTTPError as exc:
        return f"the page could not be reached ({type(exc).__name__})"
    finally:
        if own:
            http.close()
    if _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
        return None
    if response.status_code == httpx.codes.NOT_FOUND:
        return "the page does not exist (404)"
    return f"the page answers {response.status_code}"


def check_targets(urls: Iterable[str], checker: Checker = check_url) -> Problems:
    """Check every distinct address once, a few at a time. Never raises."""
    distinct = sorted(set(urls))
    if not distinct:
        return {}
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(distinct))) as pool:
        return dict(zip(distinct, pool.map(checker, distinct), strict=True))
