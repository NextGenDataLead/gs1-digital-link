"""Tests for ``generator.translate_copy`` (``lib.copy_translation`` and its generator half).

What is pinned is the operator's rule (2026-11-10): the default language is written, every other
language is its translation; a change to the default language's GS1 text rewrites every language,
a change to another language's rewrites nothing; and the switch itself replaces only the
translated pages, translating the default-language page as it is live.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lib.copy_translation import (
    copy_basis,
    default_copy,
    due_languages,
    due_units,
    translation_requests,
)
from lib.generator import (
    MODE_TRANSLATE,
    GenerationContext,
    ResultItem,
    ResultsFile,
    merge_generated,
    missing_copy,
    pending_requests,
)
from lib.records import LocalisedText, ProductRecord, State, StateEntry

GTIN = "08713195006109"
NOW = datetime(2026, 11, 10, tzinfo=UTC)


def _context(*, translate: bool = True) -> GenerationContext:
    return GenerationContext(
        languages=["nl", "fr"], default_language="nl", prompt_version="v1", translate_copy=translate
    )


def _product(
    *, nl: str = "Borstel voor je huisdier", fr: str = "Brosse pour vos cheveux", fr_1067: str = ""
) -> ProductRecord:
    long = {"fr": fr_1067} if fr_1067 else {}
    return ProductRecord(
        gtin=GTIN,
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Pet spa", "fr": "Pet spa"}),
        description_short=LocalisedText(values={"nl": nl, "fr": fr}),
        description_long=LocalisedText(values=long) if long else None,
        net_content="1 H87",
    )


def _entry(basis: str | None, *, status: str = "publish") -> StateEntry:
    return StateEntry(
        wp_page_id=1,
        wp_url="https://x.test/p/",
        wp_featured_media_id=None,
        content_hash="h",
        gs1_link_set_hash="g",
        last_run=NOW,
        wp_status=status,
        copy_basis=basis,
    )


# --- what the copy is written from -----------------------------------------------------


def test_the_basis_moves_with_the_default_language_text_only() -> None:
    base = copy_basis(_product(), _context())

    assert copy_basis(_product(fr="Une autre phrase"), _context()) == base
    assert copy_basis(_product(nl="Een andere zin"), _context()) != base


def test_a_translated_unit_is_written_from_the_default_language_text() -> None:
    requests = pending_requests([_product()], _context())

    assert [(r.language, r.inputs.marketing_message) for r in requests] == [
        ("nl", "Borstel voor je huisdier")
    ]


def test_editing_the_translated_language_does_not_make_its_copy_stale() -> None:
    """The French copy's fingerprint is over Dutch inputs: a French GS1 edit leaves it valid."""
    sources = {GTIN: ["Borstel"]}
    before, _ = translation_requests([_product()], _context(), sources)
    after, _ = translation_requests([_product(fr="Edited in MyGS1")], _context(), sources)

    assert before[0].input_fingerprint == after[0].input_fingerprint


def test_a_short_french_1067_is_translated_too_not_published_as_it_stands() -> None:
    """Without the setting a short 1067 is the copy; translated, French must follow Dutch."""
    product = _product(fr_1067="Pour les animaux")
    empty = ResultsFile(client_id="democlient")

    assert missing_copy([product], empty, _context(translate=False)) == [(GTIN, "nl")]
    assert missing_copy([product], empty, _context()) == [(GTIN, "nl"), (GTIN, "fr")]


# --- what a translation translates -----------------------------------------------------


def test_a_translation_request_carries_its_source_line_for_line() -> None:
    requests, unsourced = translation_requests(
        [_product()], _context(), {GTIN: ["Tagline", "Bullet"]}
    )

    assert unsourced == []
    assert [(r.language, r.mode, r.source_usps, r.source_language) for r in requests] == [
        ("fr", MODE_TRANSLATE, ["Tagline", "Bullet"], "nl")
    ]


def test_no_source_means_no_request_never_one_from_the_own_language() -> None:
    requests, unsourced = translation_requests([_product()], _context(), {})

    assert (requests, unsourced) == ([], [(GTIN, "fr")])


def test_the_default_copy_is_this_runs_result_or_the_feeds_short_1067() -> None:
    context = _context()
    product = _product()
    nl_request = pending_requests([product], context)[0]
    results = ResultsFile(
        client_id="democlient",
        results=[
            ResultItem(
                gtin=GTIN,
                language="nl",
                usps=["Tagline"],
                input_fingerprint=nl_request.input_fingerprint,
            )
        ],
    )
    verbatim = product.model_copy(
        update={"description_long": LocalisedText(values={"nl": "Kort\nKrachtig"})}
    )

    assert default_copy(product, context, results) == ["Tagline"]
    assert default_copy(product, context, ResultsFile(client_id="democlient")) is None
    assert default_copy(verbatim, context, ResultsFile(client_id="democlient")) == [
        "Kort",
        "Krachtig",
    ]


def test_a_translation_is_merged_and_reported_as_one() -> None:
    context = _context()
    product = _product()
    request = translation_requests([product], context, {GTIN: ["Tagline"]})[0][0]
    results = ResultsFile(
        client_id="democlient",
        results=[
            ResultItem(
                gtin=GTIN,
                language="fr",
                usps=["Slogan"],
                input_fingerprint=request.input_fingerprint,
            )
        ],
    )

    merged, issues = merge_generated([product], results, context)

    assert merged[0].generated_tagline is not None
    assert merged[0].generated_tagline.values == {"fr": "Slogan"}
    assert [i.source for i in issues if i.issue == "content_generated"] == [
        "translated from the default language's copy"
    ]


# --- when a live page's copy is out of date --------------------------------------------


def test_the_switch_replaces_the_translated_pages_and_keeps_the_default_one() -> None:
    entries = {"nl": _entry(None), "fr": _entry(None)}

    assert due_languages(entries, "b", "nl") == {"fr"}


def test_nothing_is_due_while_the_default_text_is_unchanged() -> None:
    entries = {"nl": _entry(None), "fr": _entry("b")}

    assert due_languages(entries, "b", "nl") == frozenset()


def test_a_changed_default_text_rewrites_every_language() -> None:
    entries = {"nl": _entry("before"), "fr": _entry("before")}

    assert due_languages(entries, "after", "nl") == {"nl", "fr"}


def test_a_translation_that_lags_does_not_send_the_default_page_round_again() -> None:
    """Review finding: Dutch rewritten, French failed — only French is asked for again."""
    entries = {"nl": _entry("after"), "fr": _entry("before")}

    assert due_languages(entries, "after", "nl") == {"fr"}


def test_a_default_page_left_behind_rewrites_its_translations_with_it() -> None:
    """French applied, Dutch skipped at the gate: French translated copy no page carries."""
    entries = {"nl": _entry("before"), "fr": _entry("after")}

    assert due_languages(entries, "after", "nl") == {"nl", "fr"}


def test_a_page_taken_down_is_never_due() -> None:
    entries = {"nl": _entry(None), "fr": _entry(None, status="draft")}

    assert due_languages(entries, "b", "nl") == frozenset()


def test_without_the_setting_nothing_is_ever_due() -> None:
    state = State(client_id="democlient", entries={GTIN: {"nl": _entry(None), "fr": _entry(None)}})

    assert due_units([_product()], state, _context(translate=False)) == set()
    assert due_units([_product()], state, _context()) == {(GTIN, "fr")}
