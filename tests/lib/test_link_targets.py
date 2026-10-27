"""Whether a GS1 record's target loads — the Data screen's check for a links-only batch.

Driven through ``httpx.MockTransport``: no network, and the redirect-following is httpx's own.
"""

from __future__ import annotations

import httpx
import pytest

from lib.link_targets import check_targets, check_url, host_problem

_SITE = "https://www.democlient.nl"


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler, follow_redirects=True)


def _answer(status: int, *, location: str | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if location and request.url.path != httpx.URL(location).path:
            return httpx.Response(301, headers={"location": location})
        return httpx.Response(status)

    return httpx.MockTransport(handle)


def test_a_page_that_loads_has_no_problem() -> None:
    assert check_url(f"{_SITE}/a/", client=_client(_answer(200))) is None


def test_a_missing_page_says_404() -> None:
    """``…/screw-remove-tool-2/st`` on the pilot's list: a stray suffix, and a 404."""
    problem = check_url(f"{_SITE}/screw-remove-tool-2/st", client=_client(_answer(404)))

    assert problem == "the page does not exist (404)"


def test_a_redirect_is_followed_to_where_a_visitor_lands() -> None:
    """Stricter than the run's HEAD on purpose: a 301 to a 404 must not read as a page."""
    client = _client(_answer(404, location=f"{_SITE}/gone/"))

    assert check_url(f"{_SITE}/old/", client=client) == "the page does not exist (404)"


def test_a_server_that_refuses_head_is_asked_with_get() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405 if request.method == "HEAD" else 200)

    assert check_url(f"{_SITE}/a/", client=_client(httpx.MockTransport(handle))) is None


def test_a_timeout_is_a_problem_not_an_exception() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    problem = check_url(f"{_SITE}/a/", client=_client(httpx.MockTransport(handle)))

    assert problem == "the page did not answer in time"


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        (f"{_SITE}/democlient/notenkraker-2/", None),
        ("https://WWW.DEMOCLIENT.NL/x/", None),
        ("https://example.com/x/", "not on the client's site (www.democlient.nl)"),
        ("www.democlient.nl/x/", "not a web address"),
    ],
)
def test_only_the_clients_own_site_is_a_target(url: str, problem: str | None) -> None:
    assert host_problem(url, _SITE) == problem


def test_each_address_is_checked_once() -> None:
    asked: list[str] = []

    def checker(url: str) -> str | None:
        asked.append(url)
        return None if url.endswith("ok") else "broken"

    found = check_targets(["a/ok", "b/bad", "a/ok"], checker)

    assert found == {"a/ok": None, "b/bad": "broken"}
    assert sorted(asked) == ["a/ok", "b/bad"]
    assert check_targets([], checker) == {}
