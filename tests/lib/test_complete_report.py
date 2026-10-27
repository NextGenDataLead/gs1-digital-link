"""Tests for lib/complete_report.py — the sections only the complete data-quality report has.

Operator, 2026-10-08: the report "needs to give a complete insight into all missing data/video's,
data to be adjusted, etc. So an actionable report." Every product the client sent and everything
live, each with where it stands; what is wrong with the product list itself; who does what.
"""

from __future__ import annotations

from lib.complete_report import (
    LIVE,
    NOT_IN_EXPORT,
    READY,
    ListedRow,
    PageState,
    list_lines,
    published_lines,
    status_rows,
)
from lib.live_inventory import PublishedTotals

A, B, C, D = "02987653004488", "02987653005829", "02987653008486", "02987653009999"
LANGS = ["nl", "fr"]


def _rows() -> list:
    return status_rows(
        listed=[
            ListedRow(A, "4156 Desk lamp"),
            ListedRow(B, "4194 Coil Light"),
            ListedRow(C, "5003 Joy Grill"),
            ListedRow(D, "9999 Unknown"),
        ],
        exported={A: "bureaulamp", B: "ledstrip", C: "Grillen"},
        held={B: "two videos in nl — the client must keep one"},
        missing_video={C: "no confirmed video in fr"},
        live={
            A: {
                "nl": PageState(on_site=True, video=True, text=True),
                "fr": PageState(on_site=True, video=False, text=True),
            }
        },
        languages=LANGS,
    )


def test_every_product_gets_one_status() -> None:
    by_gtin = {row.gtin: row for row in _rows()}

    assert by_gtin[A].status == LIVE
    assert by_gtin[B].status.startswith("held — two videos")
    assert by_gtin[C].status == READY
    assert by_gtin[D].status == NOT_IN_EXPORT


def test_a_live_page_says_what_the_site_has() -> None:
    row = next(r for r in _rows() if r.gtin == A)

    assert row.cells == ["video · text", "no video"]


def test_a_product_not_yet_live_names_what_it_will_lack() -> None:
    row = next(r for r in _rows() if r.gtin == C)

    assert row.note == "no confirmed video in fr"
    assert row.cells == ["—", "—"]


def test_an_unchecked_site_is_never_called_live_with_video() -> None:
    rows = status_rows(
        listed=[ListedRow(A, "4156 Desk lamp")],
        exported={A: "bureaulamp"},
        held={},
        missing_video={},
        live={A: {"nl": PageState(on_site=None, video=None, text=None)}},
        languages=["nl"],
    )

    assert rows[0].cells == ["not checked"]


def test_the_product_list_names_barcodes_the_export_lacks_and_shared_barcodes() -> None:
    """7 Days and Joy Grill on one barcode: the export says it is the grill."""
    lines = "\n".join(
        list_lines(
            [
                ListedRow(A, "4156 Desk lamp"),
                ListedRow(C, "4214 7 Days"),
                ListedRow(C, "5003 Joy Grill"),
                ListedRow(D, "9999 Unknown"),
            ],
            exported={A: "bureaulamp", C: "Grillen"},
        )
    )

    assert "`02987653009999`" in lines and "9999 Unknown" in lines
    assert "4214 7 Days" in lines and "5003 Joy Grill" in lines
    assert "| Grillen |" in lines


def test_the_published_section_gives_the_numbers_per_language() -> None:
    totals = PublishedTotals(
        products=38,
        pages={"nl": 38, "fr": 38},
        records=38,
        links={"nl": 38, "fr": 38},
        qr_codes=38,
    )

    text = "\n".join(published_lines(totals, LANGS, "nl"))

    assert "## What this tool has published" in text
    assert "**38** products live, **38** GS1 records, **38** QR codes" in text
    assert "a scan of the QR code opens the nl page" in text
    assert "| Pages live | 38 | 38 |" in text
    assert "| Links in GS1 records | 38 | 38 |" in text
    assert "no GS1 record yet" not in text  # nothing to say, so nothing said


def test_the_published_section_names_what_is_unfinished() -> None:
    totals = PublishedTotals(
        products=3,
        pages={"nl": 3, "fr": 3},
        records=2,
        links={"nl": 2, "fr": 2},
        without_record=1,
        qr_codes=1,
        records_without_qr=1,
        taken_down=1,
    )

    text = "\n".join(published_lines(totals, LANGS, "nl"))

    assert "**1** live product(s) have their pages but no GS1 record yet" in text
    assert "**1** product(s) have a GS1 record but no QR file" in text
    assert "**1** product(s) were published once and taken down since" in text
