"""Tests for scripts/report_live_copy.py — which products the Content screen offers to write for.

The site read itself is not exercised (it needs credentials); ``_fetch`` is replaced with an empty
site, which is the state of every product that has never published. What is under test is the one
decision this script makes before classifying: which in-scope products the plan will hold.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lib.holds import ProductHold
from lib.records import LocalisedText, ProductRecord, SkipReason
from scripts import report_live_copy

if TYPE_CHECKING:
    import pytest

TICKED = "02987653004488"
HELD = "02987653007922"


def _product(gtin: str) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Acme",
        product_name=LocalisedText(values={"nl": f"product {gtin[-4:]}"}),
        description_short=LocalisedText(values={"nl": "Handig"}),
    )


class _Cfg:
    client_id = "acme"

    class wordpress:  # noqa: N801 — stands in for a config attribute
        languages = ("nl", "fr")
        site_url = "https://example.test"


def _run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    products = [_product(TICKED), _product(HELD)]
    monkeypatch.setattr(report_live_copy, "resolve_client_id", lambda cid: "acme")
    monkeypatch.setattr(report_live_copy, "get_client", lambda cid: _Cfg)
    monkeypatch.setattr(report_live_copy, "_load_products", lambda cfg: products)
    monkeypatch.setattr(report_live_copy, "in_scope", lambda cfg, ps: ps)
    monkeypatch.setattr(
        report_live_copy,
        "held_products",
        lambda cfg, ps: {HELD: ProductHold(SkipReason.MISSING_MANDATORY_FIELD)},
    )
    monkeypatch.setattr(report_live_copy, "_fetch", lambda cfg: {})

    assert report_live_copy.main(["--json"]) == 0
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    return payload


def test_a_held_row_on_the_saved_list_is_not_offered_for_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pilot's 3 ticked products came back as "6 to process" — the other 3 were holds.

    The saved selection keeps every not-eligible row so a run can name it; this is what stops those
    rows reading as chosen on the Content screen.
    """
    payload = _run(monkeypatch, capsys)

    buckets = {product["gtin"]: product["bucket"] for product in payload["products"]}
    assert buckets == {TICKED: "needs_text", HELD: "held"}
    assert payload["counts"]["needs_text"] == 1
    assert payload["counts"]["held"] == 1
