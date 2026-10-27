"""Tests for lib/live_inventory.py — everything this tool has put live, from the ledger.

Operator, 2026-10-07: a data-quality report for every product that went live, to give it follow-up.
The ledger records each page this machine wrote; a retracted or unpublished page is not live. The
site is the authority on what is there now, so the inventory says per page whether the site was
checked and what it answered — and never claims "live" for a page the check could not see.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lib.live_inventory import live_products, published_totals
from lib.records import State, StateEntry

A, B, C = "02987653004488", "02987653007151", "02987653000527"


def _entry(
    gtin: str, lang: str, *, video: str | None = "v.mp4", gs1: str = "h", **over: object
) -> StateEntry:
    base: dict[str, object] = {
        "wp_page_id": 1,
        "wp_url": f"https://site.test/{lang}/p-{gtin}/",
        "wp_featured_media_id": None,
        "content_hash": "c",
        "gs1_link_set_hash": gs1,
        "last_run": datetime(2026, 10, 7, 2, 28, tzinfo=UTC),
        "title": f"title-{gtin[-4:]}",
        "video_file": video,
    }
    base.update(over)
    return StateEntry.model_validate(base)


def _state() -> State:
    return State(
        client_id="democlient",
        entries={
            A: {"nl": _entry(A, "nl"), "fr": _entry(A, "fr")},
            B: {"nl": _entry(B, "nl"), "fr": _entry(B, "fr", video="")},
            C: {"nl": _entry(C, "nl", retracted=True), "fr": _entry(C, "fr", wp_status="draft")},
        },
    )


def test_retracted_and_unpublished_pages_are_not_live() -> None:
    live = live_products(_state())

    assert [p.gtin for p in live] == [A, B]
    assert {page.language for page in live[0].pages} == {"nl", "fr"}


# --- What this tool has published: the counts the complete report opens its record with ----------
# Operator, 2026-10-08: the report "should also contain numbers on what is processed like pages and
# links".

D = "02987653003344"


def test_published_totals_count_pages_records_links_and_qr_codes() -> None:
    state = _state()
    # D went live with pages only: no GS1 record yet.
    state.entries[D] = {"nl": _entry(D, "nl", gs1=""), "fr": _entry(D, "fr", gs1="")}

    totals = published_totals(state, ["nl", "fr"], qr_gtins={A, C})

    assert totals.products == 3  # A, B, D — C was taken down
    assert totals.pages == {"nl": 3, "fr": 3}
    assert totals.records == 2  # A and B
    assert totals.links == {"nl": 2, "fr": 2}
    assert totals.without_record == 1  # D
    assert totals.qr_codes == 1  # A; C's file belongs to a product no longer live
    assert totals.records_without_qr == 1  # B
    assert totals.taken_down == 1  # C


def test_a_language_with_nothing_published_is_counted_as_zero() -> None:
    totals = published_totals(_state(), ["nl", "fr", "de"], qr_gtins=set())

    assert totals.pages["de"] == 0
    assert totals.links["de"] == 0
