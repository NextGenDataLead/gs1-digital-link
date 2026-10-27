"""Unit tests for the content-generator core (``lib.generator``).

Pure and network-free: exercises the per-run results seam, the request/result contract, and the
deterministic merge. No LLM is involved — a producer's output is simulated by constructing
:class:`~lib.generator.GenerationResult` directly.

There is no store. Copy arrives in one ``generation_results.json`` per run, is read once by
:func:`~lib.generator.merge_generated`, and is never reused to let a later run skip work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.errors import GeneratorError
from lib.gdsn import GdsnSource
from lib.generator import (
    MODE_GENERATE,
    MODE_TIGHTEN,
    GenerationContext,
    GenerationResult,
    ResultItem,
    ResultsFile,
    TranslatableField,
    _combine_title,
    generation_context,
    load_results,
    merge_generated,
    missing_copy,
    pending_requests,
    result_item,
    save_results,
    translation_gaps,
    withhold_published_copy,
)
from lib.records import LocalisedText, ProductRecord

_NOW = datetime(2026, 7, 18, tzinfo=UTC)

#: The pilot client's `translate: true` set, in config order — mapped fields then extras.
#: `material` is language-agnostic in GS1, so it has no slot a filled value could go back into.
_TRANSLATABLE = [
    TranslatableField(field="product_name", source_label="TradeItemDescription attr 3301"),
    TranslatableField(field="description_short", source_label="MarketingInformation attr 1083"),
    TranslatableField(field="product_variation", source_label="TradeItemDescription attr 3332"),
    TranslatableField(
        field="material",
        source_label="BrickGPCCommercialData attr Material",
        has_language_slot=False,
    ),
]


def _ctx(*languages: str, translatable: list[TranslatableField] | None = None) -> GenerationContext:
    """A client context for the tests: the given languages, nl default, prompt v1."""
    return GenerationContext(
        languages=list(languages),
        default_language="nl",
        prompt_version="v1",
        translatable=_TRANSLATABLE if translatable is None else translatable,
    )


def _product(**overrides: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": "02987653007359",
        "brand": "Democlient",
        "product_name": LocalisedText(values={"nl": "Bewateringpin"}),
        "net_content": "6 H87",
        "description_short": LocalisedText(values={"nl": "Water voor je planten"}),
        "extras": {
            "dim_height": "350 MMT",
            "dim_width": "250 MMT",
            "dim_depth": "80 MMT",
            "material": "kunststof",
        },
    }
    base.update(overrides)
    return ProductRecord(**base)


def _result(
    *usps: str, translations: dict[str, str] | None = None, inferences: list[str] | None = None
) -> GenerationResult:
    return GenerationResult(
        usps=list(usps), translations=translations or {}, inferences=inferences or []
    )


def _copy(*items: ResultItem, provenance: str = "in-session") -> ResultsFile:
    """One run's results file, holding exactly these items."""
    return ResultsFile(client_id="democlient", provenance=provenance, results=list(items))


def _copy_for(
    product: ProductRecord,
    context: GenerationContext,
    *usps: str,
    translations: dict[str, str] | None = None,
    inferences: list[str] | None = None,
) -> ResultsFile:
    """A results file answering every pending request for ``product`` with the same copy."""
    result = _result(*usps, translations=translations, inferences=inferences)
    requests = pending_requests([product], context)
    return _copy(*(result_item(request, result) for request in requests))


# --- title combiner ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "variation", "expected"),
    [
        ("Snoeischaar", "Snoeischaar", "Snoeischaar"),  # exact duplicate → dedup
        ("Snoeischaar", "snoeischaar", "Snoeischaar"),  # case-insensitive dedup
        ("Emmer", "Set", "Emmer Set"),  # genuine variation → appended
        ("Bewateringpin", None, "Bewateringpin"),  # no variation → unchanged
        ("Bewateringpin", "", "Bewateringpin"),  # empty variation → unchanged
    ],
)
def test_combine_title(name: str, variation: str | None, expected: str) -> None:
    assert _combine_title(name, variation) == expected


# --- pending_requests --------------------------------------------------------


def test_pending_requests_flags_every_language() -> None:
    requests = pending_requests([_product()], _ctx("nl", "fr"))

    assert {r.language for r in requests} == {"nl", "fr"}
    assert requests[0].inputs.marketing_message == "Water voor je planten"


def test_every_unit_is_pending_on_every_run() -> None:
    """No store, so nothing can make a unit look already done — the point of the whole change.

    The cache's freshness skip is what let a re-run reuse frozen copy. With it gone, the same call
    must keep naming a unit however much copy already exists for it. ``units`` is not a way back to
    that: it says which units this run *will publish*, and existing copy has no vote in it — see
    :func:`test_the_units_filter_is_scope_not_reuse`.
    """
    product = _product()
    context = _ctx("nl", "fr")

    first = pending_requests([product], context)
    written = _copy_for(product, context, "Tagline", "Bullet")

    assert len(written.results) == 2  # both units have copy now…
    assert pending_requests([product], context) == first  # …and both are still asked for
    assert {r.language for r in first} == {"nl", "fr"}


def test_pending_requests_narrows_to_the_units_the_caller_names() -> None:
    """Copy is written for the rows a run executes; an UNCHANGED row is not one of them."""
    product = _product()
    context = _ctx("nl", "fr")

    requests = pending_requests([product], context, units={(product.gtin, "fr")})

    assert [(r.gtin, r.language) for r in requests] == [(product.gtin, "fr")]


