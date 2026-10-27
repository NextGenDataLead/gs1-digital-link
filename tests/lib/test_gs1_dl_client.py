"""Tests for the GS1 Digital Link API v2 client (IMPLEMENTATION_SPEC §4.2-4.3, §5, §6.3).

Auth is OAuth2 client-credentials: the client mints a JWT from client_id/client_secret
and sends it as a Bearer token. Digital Link tests pre-seed the token cache so they
exercise the API surface directly; a separate group covers minting/refresh. All HTTP is
mocked with ``pytest-httpx``; retry backoff is made instant via an injected ``sleep``.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from pytest_httpx import HTTPXMock

from lib.errors import ConfigError, GS1APIError, MissingCredentialError, OverwriteError
from lib.gs1_dl_client import (
    BulkEntry,
    GS1Config,
    GS1DigitalLinkClient,
    LinkInput,
    ResolverSettings,
)

CLIENT_ID_ENV = "GS1_CLIENT_ID"
CLIENT_SECRET_ENV = "GS1_CLIENT_SECRET"
CLIENT_ID_VALUE = "client-id-abc"
CLIENT_SECRET_VALUE = "SECRET-CLIENT-XYZ"
TOKEN_VALUE = "minted.jwt.token"

HOST = "https://gs1nl-api-acc.gs1.nl"
TOKEN_URL = f"{HOST}/authorization/token"
UPSERT_URL = f"{HOST}/digitallinkv2/v2/digitallink"

SAMPLE_LINK: LinkInput = {
    "link_type": "pip",
    "language": "nl",
    "link_title": "Product page",
    "target_url": "https://example.com/p/123",
    "default_link_type": True,
    "public": True,
    "media_type": "text/html",
}


def make_config(**overrides: object) -> GS1Config:
    params: dict[str, object] = {
        "account_number": "8712345000010",
        "client_id_env": CLIENT_ID_ENV,
        "client_secret_env": CLIENT_SECRET_ENV,
    }
    params.update(overrides)
    return GS1Config(**params)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _credentials_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLIENT_ID_ENV, CLIENT_ID_VALUE)
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET_VALUE)


def _unseeded(config: GS1Config | None = None) -> tuple[GS1DigitalLinkClient, list[float]]:
    sleeps: list[float] = []
    client = GS1DigitalLinkClient(config or make_config(), sleep=sleeps.append)
    return client, sleeps


def make_client(config: GS1Config | None = None) -> tuple[GS1DigitalLinkClient, list[float]]:
    """A client with the token cache pre-seeded (no mint round-trip)."""
    client, sleeps = _unseeded(config)
    client._token = TOKEN_VALUE
    client._token_expiry = time.monotonic() + 3600
    return client, sleeps


# --- Happy paths & path anomalies (token pre-seeded) -------------------------


def test_upsert_posts_lowercase_path_and_camelcase_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", status_code=200)
    client, _ = make_client()

    client.upsert("8712345678905", "Test product", [SAMPLE_LINK])

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/digitallinkv2/v2/digitallink"
    assert request.headers["Authorization"] == f"Bearer {TOKEN_VALUE}"
    body = json.loads(request.content)
    assert body["accountNumber"] == "8712345000010"
    assert body["identificationKeyType"] == "Gtin"
    assert body["identificationKey"] == "08712345678905"  # zero-padded to 14
    assert body["resolverSettings"] == {"useGS1Resolver": True, "resolverDomainName": None}
    assert body["links"][0] == {
        "linkType": "pip",
        "language": "nl",
        "linkTitle": "Product page",
        "targetUrl": "https://example.com/p/123",
        "defaultLinkType": True,
        "public": True,
        "mediaType": "text/html",
    }
    assert body["applicationIdentifiers"] == []


def test_upsert_idempotent_body_then_single_get(httpx_mock: HTTPXMock) -> None:
    # §6.3: same input twice -> identical request bodies; GET returns one record.
    httpx_mock.add_response(method="POST", status_code=200)
    httpx_mock.add_response(method="POST", status_code=200)
    httpx_mock.add_response(
        method="GET",
        status_code=200,
        json={"identificationKey": "08712345678905", "isEnabled": True},
    )
    client, _ = make_client()

    client.upsert("8712345678905", "Test product", [SAMPLE_LINK])
    client.upsert("8712345678905", "Test product", [SAMPLE_LINK])
    record = client.get("8712345678905")

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert posts[0].content == posts[1].content
    assert record == {"identificationKey": "08712345678905", "isEnabled": True}


def test_upsert_bulk_batches_into_groups(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_response(method="POST", status_code=200, json={"ok": True})
    client, _ = make_client(make_config(batch_size=2))
    entries: list[BulkEntry] = [
        {"gtin": f"000000000000{i}"[-13:], "item_description": f"p{i}", "links": [SAMPLE_LINK]}
        for i in range(5)
    ]

    result = client.upsert_bulk(entries)

    requests = httpx_mock.get_requests()
    assert all(r.url.path == "/digitallinkv2/v2/digitallinks" for r in requests)
    batch_sizes = [len(json.loads(r.content)) for r in requests]
    assert batch_sizes == [2, 2, 1]
    assert result["total"] == 5
    assert result["batches"] == 3
    assert result["status_codes"] == [200, 200, 200]


def test_get_uses_ai_01_path_and_zfill(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=200, json={"identificationKey": "x"})
    client, _ = make_client()

    client.get("8712345678905")

    request = httpx_mock.get_requests()[0]
    # Path keys on the GTIN application identifier "01", not the string "Gtin".
    assert request.url.path == "/digitallinkv2/v2/digitalLink/01/08712345678905"


def test_get_missing_returns_none(httpx_mock: HTTPXMock) -> None:
    # A missing GTIN is a 400 with a "No valid contract found" body (not 404).
    httpx_mock.add_response(
        method="GET",
        status_code=400,
        text='"No valid contract found for Gtin with id: 00000000000000."',
    )
    client, _ = make_client()

    assert client.get("00000000000000") is None


def test_get_other_400_still_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=400, text="Some other validation error")
    client, _ = make_client()

    with pytest.raises(GS1APIError):
        client.get("8712345678905")


def test_set_enabled_patches_activation_status(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="PATCH", status_code=204)
    client, _ = make_client()

    client.set_enabled("8712345678905", is_enabled=False)

    request = httpx_mock.get_requests()[0]
    assert request.method == "PATCH"
    assert request.url.path == "/digitallinkv2/v2/digitalLink/01/08712345678905/activationStatus"
    assert json.loads(request.content) == {"isEnabled": False}


def test_validate_draft_path_has_no_v2_segment(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", status_code=200, json={"validationResult": {"isValid": True}}
    )
    client, _ = make_client()

    result = client.validate_draft("8712345678905")

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/digitallinkv2/digitalLink/validateDraft"
    assert result["validationResult"] == {"isValid": True}


# --- Retry policy (§4.3 / §5.1) — token pre-seeded ---------------------------


def test_retry_on_429_honours_retry_after(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", status_code=429, headers={"Retry-After": "2"})
    httpx_mock.add_response(method="POST", status_code=200)
    client, sleeps = make_client()

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert len(httpx_mock.get_requests()) == 2
    assert sleeps == [2.0]


def test_retry_on_5xx_then_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", status_code=500)
    httpx_mock.add_response(method="POST", status_code=503)
    httpx_mock.add_response(method="POST", status_code=200)
    client, sleeps = make_client()

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert len(httpx_mock.get_requests()) == 3
    assert sleeps == [0.5, 1.0]  # exponential base 0.5s


def test_5xx_exhaustion_raises(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_response(method="POST", status_code=500, text="boom")
    client, _ = make_client()

    with pytest.raises(GS1APIError) as exc:
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.status_code == 500
    assert len(httpx_mock.get_requests()) == 3


def test_429_exhaustion_raises(httpx_mock: HTTPXMock) -> None:
    for _ in range(5):
        httpx_mock.add_response(method="POST", status_code=429)
    client, _ = make_client()

    with pytest.raises(GS1APIError) as exc:
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.status_code == 429
    assert len(httpx_mock.get_requests()) == 5


def test_network_error_retried_as_5xx(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("no route"))
    httpx_mock.add_exception(httpx.ConnectError("no route"))
    httpx_mock.add_response(method="POST", status_code=200)
    client, sleeps = make_client()

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert len(httpx_mock.get_requests()) == 3
    assert sleeps == [0.5, 1.0]


def test_network_error_exhaustion_raises(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    client, _ = make_client()

    with pytest.raises(GS1APIError) as exc:
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.status_code == 0  # network-error sentinel


# --- Error parsing (§5.1) — token pre-seeded ---------------------------------


def test_400_populates_error_results(httpx_mock: HTTPXMock) -> None:
    error_body = [
        {
            "identifier": "08712345678905",
            "errors": [{"code": "GS1_INVALID_GTIN", "message": "GTIN checksum invalid"}],
        }
    ]
    httpx_mock.add_response(method="POST", status_code=400, json=error_body)
    client, _ = make_client()

    with pytest.raises(GS1APIError) as exc:
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.status_code == 400
    assert exc.value.error_results == error_body
    assert len(httpx_mock.get_requests()) == 1  # 4xx not retried


def test_400_malformed_body_leaves_error_results_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", status_code=400, text="not json at all")
    client, _ = make_client()

    with pytest.raises(GS1APIError) as exc:
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.error_results is None
    assert exc.value.response_body == "not json at all"


# --- OAuth2 token minting / refresh (§4.2) -----------------------------------


def test_mints_token_then_calls_api(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=TOKEN_URL, json={"access_token": TOKEN_VALUE, "expires_in": 3600}
    )
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=200)
    client, _ = _unseeded()

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    token_req, api_req = httpx_mock.get_requests()
    assert token_req.url == httpx.URL(TOKEN_URL)
    assert token_req.headers["client_id"] == CLIENT_ID_VALUE
    assert token_req.headers["client_secret"] == CLIENT_SECRET_VALUE
    assert api_req.headers["Authorization"] == f"Bearer {TOKEN_VALUE}"


def test_token_is_cached_across_calls(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=TOKEN_URL, json={"access_token": TOKEN_VALUE, "expires_in": 3600}
    )
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=200)
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=200)
    client, _ = _unseeded()

    client.upsert("8712345678905", "p", [SAMPLE_LINK])
    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    token_calls = [r for r in httpx_mock.get_requests() if str(r.url) == TOKEN_URL]
    assert len(token_calls) == 1  # minted once, reused


def test_expired_token_is_refreshed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=TOKEN_URL, json={"access_token": TOKEN_VALUE, "expires_in": 3600}
    )
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=200)
    client, _ = _unseeded()
    client._token = "stale"
    client._token_expiry = time.monotonic() - 1  # already expired

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    api_req = next(r for r in httpx_mock.get_requests() if str(r.url) == UPSERT_URL)
    assert api_req.headers["Authorization"] == f"Bearer {TOKEN_VALUE}"


def test_401_refreshes_token_and_retries(httpx_mock: HTTPXMock) -> None:
    client, _ = make_client()  # seeded with a (now-rejected) token
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=401)
    httpx_mock.add_response(
        method="POST", url=TOKEN_URL, json={"access_token": "fresh.jwt", "expires_in": 3600}
    )
    httpx_mock.add_response(method="POST", url=UPSERT_URL, status_code=200)

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    api_calls = [r for r in httpx_mock.get_requests() if str(r.url) == UPSERT_URL]
    assert len(api_calls) == 2
    assert api_calls[1].headers["Authorization"] == "Bearer fresh.jwt"


def test_mint_rejected_raises_config_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=TOKEN_URL,
        status_code=400,
        text="Your ClientId or ClientSecret might be incorrect.",
    )
    client, _ = _unseeded()

    with pytest.raises(ConfigError):
        client.upsert("8712345678905", "p", [SAMPLE_LINK])


def test_missing_client_credential_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    client, _ = _unseeded()

    with pytest.raises(MissingCredentialError):
        client.upsert("8712345678905", "p", [SAMPLE_LINK])


# --- PII scrubbing DoD (§5.2) ------------------------------------------------


def test_secrets_never_appear_in_logs(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    # The 400 body carries a sensitive-looking field; the ERROR log must scrub it,
    # and neither the token nor the client secret may surface in any log record.
    httpx_mock.add_response(
        method="POST",
        status_code=400,
        json=[
            {
                "identifier": "08712345678905",
                "errors": [{"code": "X", "message": "Y"}],
                "token": "leaked-in-body",
            }
        ],
    )
    client, _ = make_client()

    with (
        caplog.at_level(logging.DEBUG, logger="lib.gs1_dl_client"),
        pytest.raises(GS1APIError),
    ):
        client.upsert("8712345678905", "p", [SAMPLE_LINK])

    log_text = caplog.text
    assert TOKEN_VALUE not in log_text
    assert CLIENT_SECRET_VALUE not in log_text
    assert "leaked-in-body" not in log_text
    assert "[REDACTED]" in log_text


_NOT_FOUND_BODY = '"No valid contract found for Gtin with id: 08712345678905."'


def test_safe_upsert_writes_when_absent(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=400, text=_NOT_FOUND_BODY)
    httpx_mock.add_response(method="POST", status_code=200)
    client, _ = make_client()

    prior = client.safe_upsert("8712345678905", "p", [SAMPLE_LINK])

    assert prior is None
    assert any(r.method == "POST" for r in httpx_mock.get_requests())


def test_safe_upsert_refuses_to_overwrite(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=200, json={"identificationKey": "x"})
    client, _ = make_client()

    with pytest.raises(OverwriteError) as exc:
        client.safe_upsert("8712345678905", "p", [SAMPLE_LINK])

    assert exc.value.gtin == "8712345678905"
    assert all(r.method != "POST" for r in httpx_mock.get_requests())  # no write happened


def test_safe_upsert_overwrites_when_allowed_and_returns_prior(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=200, json={"identificationKey": "x"})
    httpx_mock.add_response(method="POST", status_code=200)
    client, _ = make_client()

    prior = client.safe_upsert("8712345678905", "p", [SAMPLE_LINK], overwrite=True)

    assert prior == {"identificationKey": "x"}
    assert any(r.method == "POST" for r in httpx_mock.get_requests())


# --- retract: the closest thing to a delete the v2 API has -------------------


def test_retract_deactivates_and_leaves_links_intact(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        status_code=200,
        json={"itemDescription": "Widget", "links": [{"linkType": "pip"}], "isEnabled": True},
    )
    httpx_mock.add_response(method="PATCH", status_code=204)
    client, _ = make_client()

    assert client.retract("8712345678905") is True

    requests = httpx_mock.get_requests()
    # Read, then flip the switch — and nothing else. No POST: rewriting the record would
    # destroy the configured link set, which deactivating already makes unreachable.
    assert [r.method for r in requests] == ["GET", "PATCH"]
    assert json.loads(requests[1].content) == {"isEnabled": False}
    assert requests[1].url.path.endswith("/digitalLink/01/08712345678905/activationStatus")


def test_retract_absent_gtin_writes_nothing(httpx_mock: HTTPXMock) -> None:
    # Only the GET is registered: pytest-httpx errors on any request beyond it, so this
    # asserts the no-write rather than merely observing it afterwards.
    httpx_mock.add_response(method="GET", status_code=400, text=_NOT_FOUND_BODY)
    client, _ = make_client()

    assert client.retract("8712345678905") is False

    assert all(r.method == "GET" for r in httpx_mock.get_requests())


def test_retract_is_idempotent(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        status_code=200,
        json={"itemDescription": "Widget", "links": [{"linkType": "pip"}], "isEnabled": False},
    )
    httpx_mock.add_response(method="PATCH", status_code=204)
    client, _ = make_client()

    assert client.retract("8712345678905") is True

    assert json.loads(httpx_mock.get_requests()[1].content) == {"isEnabled": False}


def test_resolver_settings_override_flows_into_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", status_code=200)
    config = make_config(
        resolver_settings=ResolverSettings(
            use_gs1_resolver=False, resolver_domain_name="id.example.com"
        )
    )
    client, _ = make_client(config)

    client.upsert("8712345678905", "p", [SAMPLE_LINK])

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["resolverSettings"] == {
        "useGS1Resolver": False,
        "resolverDomainName": "id.example.com",
    }
