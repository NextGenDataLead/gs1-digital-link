"""Tests for lib/live_copy.py — bucketing products against what the site actually carries.

The module exists because ``state.json`` cannot answer this question on a machine that did not do
the publishing, and the whole point is to be right about a page nobody here wrote. So the cases
worth covering are the ones where the site disagrees with what a ledger would have said: a page
that is live in one language and absent in the other, a page that exists with its generated fields
blank (the silent ACF failure this pipeline has already produced), and a product nothing on this
machine can ever write copy for.
"""

from __future__ import annotations

from lib.live_copy import Bucket, LiveText, classify, has_copy_inputs
from lib.records import LocalisedText, ProductRecord

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"
_FIELDS = ("product_title", "product_description")


def _product(
    gtin: str = GTIN_A, *, short: str | None = "Handig", long: str | None = None
) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Acme",
        product_name=LocalisedText(values={"nl": "Rugsteun", "fr": "Support"}),
        description_short=LocalisedText(values={"nl": short}) if short else None,
        description_long=LocalisedText(values={"nl": long}) if long else None,
    )


def _live(gtin: str, language: str, *, filled: tuple[str, ...] = _FIELDS) -> LiveText:
    return LiveText(
        gtin=gtin,
        language=language,
        page_id=1,
        filled=frozenset(filled),
        empty=tuple(field for field in _FIELDS if field not in filled),
    )


def test_a_product_live_in_every_language_has_text() -> None:
    product = _product()
    live = {(GTIN_A, "nl"): _live(GTIN_A, "nl"), (GTIN_A, "fr"): _live(GTIN_A, "fr")}

    report = classify([product], live, ["nl", "fr"])

    assert report.counts == {
        "in_scope": 1,
        "has_text": 1,
        "needs_text": 0,
        "no_inputs": 0,
        "held": 0,
    }
    assert report.products[0].missing_languages == ()


def test_live_in_one_language_and_absent_in_the_other_still_needs_text() -> None:
    """The case a per-product check misses, and the one this client is in.

    Publishing is per ``(GTIN, language)``, so "the product is live" is not a state the site has.
    A product with a Dutch page and no French one is half-done, and calling it finished leaves the
    French page permanently unwritten with nothing reporting it.
    """
    report = classify([_product()], {(GTIN_A, "nl"): _live(GTIN_A, "nl")}, ["nl", "fr"])

    assert report.products[0].bucket is Bucket.NEEDS_TEXT
    assert report.products[0].missing_languages == ("fr",)
    assert report.products[0].absent_languages == ("fr",)


def test_a_live_page_with_blank_fields_needs_text_and_is_not_reported_as_absent() -> None:
    """The silent failure: the page exists, the ACF write did not land.

    ``run_execute`` gets a ``200`` for the post and never learns the fields were dropped — which is
    why this project's own rule is to verify against the rendered page. Here it has to come back
    as *needs text*, and as a different shape of problem from a page that was never created:
    absent means the pipeline has not run, blank means it ran and lost the result.
    """
    live = {
        (GTIN_A, "nl"): _live(GTIN_A, "nl", filled=()),
        (GTIN_A, "fr"): _live(GTIN_A, "fr"),
    }

    report = classify([_product()], live, ["nl", "fr"])

    assert report.products[0].bucket is Bucket.NEEDS_TEXT
    assert report.products[0].missing_languages == ("nl",)
    assert report.products[0].absent_languages == ()


def test_a_product_with_no_1083_and_no_1067_cannot_be_written_at_all() -> None:
    """Two buckets, because "needs copy" and "can be given copy" have different owners.

    One is a button on this machine; the other is a trip to MyGS1 and a fresh export. Collapsed
    into one figure, the Generate button names products it cannot help, and an operator who
    presses it learns that pressing it does nothing.
    """
    report = classify([_product(short=None, long=None)], {}, ["nl"])

    assert report.products[0].bucket is Bucket.NO_INPUTS
    assert report.counts["no_inputs"] == 1
    assert report.counts["needs_text"] == 0


def test_either_source_is_enough_to_write_from() -> None:
    """1083 *or* 1067 — the rule the screen states, asserted rather than described."""
    assert has_copy_inputs(_product(short="Handig", long=None))
    assert has_copy_inputs(_product(short=None, long="Lang verhaal"))
    assert not has_copy_inputs(_product(short=None, long=None))
    assert not has_copy_inputs(_product(short="   ", long=None)), "whitespace is not an input"


def test_the_other_language_counts_as_an_input() -> None:
    """A value the feed carries in Dutch and not French is a gap the producer fills.

    That is what ``translate: true`` is for, so asking per language would report a French unit as
    unwritable while the run was about to write it.
    """
    product = ProductRecord(
        gtin=GTIN_A,
        brand="Acme",
        product_name=LocalisedText(values={"nl": "Rugsteun"}),
        description_short=LocalisedText(values={"nl": "Handig"}),
    )

    report = classify([product], {}, ["nl", "fr"])

    assert report.products[0].bucket is Bucket.NEEDS_TEXT


def test_a_held_product_is_counted_apart_and_never_offered_for_text() -> None:
    """Data shows a held product without a tick box; Content must not then offer to write for it.

    The saved selection keeps every not-eligible row so a run can name it afterwards, which made
    those rows look chosen here: on the pilot, 3 ticked products came back as "6 to process / 11
    skipped" — the other 14 were holds. Counted apart rather than dropped, so a hold still reads as
    work outstanding and not as nothing.
    """
    report = classify([_product(GTIN_A), _product(GTIN_B)], {}, ["nl"], held=[GTIN_B])

    assert [p.gtin for p in report.needs_text] == [GTIN_A]
    assert [p.gtin for p in report.held] == [GTIN_B]
    assert report.counts == {
        "in_scope": 2,
        "has_text": 0,
        "needs_text": 1,
        "no_inputs": 0,
        "held": 1,
    }


def test_held_wins_over_live_text_so_it_is_not_offered_as_an_override() -> None:
    """The override list rewrites live text — it must not reach a product the plan will drop."""
    live = {(GTIN_A, "nl"): _live(GTIN_A, "nl")}

    report = classify([_product()], live, ["nl"], held=[GTIN_A])

    assert report.products[0].bucket is Bucket.HELD
    assert report.has_text == ()


def test_held_matches_whatever_width_the_gtin_arrives_in() -> None:
    report = classify([_product()], {}, ["nl"], held=[GTIN_A.lstrip("0")])

    assert report.products[0].bucket is Bucket.HELD


def test_products_keep_the_order_they_arrived_in() -> None:
    """The screen renders them in the order the operator's own list did."""
    report = classify([_product(GTIN_B), _product(GTIN_A)], {}, ["nl"])

    assert [product.gtin for product in report.products] == [GTIN_B, GTIN_A]