def test_the_units_filter_is_scope_not_reuse() -> None:
    """The distinction that keeps this from being the cache again, in one assertion.

    A unit is excluded because nothing will be published for it, never because copy for it exists.
    Write copy for every unit and the answer does not move — only the caller's scope moves it.
    """
    product = _product()
    context = _ctx("nl", "fr")
    _copy_for(product, context, "Tagline", "Bullet")  # every unit answered

    both = pending_requests([product], context, units={(product.gtin, "nl"), (product.gtin, "fr")})

    assert {r.language for r in both} == {"nl", "fr"}


def test_pending_requests_without_units_asks_for_everything() -> None:
    """Back-compat pin: the library stays ignorant of state unless a caller supplies scope."""
    product = _product()
    context = _ctx("nl", "fr")

    assert pending_requests([product], context, units=None) == pending_requests([product], context)


def test_an_empty_units_set_asks_for_nothing() -> None:
    """Distinct from ``None``. "Nothing to publish" is an answer; "I did not decide" is not."""
    assert pending_requests([_product()], _ctx("nl", "fr"), units=set()) == []


# --- missing_copy: the read-side twin, asked about the same units -------------


def test_missing_copy_names_every_unanswered_unit() -> None:
    product = _product()

    assert missing_copy([product], _copy(), _ctx("nl", "fr")) == [
        (product.gtin, "nl"),
        (product.gtin, "fr"),
    ]


def test_missing_copy_narrows_to_the_units_the_caller_names() -> None:
    """It must be asked about the units generation was asked for, or it fails every scoped run.

    ``pending_requests`` and this are the write and read halves of one question. Narrow the first
    and not the second and the doctor reports every already-live unit as uncovered — a FAIL on
    every run after the first, which is how a check stops being read.
    """
    product = _product()

    assert missing_copy([product], _copy(), _ctx("nl", "fr"), units={(product.gtin, "fr")}) == [
        (product.gtin, "fr")
    ]


def test_missing_copy_counts_an_answered_unit_as_covered() -> None:
    product = _product()
    context = _ctx("nl", "fr")

    written = _copy_for(product, context, "Tagline", "Bullet")

    assert missing_copy([product], written, context) == []


# --- the producer's `functional_name` input (attr 3301) ----------------------


def test_the_producer_is_seeded_with_this_languages_own_name_when_the_feed_carries_one() -> None:
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}))

    request = pending_requests([product], _ctx("fr"))[0]

    assert request.inputs.functional_name == "Pic"


def test_a_unit_with_no_name_in_its_own_language_is_seeded_with_the_default_one() -> None:
    """The producer has to know what it is describing, and the Dutch name still says so.

    This used to arrive by accident: attr 3301 was declared a second time as an
    `extras.functional_name`, extras collapsed to one language, and the Dutch value fell out of
    that. It is now the stated rule on the single source that remains — and it is what stops a
    French unit with no French 3301 being handed nothing at all.
    """
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin"}))  # no fr

    request = pending_requests([product], _ctx("fr"))[0]

    assert request.inputs.functional_name == "Bewateringpin"


def test_a_declared_functional_name_feeds_the_producer_instead_of_the_title() -> None:
    """The title and what the product *is* can be two attributes (operator, 2026-10-22).

    Democlient titles its pages with the short name (3297, "Dizzy Dog") and keeps the functional
    name (3301, "Hondenspeelgoed") as ``extras.functional_name``. A brand name gives the copy
    nothing to describe, so the producer is handed the functional name.
    """
    product = _product(
        product_name=LocalisedText(values={"nl": "Dizzy Dog", "fr": "Dizzy Dog"}),
        extras_localised={
            "functional_name": LocalisedText(
                values={"nl": "Hondenspeelgoed", "fr": "Jouets pour chiens"}
            )
        },
    )

    request = pending_requests([product], _ctx("fr"))[0]

    assert request.inputs.functional_name == "Jouets pour chiens"


def test_a_functional_name_missing_in_its_language_falls_back_to_the_default_one() -> None:
    product = _product(
        product_name=LocalisedText(values={"nl": "Dizzy Dog", "fr": "Dizzy Dog"}),
        extras_localised={"functional_name": LocalisedText(values={"nl": "Hondenspeelgoed"})},
    )

    request = pending_requests([product], _ctx("fr"))[0]

    assert request.inputs.functional_name == "Hondenspeelgoed"


def test_translation_sources_carry_only_the_fields_this_unit_is_actually_missing() -> None:
    """`translation_sources` holds one key per *gap*, not one per translatable field.

    It is part of the fingerprint, so the narrower reading is what stops a product the feed carries
    in every language re-fingerprinting whenever a translatable source is added or removed.
    """
    product = _product(
        product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}),
        description_short=LocalisedText(values={"nl": "Water", "fr": "Eau"}),
    )

    request = pending_requests([product], _ctx("fr"))[0]

    assert set(request.inputs.translation_sources) == {"material"}  # the nl-only flat extra


def test_pending_requests_asks_the_producer_to_fill_each_language_gap() -> None:
    product = _product()  # nl only, in product_name / description_short / material
    requests = pending_requests([product], _ctx("fr"))

    gaps = {gap.field: gap for gap in requests[0].translations}
    assert set(gaps) == {"product_name", "description_short", "material"}
    # Each gap carries the text to translate from, not just the field name: the producer works
    # from it in the same call it writes the copy.
    assert gaps["product_name"].source_language == "nl"
    assert gaps["product_name"].source_value == "Bewateringpin"
    assert gaps["description_short"].source_value == "Water voor je planten"


def test_a_value_present_in_this_language_is_not_a_gap() -> None:
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}))

    fields = {gap.field for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert "product_name" not in fields


def test_a_value_blank_in_every_language_is_never_filled() -> None:
    """The line between translating and inventing, and the reason this is defensible at all.

    Rendering a value the feed already holds into a second language is translation. Writing one
    that exists in no language would be invention — it stays a source finding for MyGS1 (E23).
    """
    product = _product(description_short=None, extras={})

    fields = {gap.field for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert fields == {"product_name"}  # the only value this product carries at all


def test_a_slot_holding_the_other_language_is_a_gap_not_a_value() -> None:
    """The real case: `product_name.fr` reads `Schoonmaakdoek`, which is Dutch.

    A full slot in the wrong language leaves the page permanently untranslated if it counts as
    carried — no gap, no request, nothing reported as filled, and the French page keeps showing a
    Dutch word for as long as the feed says so. Treating it as a language the feed does not carry
    turns it into an ordinary gap the authoritative language fills.
    """
    product = _product(
        product_name=LocalisedText(values={"nl": "microvezeldoek", "fr": "Schoonmaakdoek"})
    )

    gaps = {gap.field: gap for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert "product_name" in gaps
    # Filled from the authoritative language, not by translating the wrong-language text: nl and
    # fr then name the same product. Where the two slots disagreed about more than language, that
    # is a source defect and stays in the report rather than being laundered into French.
    assert gaps["product_name"].source_language == "nl"
    assert gaps["product_name"].source_value == "microvezeldoek"


def test_a_filled_wrong_language_value_is_not_overwritten_by_the_feed() -> None:
    """The half that makes the fix real, and the one that fails silently when it is missing.

    "The feed always wins" is enforced on read as well as write, and a wrong-language value is the
    single case where the feed is what we are correcting. Left in `_stored_values`, `Schoonmaakdoek`
    beats the translation written to replace it: the run reports the value as filled, the report
    tells the operator to paste it into MyGS1, and the page still says `Schoonmaakdoek`.
    """
    product = _product(
        product_name=LocalisedText(values={"nl": "microvezeldoek", "fr": "Schoonmaakdoek"})
    )
    written = _copy_for(
        product, _ctx("fr"), "Slogan", "Puce", translations={"product_name": "chiffon microfibre"}
    )

    merged, issues = merge_generated([product], written, _ctx("fr"))

    assert merged[0].product_name.get("fr") == "chiffon microfibre"
    assert merged[0].product_name.get("nl") == "microvezeldoek"  # the correct sibling is untouched
    # And it is reported, so the operator can put it back into MyGS1 rather than re-deriving it.
    assert {i.field for i in issues if i.issue == "value_translated"} >= {"product_name.fr"}


def test_a_correct_value_still_beats_a_written_translation() -> None:
    """The wrong-language rule must not widen into "the producer may overwrite the feed".

    Same shape as the test above with a genuine French value in the slot; the producer's answer is
    dropped. Only a slot the detector reads as another language is open to being replaced.
    """
    product = _product(product_name=LocalisedText(values={"nl": "microvezeldoek", "fr": "Chiffon"}))
    written = _copy_for(
        product, _ctx("fr"), "Slogan", "Puce", translations={"product_name": "Autre chose"}
    )

    merged, _ = merge_generated([product], written, _ctx("fr"))

    assert merged[0].product_name.get("fr") == "Chiffon"


def test_a_wrong_language_value_is_left_alone_when_the_field_is_not_translatable() -> None:
    """`translate` stays the client's decision about its own page — this rule does not override it.

    Without the flag there is no gap, so the value publishes as it stands and the report's §3b
    flag is the only thing that happens. That is the pre-existing behaviour and must not change.
    """
    product = _product(
        product_name=LocalisedText(values={"nl": "microvezeldoek", "fr": "Schoonmaakdoek"})
    )
    context = GenerationContext(languages=["nl", "fr"], default_language="nl", prompt_version="v1")

    assert translation_gaps(product, "fr", context) == []


def test_a_wrong_language_value_with_nothing_to_fill_from_is_not_invented() -> None:
    """The guard rail holds here too: dropping the only value the field has leaves nothing.

    A slot in the wrong language and no other language carrying the field is a source finding for
    MyGS1, not a licence to write one — the same line `translation_gaps` draws for a blank.
    """
    product = _product(product_name=LocalisedText(values={"fr": "Schoonmaakdoek"}))

    fields = {gap.field for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert "product_name" not in fields


def test_a_datapool_placeholder_is_not_something_to_translate() -> None:
    """`zzzanders` is the feed's way of saying "no value", and the generator already knows it.

    Reading it as a value to translate asks the producer to render a placeholder into French and
    then tells the operator to paste that back into MyGS1 — turning a blank into fabricated master
    data, which is the exact failure the guard rail exists to prevent. Found in a real run: one
    in-scope product carries it.
    """
    product = _product(extras={"material": "zzzanders"})

    fields = {gap.field for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert "material" not in fields


def test_a_placeholder_slot_is_dropped_from_a_multi_value_material() -> None:
    """The placeholder guard has to read slots, not the whole string.

    `Material` repeats in the feed, and the parser joins its slots. Testing `startswith("zzz")`
    against the joined value reads `kunststof, zzzanders` as an ordinary material — so the junk
    would reach the page and, worse, §4 of the report would tell the operator to paste it into
    MyGS1. Same class as the placeholder finding that landed with the translation work: the guard
    rail existed, the placeholder walked in through a door it was not standing in.
    """
    product = _product(extras={"material": "kunststof, zzzanders"})

    request = pending_requests([product], _ctx("nl"))[0]

    assert request.inputs.material == "kunststof"


def test_a_placeholder_slot_is_not_offered_for_translation() -> None:
    # The translation source is what §4 tells the operator to put back into MyGS1, so a
    # placeholder surviving into it is fabricated master data, not just a bad page.
    product = _product(extras={"material": "kunststof, zzzanders"})

    gaps = {gap.field: gap for gap in translation_gaps(product, "fr", _ctx("nl", "fr"))}

    assert gaps["material"].source_value == "kunststof"


def test_a_material_of_placeholders_only_is_still_absent() -> None:
    # Dropping every slot must land back on "no material at all" — not on an empty string that
    # renders as a bare `Materiaal:` bullet and reads as a translatable value.
    product = _product(extras={"material": "zzzanders, zzzonbekend"})

    request = pending_requests([product], _ctx("nl"))[0]

    assert request.inputs.material is None
    gaps = translation_gaps(product, "fr", _ctx("nl", "fr"))
    assert "material" not in {gap.field for gap in gaps}


def test_a_material_of_real_slots_is_passed_through_byte_identical() -> None:
    # The guard may only ever remove a placeholder or an empty slot. A value whose slots are all
    # real comes back exactly as the feed wrote it, punctuation and stray spacing included.
    product = _product(extras={"material": "kunststof,metaal , stof"})

    request = pending_requests([product], _ctx("nl"))[0]

    assert request.inputs.material == "kunststof,metaal , stof"


def test_an_empty_slot_is_dropped_whether_or_not_a_placeholder_is_present() -> None:
    """One rule for slots, so no input's rendering depends on another's contents.

    Walking only when a placeholder is present is the cheaper branch and was the first shape of
    this guard. It makes the empty slot in `a, , b` survive while the one in `a, , zzzanders`
    vanishes — the same malformed value rendering two ways depending on what sits beside it.
    """
    with_placeholder = _product(extras={"material": "kunststof, , zzzanders"})
    without = _product(extras={"material": "kunststof, , metaal"})

    assert pending_requests([with_placeholder], _ctx("nl"))[0].inputs.material == "kunststof"
    assert pending_requests([without], _ctx("nl"))[0].inputs.material == "kunststof, metaal"


def test_a_multi_value_material_renders_as_one_technische_details_line() -> None:
    merged = _merge_one(
        _product(extras={**_product().extras, "material": "kunststof, metaal"}),
        "Tagline",
        "Bullet",
    )

    html = merged.generated_description.get("nl")
    assert html is not None
    assert "Materiaal: kunststof, metaal" in html


def test_the_context_selects_exactly_the_sources_marked_translate() -> None:
    """`localised` is not the selector — `translate` is, and only where a client set it.

    `logistics_name` and `marketing_name` are localised and consumed by nothing, so deriving the
    list from `localised` would spend producer tokens on values no page reads. It also has to pick
    up `material`, which is language-agnostic in the feed and so could never be selected that way.
    """
    context = generation_context(
        ["nl", "fr"],
        "nl",
        "v1",
        {
            "product_name": GdsnSource(sheet="S", attribute="3301", localised=True, translate=True),
            "brand": GdsnSource(sheet="S", attribute="3336"),
        },
        {
            "logistics_name": GdsnSource(sheet="S", attribute="3297", localised=True),
            "material": GdsnSource(sheet="B", attribute="Material", translate=True),
        },
    )

    assert [f.field for f in context.translatable] == ["product_name", "material"]
    assert context.translatable[0].source_label == "S attr 3301"
    # A per-language GDSN attribute has a slot the filled value can go back into; 4.012 has not.
    assert context.translatable[0].has_language_slot is True
    assert context.translatable[1].has_language_slot is False


def test_a_field_not_opted_in_is_never_filled() -> None:
    # `logistics_name`/`marketing_name` are carried and consumed by nothing; filling them would
    # be producer tokens spent on a value no page reads.
    product = _product()

    gaps = translation_gaps(product, "fr", _ctx("nl", "fr", translatable=[]))

    assert gaps == []


# --- result_item -------------------------------------------------------------


def test_result_item_carries_the_copy_and_the_fingerprint_it_answered() -> None:
    request = pending_requests([_product()], _ctx("nl"))[0]

    item = result_item(request, _result("Tagline", "Bullet"))

    assert item.gtin == "02987653007359"
    assert item.language == "nl"
    assert item.usps == ["Tagline", "Bullet"]
    # Echoed so `run_plan` can tell copy written for these inputs from copy written for older ones.
    assert item.input_fingerprint == request.input_fingerprint


def test_result_item_rejects_empty_usps() -> None:
    request = pending_requests([_product()], _ctx("nl"))[0]

    with pytest.raises(GeneratorError, match="empty generation result"):
        result_item(request, _result("   "))


def test_result_item_keeps_inferences() -> None:
    request = pending_requests([_product()], _ctx("nl"))[0]

    item = result_item(request, _result("Tagline", "Bullet", inferences=["snoerloos"]))

    assert item.inferences == ["snoerloos"]


def test_result_item_defaults_inferences_to_empty() -> None:
    request = pending_requests([_product()], _ctx("nl"))[0]

    item = result_item(request, _result("Tagline"))

    assert item.inferences == []


# --- merge_generated ---------------------------------------------------------


def _merge_one(product: ProductRecord, *usps: str) -> ProductRecord:
    """Generate for ``product`` (nl) and return the merged record."""
    merged, _ = merge_generated([product], _copy_for(product, _ctx("nl"), *usps), _ctx("nl"))
    return merged[0]


def test_merge_assembles_the_three_part_description() -> None:
    merged = _merge_one(
        _product(), "Alle kabels perfect weggewerkt!", "Klem om te bundelen", "Op maat te knippen"
    )

    assert merged.generated_tagline is not None
    assert merged.generated_tagline.get("nl") == "Alle kabels perfect weggewerkt!"
    html = merged.generated_description.get("nl")
    assert html is not None
    assert "<p><strong>Alle kabels perfect weggewerkt!</strong></p>" in html
    # usps[1:] become Eigenschappen; usps[0] does not reappear as a bullet
    eigenschappen = (
        "<strong>Eigenschappen</strong><br />• Klem om te bundelen<br />• Op maat te knippen"
    )
    assert eigenschappen in html
    assert "• Alle kabels perfect weggewerkt!" not in html
    # Technische details is deterministic from feed data
    assert "<strong>Technische details</strong><br />• 6 Stuk" in html
    assert "Afmetingen: 350 × 250 × 80 Millimeter" in html
    assert "Materiaal: kunststof" in html


def test_merge_reports_one_generated_issue_with_source_input() -> None:
    product = _product()

    _, issues = merge_generated(
        [product], _copy_for(product, _ctx("nl"), "Tagline", "Bullet"), _ctx("nl")
    )

    generated = [i for i in issues if i.issue == "content_generated"]
    assert len(generated) == 1
    assert generated[0].field == "generated_description.nl"
    assert generated[0].value == "Water voor je planten"  # the source-language input


def test_merge_reports_one_generation_inference_per_claim() -> None:
    product = _product()
    written = _copy_for(
        product, _ctx("nl"), "Tagline", "Bullet", inferences=["snoerloos", "oplaadbaar"]
    )

    _, issues = merge_generated([product], written, _ctx("nl"))

    inferred = [i for i in issues if i.issue == "generation_inference"]
    assert len(inferred) == 2
    assert {i.value for i in inferred} == {"snoerloos", "oplaadbaar"}
    assert all(i.field == "generated_description.nl" for i in inferred)


def test_merge_reports_no_inference_issue_when_none_declared() -> None:
    product = _product()

    _, issues = merge_generated(
        [product], _copy_for(product, _ctx("nl"), "Tagline", "Bullet"), _ctx("nl")
    )

    assert [i for i in issues if i.issue == "generation_inference"] == []


def test_the_provenance_of_the_run_reaches_the_report() -> None:
    """Which producer wrote the copy is a property of the file, not of each row in it.

    One producer writes one results file per run, so per-entry provenance never carried anything
    the file could not — but the report still has to name it, because "review this copy" is a
    different instruction depending on where it came from.
    """
    product = _product()
    written = _copy_for(product, _ctx("nl"), "Tagline", "Bullet")

    _, issues = merge_generated(
        [product], _copy(*written.results, provenance="api:claude-sonnet-5"), _ctx("nl")
    )

    generated = next(i for i in issues if i.issue == "content_generated")
    assert "api:claude-sonnet-5" in generated.detail


def test_merge_ignores_a_result_written_for_older_inputs() -> None:
    """Copy keyed on inputs the feed no longer has must not be published (supersession).

    This is the only job the fingerprint still does. It is never a reuse key — nothing lets a run
    skip generating — but `generation_results.json` outlives the producer session that wrote it, so
    a `parse_export` re-run between the two would otherwise publish copy about data that is gone.
    """
    old = _product(description_short=LocalisedText(values={"nl": "Oud"}))
    written = _copy_for(old, _ctx("nl"), "Tagline", "Bullet")

    edited = _product(description_short=LocalisedText(values={"nl": "Nieuw"}))
    merged, _ = merge_generated([edited], written, _ctx("nl"))

    assert merged[0].generated_description is None  # stale result ignored


def test_a_stale_result_says_so_rather_than_going_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping the copy is right; dropping it silently is how wrong pages used to happen.

    The unit then trips E21 and is held out of the plan, which is visible — but only if someone
    reads the skip list. The warning names the unit at the moment the decision is made.
    """
    old = _product(description_short=LocalisedText(values={"nl": "Oud"}))
    written = _copy_for(old, _ctx("nl"), "Tagline", "Bullet")
    edited = _product(description_short=LocalisedText(values={"nl": "Nieuw"}))

    with caplog.at_level("WARNING"):
        merge_generated([edited], written, _ctx("nl"))

    assert "02987653007359" in caplog.text
    assert "nl" in caplog.text


def test_a_result_without_a_fingerprint_is_taken_at_face_value() -> None:
    """`input_fingerprint` is optional on the contract, and a hand-written file may omit it.

    Rejecting those would make the field mandatory by the back door and break the in-session
    producer, whose whole contract is a small JSON file a human can write.
    """
    product = _product()
    item = ResultItem(gtin=product.gtin, language="nl", usps=["Tagline", "Bullet"])

    merged, _ = merge_generated([product], _copy(item), _ctx("nl"))

    assert merged[0].generated_tagline.get("nl") == "Tagline"


def test_merge_flags_a_marketing_message_blank_in_every_language() -> None:
    product = _product(description_short=None)  # no 1083 anywhere
    _, issues = merge_generated([product], _copy(), _ctx("nl"))

    flags = [i for i in issues if i.issue == "missing_generation_input"]
    assert len(flags) == 1
    assert flags[0].field == "description_short.nl"


def test_a_marketing_message_the_feed_has_in_another_language_is_not_a_missing_input() -> None:
    """It is a pending translation, and reporting it as a datapool gap sends the wrong work.

    Before this, every French unit whose 1083 was Dutch-only was reported held — asking the
    operator to write French marketing copy into MyGS1 for a product that already had it in
    Dutch.
    """
    product = _product()  # 1083 in nl only
    _, issues = merge_generated([product], _copy(), _ctx("nl", "fr"))

    assert [i for i in issues if i.issue == "missing_generation_input"] == []


def test_merge_combines_title_with_variation() -> None:
    product = _product(
        product_name=LocalisedText(values={"nl": "Emmer"}),
        extras_localised={"product_variation": LocalisedText(values={"nl": "Set"})},
    )
    merged, _ = merge_generated([product], _copy(), _ctx("nl"))

    assert merged[0].product_name.get("nl") == "Emmer Set"


def _fill_one(product: ProductRecord, language: str, **translations: str) -> ProductRecord:
    """Generate for one language, answering its gaps, and return the merged record."""
    context = _ctx(language)
    written = _copy_for(product, context, "Slogan", "Puce", translations=translations)
    merged, _ = merge_generated([product], written, context)
    return merged[0]


def test_merge_fills_a_missing_name_from_the_results() -> None:
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin"}))  # no fr

    merged = _fill_one(product, "fr", product_name="Pic d'arrosage")

    assert merged.product_name.get("fr") == "Pic d'arrosage"


def test_merge_fills_a_missing_localised_extra() -> None:
    product = _product(extras_localised={"product_variation": LocalisedText(values={"nl": "Set"})})

    merged = _fill_one(product, "fr", product_variation="Ensemble", product_name="Pic")

    assert merged.extra("product_variation", "fr") == "Ensemble"
    # …and it suffixes the *translated* title, not the Dutch one, because the fill runs first.
    assert merged.product_name.get("fr") == "Pic Ensemble"


def test_merge_fills_a_language_agnostic_extra_so_the_page_stops_reading_dutch() -> None:
    """`Matériau: kunststof` on a French page is the failure this closes."""
    merged = _fill_one(_product(), "fr", material="plastique", product_name="Pic")

    assert merged.extra("material", "fr") == "plastique"
    description = merged.generated_description
    assert description is not None
    assert "Matériau: plastique" in (description.get("fr") or "")


def test_filling_a_language_agnostic_extra_keeps_the_language_it_was_authored_in() -> None:
    """Translating for French must not take the value off the Dutch page.

    A flat extra has one value and no language key of its own, so moving it into a per-language
    mapping silently dropped it out of every language but the filled one — the Dutch
    Technische-details block lost its `Materiaal:` line the moment French was translated.
    """
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}))
    context = _ctx("nl", "fr")
    written = _copy(
        *(
            result_item(
                request,
                _result(
                    "Slogan",
                    "Puce",
                    translations={"material": "plastique"} if request.language == "fr" else {},
                ),
            )
            for request in pending_requests([product], context)
        )
    )

    merged, _ = merge_generated([product], written, context)

    assert merged[0].extra("material", "nl") == "kunststof"
    assert merged[0].extra("material", "fr") == "plastique"
    description = merged[0].generated_description
    assert description is not None
    assert "Materiaal: kunststof" in (description.get("nl") or "")
    assert "Matériau: plastique" in (description.get("fr") or "")


def test_merge_fills_a_missing_marketing_message() -> None:
    merged = _fill_one(_product(), "fr", description_short="De l'eau pour vos plantes")

    assert merged.description_short is not None
    assert merged.description_short.get("fr") == "De l'eau pour vos plantes"
    assert merged.description_short.get("nl") == "Water voor je planten"  # feed value kept


def test_the_feed_wins_over_a_written_translation() -> None:
    """Read-side half of the precedence rule; `_requested_translations` is the write-side half.

    A producer that answers for a language the feed already carries must never overwrite the
    datapool's own text — that is silent wrong content on a live page.
    """
    both = _product(product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}))
    written = _copy_for(
        both, _ctx("fr"), "Slogan", "Puce", translations={"product_name": "Autre chose"}
    )

    merged, _ = merge_generated([both], written, _ctx("fr"))

    assert merged[0].product_name.get("fr") == "Pic"


def test_every_filled_value_is_reported_for_mygs1() -> None:
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin"}))
    written = _copy_for(
        product,
        _ctx("fr"),
        "Slogan",
        "Puce",
        translations={"product_name": "Pic d'arrosage", "material": "plastique"},
    )

    _, issues = merge_generated([product], written, _ctx("fr"))

    translated = {i.field: i for i in issues if i.issue == "value_translated"}
    assert set(translated) == {"product_name.fr", "material.fr"}
    # The value IS the deliverable: it is what the operator pastes into MyGS1.
    assert translated["product_name.fr"].value == "Pic d'arrosage"
    assert translated["product_name.fr"].source == "TradeItemDescription attr 3301"
    assert "Bewateringpin" in translated["product_name.fr"].detail  # what it came from
    assert "add it to TradeItemDescription attr 3301 for fr" in translated["product_name.fr"].detail


def test_a_translated_value_with_no_gs1_slot_says_so_rather_than_sending_a_wasted_trip() -> None:
    _, issues = merge_generated(
        [_product()],
        _copy_with_translations(_product(), "fr", {"material": "plastique"}),
        _ctx("fr"),
    )

    material = next(i for i in issues if i.field == "material.fr")
    assert "language-agnostic in GS1" in material.detail
    assert "no fr slot" in material.detail
    # …and on `source`, because the report's table renders that column and not the detail: a row
    # reading only "attr Material" sends the operator hunting for a French field that is not there.
    assert material.source == "BrickGPCCommercialData attr Material — no per-language slot in GS1"


def _copy_with_translations(
    product: ProductRecord, language: str, translations: dict[str, str]
) -> ResultsFile:
    """A results file holding one entry for ``(product, language)`` with these translations."""
    return _copy_for(product, _ctx(language), "Slogan", "Puce", translations=translations)


def test_a_translation_the_request_did_not_ask_for_never_reaches_the_results_file() -> None:
    both = _product(product_name=LocalisedText(values={"nl": "Bewateringpin", "fr": "Pic"}))
    written = _copy_with_translations(both, "fr", {"product_name": "Autre chose"})

    item = next(r for r in written.results if r.language == "fr")
    assert "product_name" not in item.translations


def test_editing_the_source_language_supersedes_the_translation() -> None:
    """Without the source text in the fingerprint, a Dutch edit left the French fill looking fresh.

    The French entry's own inputs are all empty in that case, so nothing else in the fingerprint
    moves — the translation of a value that had since changed would have survived the edit.
    """
    product = _product(product_name=LocalisedText(values={"nl": "Bewateringpin"}))
    written = _copy_with_translations(product, "fr", {"product_name": "Pic d'arrosage"})

    renamed = _product(product_name=LocalisedText(values={"nl": "Waterpin"}))
    merged, _ = merge_generated([renamed], written, _ctx("fr"))

    assert merged[0].product_name.get("fr") is None  # stale result ignored


# --- 1067 routing: verbatim / tighten / generate -----------------------------


def _with_1067(text: str) -> ProductRecord:
    return _product(description_long=LocalisedText(values={"nl": text}))


def test_short_1067_is_published_verbatim_without_ever_reaching_a_producer() -> None:
    """The feed's own short 1067 *is* the copy, so it is materialised at plan time and free.

    This used to be `prefill_from_feed`, which wrote those units into the cache. With no cache
    there is nowhere to write them, so the same rule is applied where the copy is read: the unit
    is never a request (no tokens), and `merge_generated` derives it from the feed on every run.
    """
    product = _with_1067("Speciaal voor kleine honden\nGemaakt van kunststof")

    assert pending_requests([product], _ctx("nl")) == []  # never costs a producer call

    merged, issues = merge_generated([product], _copy(), _ctx("nl"))

    assert merged[0].generated_tagline.get("nl") == "Speciaal voor kleine honden"
    html = merged[0].generated_description.get("nl")
    assert html is not None
    assert "• Gemaakt van kunststof" in html
    # Feed copy is authoritative — it is reported nowhere.
    assert [i for i in issues if i.issue in {"content_generated", "content_adjusted"}] == []


def test_feed_verbatim_copy_wins_over_a_result_for_the_same_unit() -> None:
    """`prefill_from_feed` ran before `pending_requests`, so the feed already won; keep it so.

    Nothing should normally write a result for a verbatim unit, because it is never requested — but
    a hand-written results file can, and the feed's own words stay the authoritative ones rather
    than the decision depending on which of the two happens to be consulted first.
    """
    product = _with_1067("Speciaal voor kleine honden")
    stray = ResultItem(gtin=product.gtin, language="nl", usps=["Iets heel anders"])

    merged, _ = merge_generated([product], _copy(stray), _ctx("nl"))

    assert merged[0].generated_tagline.get("nl") == "Speciaal voor kleine honden"


def test_a_unit_answered_twice_resolves_to_the_last_entry() -> None:
    """Documented rather than incidental, because a mutation showed nothing pinned it.

    Two answers for one unit is a defect in the file, and `run_generate --validate` names it — but
    the read still has to pick one, and which one it picks must not depend on how a hand-written
    file happens to be ordered. Last wins, the same rule a dict literal follows.
    """
    product = _product()
    first = ResultItem(gtin=product.gtin, language="nl", usps=["Eerste"])
    second = ResultItem(gtin=product.gtin, language="nl", usps=["Tweede"])

    merged, _ = merge_generated([product], _copy(first, second), _ctx("nl"))

    assert merged[0].generated_tagline.get("nl") == "Tweede"


def test_long_1067_is_still_asked_for_and_reported_as_adjusted() -> None:
    product = _with_1067("De Democlient Spray Jet is een handige oplossing " * 3)  # > 80 chars

    requests = pending_requests([product], _ctx("nl"))
    assert requests[0].mode == MODE_TIGHTEN
    assert requests[0].candidates  # the 1067 text to shorten

    _, issues = merge_generated(
        [product], _copy_for(product, _ctx("nl"), "Kort", "Bullet"), _ctx("nl")
    )

    assert [i.issue for i in issues if i.issue.startswith("content_")] == ["content_adjusted"]


def test_pending_request_mode_generate_without_1067() -> None:
    requests = pending_requests([_product()], _ctx("nl"))

    assert requests[0].mode == MODE_GENERATE


def test_how_the_copy_came_to_be_is_derived_from_the_feed_not_declared_by_the_producer() -> None:
    """`--ingest` used to stamp `origin` from the request's mode; nothing stamps it now.

    The in-session producer hand-writes `generation_results.json`, and asking it to label its own
    copy `tighten` or `generate` would put a fact the feed already answers into a file a human
    edits. Recomputing it at read time means a results file cannot mislabel its own provenance.
    """
    tightened = _with_1067("A very long feature benefit sentence " * 3)
    generated = _product()

    _, from_1067 = merge_generated(
        [tightened], _copy_for(tightened, _ctx("nl"), "Kort", "B"), _ctx("nl")
    )
    _, from_1083 = merge_generated(
        [generated], _copy_for(generated, _ctx("nl"), "Tagline", "B"), _ctx("nl")
    )

    assert [i.issue for i in from_1067 if i.issue.startswith("content_")] == ["content_adjusted"]
    assert [i.issue for i in from_1083 if i.issue.startswith("content_")] == ["content_generated"]


# --- results file IO ---------------------------------------------------------


def test_results_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    request = pending_requests([_product()], _ctx("nl"))[0]
    results = ResultsFile(
        client_id="democlient",
        provenance="in-session",
        generated_at=_NOW,
        results=[result_item(request, _result("Tagline", "Bullet"))],
    )

    save_results(results)

    assert load_results("democlient") == results


def test_load_results_absent_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent file is "no copy this run", not an error — the doctor is what makes it loud.

    `run_plan` then holds every unit out of the plan under E21, which is the same shape the empty
    cache produced and is caught by `check_generation_results` before a wave starts.
    """
    monkeypatch.chdir(tmp_path)

    assert load_results("democlient").results == []


def test_load_results_corrupt_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "output" / "democlient" / "data" / "generation_results.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(GeneratorError, match="corrupt"):
        load_results("democlient")


def test_load_results_for_another_client_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A results file names the client it was produced for, and copy is not interchangeable.

    The check lived in `run_generate --ingest`; it moves here so every reader gets it, `run_plan`
    included — that one publishes, and it never saw the check before.

    Written to the path by hand rather than through `save_results`, because that derives the path
    from `client_id` and so can never produce this. The operator shell can: the Content screen
    saves whatever file is uploaded to *this* client's path.
    """
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "output" / "democlient" / "data" / "generation_results.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"client_id": "acme", "results": []}', encoding="utf-8")

    with pytest.raises(GeneratorError, match="acme"):
        load_results("democlient")


# --- Copy older than its page is not sent again --------------------------------------------------


def _with_copy(**languages: str) -> ProductRecord:
    return _product(
        product_name=LocalisedText(values={"nl": "Dizzy Dog", "fr": "Dizzy Dog"}),
        generated_tagline=LocalisedText(values=dict(languages)),
        generated_description=LocalisedText(
            values={lang: f"<p>{text}</p>" for lang, text in languages.items()}
        ),
    )


def test_copy_written_before_the_page_was_published_is_taken_back() -> None:
    """Left over from the run that published it: sending it again rewrites live text."""
    product = _with_copy(nl="Speels", fr="Ludique")
    published = {(product.gtin, "nl"): _NOW, (product.gtin, "fr"): _NOW}

    (out,) = withhold_published_copy([product], published, _NOW)

    assert out.generated_tagline is None
    assert out.generated_description is None
    assert out.product_name == product.product_name, "the title is not copy"


def test_copy_written_after_the_page_was_published_is_kept() -> None:
    """Content was run again for this page — "include already-published pages" — so it counts."""
    product = _with_copy(nl="Speels")
    later = _NOW.replace(day=19)

    (out,) = withhold_published_copy([product], {(product.gtin, "nl"): _NOW}, later)

    assert out == product


def test_copy_is_taken_back_per_language() -> None:
    product = _with_copy(nl="Speels", fr="Ludique")
    earlier = _NOW.replace(day=17)

    (out,) = withhold_published_copy([product], {(product.gtin, "nl"): _NOW}, earlier)

    assert out.generated_tagline == LocalisedText(values={"fr": "Ludique"})


def test_an_unpublished_product_keeps_its_copy() -> None:
    product = _with_copy(nl="Speels")

    assert withhold_published_copy([product], {}, _NOW) == [product]


def test_an_undated_results_file_takes_nothing_back() -> None:
    product = _with_copy(nl="Speels")

    assert withhold_published_copy([product], {(product.gtin, "nl"): _NOW}, None) == [product]
