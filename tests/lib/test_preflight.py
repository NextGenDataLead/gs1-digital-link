"""Tests for lib/preflight.py — the checks behind ``scripts/doctor``.

Each check is a pure function from configuration to a verdict, so most of these build a
``ClientConfig`` and assert the status and the words. The two network checks are driven
against recording fakes patched over ``WordPressClient`` / ``GS1DigitalLinkClient``, because
what is being tested is the *interpretation* of each failure — which one gets the six-groups
hint, which one names the 21011 contract blocker — not the HTTP wiring those clients already
have covered exhaustively.

One test here is about a side effect rather than a verdict: a preflight must never quarantine
a corrupt ``state.json``. That is what makes it safe to run at any time.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from lib.categories import assign_categories
from lib.config import (
    CategoryConfig,
    ClientConfig,
    ExportConfig,
    GeneratorConfig,
    GS1Config,
    MediaConfig,
    ProcessListConfig,
    WordPressConfig,
)
from lib.errors import GS1APIError, MissingCredentialError, WordPressAPIError
from lib.gdsn import GdsnSource
from lib.generator import (
    GenerationResult,
    ResultItem,
    ResultsFile,
    generation_context,
    pending_requests,
    result_item,
    save_results,
)
from lib.input_layout import archive_path
from lib.preflight import (
    Status,
    check_client_files,
    check_config,
    check_ffmpeg,
    check_generation_results,
    check_generator,
    check_gs1,
    check_process_list,
    check_scope,
    check_selection_matches_export,
    check_video_coverage,
    check_wordpress,
    in_scope,
    load_video_status,
    run_checks,
    units_needing_copy,
    worst_status,
)
from lib.provenance import history_path, record_selection, record_upload
from lib.records import LocalisedText, ProductRecord, State, StateEntry
from lib.state import diff_against_state, save_state, state_path
from lib.wp_client import WordPressIdentity

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"
GTIN_C = "02987653007361"


# --- Builders ----------------------------------------------------------------


def _make_config(**overrides: Any) -> ClientConfig:
    params: dict[str, Any] = {
        "client_id": "acme",
        "display_name": "Acme BV",
        "gs1": GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
        ),
        "export": ExportConfig(path="input/acme.xlsx"),
        "wordpress": WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl"],
            slug_pattern="p-{gtin}",
            target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
        ),
    }
    params.update(overrides)
    return ClientConfig(**params)


def _product(gtin: str = GTIN_A) -> ProductRecord:
    return ProductRecord(
        gtin=gtin, brand="Acme", product_name=LocalisedText(values={"nl": "Rugsteun"})
    )


def _write_process_list(tmp_path: Path, gtins: list[str]) -> ProcessListConfig:
    import openpyxl  # noqa: PLC0415 — only this helper needs it

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode"])
    for gtin in gtins:
        sheet.append([gtin])
    path = tmp_path / "process-list.xlsx"
    workbook.save(path)
    return ProcessListConfig(path=str(path), gtin_column="Barcode")


def _write_video_map(tmp_path: Path, confirmed: list[str], languages: list[str]) -> str:
    entries = {
        language: [{"file": f"{g}_{language}.mp4", "gtin": g, "confirmed": True} for g in confirmed]
        for language in languages
    }
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return str(path)


# --- check_config -------------------------------------------------------------


def test_config_missing_file_says_what_to_copy(tmp_path: Path) -> None:
    result = check_config(tmp_path / "nope.yml")
    assert result.status is Status.FAIL
    assert "clients.example.yml" in result.remedy


def test_config_reports_every_schema_error_not_just_the_first(tmp_path: Path) -> None:
    """An operator with four blank fields must not fix them one run at a time.

    ``load_clients`` raises on the first violation and discards its ``json_path``, so the
    schema is walked directly here to collect all of them with the field each belongs to.
    """
    path = tmp_path / "clients.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "clients": {
                    "acme": {
                        "display_name": "Acme",
                        "gs1": {"environment": "wrong-value"},
                        "wordpress": {"site_url": "https://wp.test"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = check_config(path)

    assert result.status is Status.FAIL
    errors = result.data["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 1  # not stopped at the first
    assert any("gs1" in error for error in errors)
    assert any("wordpress" in error for error in errors)


def test_config_unreadable_yaml_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "clients.yml"
    path.write_text("clients: [unclosed\n", encoding="utf-8")

    result = check_config(path)

    assert result.status is Status.FAIL
    assert "YAML" in result.detail


# --- check_generator (the E21 trap) ------------------------------------------


def test_generator_block_present_is_ok() -> None:
    result = check_generator(_make_config(generator=GeneratorConfig(enabled=True)))
    assert result.status is Status.OK
    assert "held out of the plan" in result.detail


def test_generator_block_missing_with_generated_copy_present_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap: the block looks like dead config on a machine with no API key. It is the switch.

    ``run_plan`` derives ``require_generated_copy = cfg.generator is not None``, so deleting
    the block does not raise — it turns off the E21 hold, and copy-less units publish blank
    taglines. Generated copy on disk proves a generator *was* configured for this client. That
    evidence used to be the cache's entry count; the results file is what carries it now.
    """
    monkeypatch.chdir(tmp_path)
    # An empty results file is not evidence that a generator was ever configured; one holding
    # copy is — that copy came from somewhere.
    save_results(ResultsFile(client_id="acme", results=[_written_unit()]))

    result = check_generator(_make_config())

    assert result.status is Status.FAIL
    assert "blank tagline" in result.detail
    assert "not a credential" in result.remedy


def test_generator_block_missing_with_no_generated_copy_is_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = check_generator(_make_config())
    assert result.status is Status.NA


def _written_unit(gtin: str = GTIN_A, fingerprint: str = "fp") -> ResultItem:
    return ResultItem(
        gtin=gtin, language="nl", usps=["Een voordeel"], input_fingerprint=fingerprint
    )


def _results_for(cfg: ClientConfig, products: list[ProductRecord]) -> ResultsFile:
    """A results file answering every unit these products make pending, fingerprints matching."""
    context = generation_context(
        cfg.wordpress.languages,
        cfg.wordpress.default_language,
        cfg.generator.prompt_version if cfg.generator else "v1",
        cfg.export.gdsn_map,
        cfg.export.gdsn_extras,
    )
    return ResultsFile(
        client_id=cfg.client_id,
        results=[
            result_item(request, GenerationResult(usps=["Een voordeel", "Nog een"]))
            for request in pending_requests(products, context)
        ],
    )


# --- Scope --------------------------------------------------------------------


def test_scope_applies_the_process_list_but_not_the_video_allowlist(tmp_path: Path) -> None:
    """The process list narrows scope; a missing video does not — it holds, visibly (E24).

    A GTIN the operator listed and cannot yet have is a different fact from one they never
    asked about, and only the first is actionable. Narrowing here made it invisible on every
    surface at once — this figure, the plan, and the quality report.
    """
    cfg = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
        ),
    )
    products = [_product(GTIN_A), _product(GTIN_B), _product("02987653007361")]

    scoped = in_scope(cfg, products)

    # B has no confirmed video but stays in scope; only the unlisted third product is dropped.
    assert [p.gtin for p in scoped] == [GTIN_A, GTIN_B]


def test_the_scope_check_names_the_gtins_and_not_only_how_many(tmp_path: Path) -> None:
    """A count lets a consumer *report* scope; the list lets it *filter* by it.

    The operator shell's Content screen needs the second: it lists generated copy, and the cache
    accumulates every unit ever generated on that machine, so showing this run's batch means
    intersecting the two. Without the list it would have to re-derive scope, and a second
    implementation of "what will this run touch" is what ``in_scope`` exists to prevent.

    ``ProductRecord.gtin`` verbatim, because that is the field the generator keys its cache by —
    a normalised variant would silently match nothing for a 13-digit feed.
    """
    cfg = _make_config(process_list=_write_process_list(tmp_path, [GTIN_A]))

    result = check_scope(cfg, [_product(GTIN_A), _product(GTIN_B)])

    assert result.data["in_scope_gtins"] == [GTIN_A]
    assert result.data["in_scope"] == 1


def test_the_gtin_list_is_never_truncated(tmp_path: Path) -> None:
    """``pending_units`` is capped at 20 because it is read; this list is *filtered with*.

    A truncated filter hides in-scope work, which is the failure this data exists to fix rather
    than a tidier version of it.
    """
    gtins = [f"0298765300{n:04d}" for n in range(50)]
    cfg = _make_config(process_list=_write_process_list(tmp_path, gtins))

    result = check_scope(cfg, [_product(gtin) for gtin in gtins])

    assert result.data["in_scope_gtins"] == gtins


def test_the_scope_sentence_only_names_filters_that_were_applied(tmp_path: Path) -> None:
    """It named ``restrict_to_mapped_gtins`` while :func:`in_scope` has never applied it (#117).

    The figure was right and the sentence was wrong, which is the harder way round to notice. On
    the pilot it read "37 in scope, after the process list **and** media.restrict_to_mapped_gtins"
    when 20 of those 37 were held for want of a video and only 17 could publish — and a held unit
    produces no error, no row and no count, so the sentence was the operator's only clue and it
    pointed away from the work.

    Gate 0 renders this string verbatim, which is where the picture of a run gets formed.
    """
    cfg = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
        ),
    )
    products = [_product(GTIN_A), _product(GTIN_B)]

    detail = check_scope(cfg, products).detail

    assert "after process list" in detail
    # The rule may be *mentioned* — it holds a product — but never as something already applied.
    before_the_hold = detail.split("held for want")[0]
    assert "restrict_to_mapped_gtins" not in before_the_hold, (
        f"the sentence still claims the video rule narrowed the figure: {detail!r}"
    )


def test_the_scope_sentence_reports_what_a_run_would_actually_publish(tmp_path: Path) -> None:
    """Both numbers, because the difference between them is the operator's next job."""
    cfg = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
        ),
    )
    products = [_product(GTIN_A), _product(GTIN_B)]

    result = check_scope(cfg, products)

    assert "2 of 2 product(s) in the export are in scope" in result.detail
    assert "1 of those are held for want of a confirmed video" in result.detail
    assert "so a run would publish 1" in result.detail
    assert result.data["in_scope"] == 2, "the figure stays the superset in_scope promises"


def test_the_hold_count_is_silent_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    """No rule, or every product mapped, means no second sentence to skim past."""
    mapped_both = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A]),
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
        ),
    )
    no_rule = _make_config(process_list=_write_process_list(tmp_path, [GTIN_A]))

    for cfg in (mapped_both, no_rule):
        assert "held for want" not in check_scope(cfg, [_product(GTIN_A)]).detail


def test_an_unreadable_video_map_does_not_become_a_hold_count_of_zero(tmp_path: Path) -> None:
    """`check_video_coverage` reports that properly; this one must not guess, or fail twice.

    Reporting zero held would be worse than saying nothing: it is the reassuring answer, and it
    would be produced by exactly the situation where nothing is known.
    """
    broken = tmp_path / "broken.yml"
    broken.write_text("nl: [this is not\n  a mapping", encoding="utf-8")
    cfg = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A]),
        media=MediaConfig(restrict_to_mapped_gtins=True, video_map_path=str(broken)),
    )

    detail = check_scope(cfg, [_product(GTIN_A)]).detail

    assert "held for want" not in detail
    assert "so a run would publish" not in detail


def test_publishing_without_video_says_how_many_go_live_without_one(tmp_path: Path) -> None:
    """Nothing is held for a missing video, and the sentence says so — and counts what is bare."""
    cfg = _make_config(
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            publish_without_video=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
        ),
    )

    detail = check_scope(cfg, [_product(GTIN_A), _product(GTIN_B)]).detail

    assert "held" not in detail
    assert "1 would publish with no video in at least one language" in detail


def test_the_shared_video_join_carries_the_publish_without_video_setting(tmp_path: Path) -> None:
    """The report and the Data screen both read this loader; it must not drop the setting.

    Without it both would call 46 publishable products "held" while the run published them.
    """
    vmap = _write_video_map(tmp_path, [GTIN_B], ["nl"])  # GTIN_A has no video at all
    for setting in (False, True):
        cfg = _make_config(
            media=MediaConfig(
                restrict_to_mapped_gtins=True, publish_without_video=setting, video_map_path=vmap
            )
        )

        status = load_video_status(cfg, [_product(GTIN_A)])

        assert status is not None
        assert [p.gtin for p in status.held] == ([] if setting else [GTIN_A])
        assert [p.gtin for p in status.without_video] == ([GTIN_A] if setting else [])


def test_scope_of_nothing_is_a_failure_not_a_quiet_pass(tmp_path: Path) -> None:
    """An empty scope means a run that publishes nothing and reports success."""
    cfg = _make_config(process_list=_write_process_list(tmp_path, [GTIN_B]))

    result = check_scope(cfg, [_product(GTIN_A)])

    assert result.status is Status.FAIL
    assert "publish nothing and report success" in result.detail


# --- units_needing_copy: which units a run would create or change -------------
#
# Copy is written per run for the rows a run executes — NEW and CHANGED. This is what decides
# which those are, and it is the same answer `run_plan` will reach, because both go through
# `lib.state`'s classifier over category-assigned records.


def _publish(cfg: ClientConfig, product: ProductRecord, language: str = "nl") -> None:
    """Record ``product`` in state as live and matching, the way a successful run would."""
    rows, _ = diff_against_state(
        [product], State(client_id=cfg.client_id, entries={}), [language], cfg.wordpress
    )
    row = rows[0]
    save_state(
        State(
            client_id=cfg.client_id,
            entries={
                product.gtin: {
                    language: StateEntry(
                        wp_page_id=1,
                        wp_url=row.target_url,
                        wp_featured_media_id=None,
                        content_hash=row.content_hash,
                        gs1_link_set_hash="g" * 64,
                        last_run=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
                        title=row.title,
                    )
                }
            },
        )
    )


def test_units_needing_copy_is_the_new_and_changed_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    live, fresh = _product(GTIN_A), _product(GTIN_B)
    _publish(cfg, live)

    assert units_needing_copy(cfg, [live, fresh]) == {(GTIN_B, "nl")}


def test_units_needing_copy_includes_a_unit_whose_content_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    product = _product(GTIN_A)
    _publish(cfg, product)
    edited = product.model_copy(update={"brand": "Ander Merk"})

    assert units_needing_copy(cfg, [edited], include_live=True) == {(GTIN_A, "nl")}


def test_a_live_page_that_changes_needs_no_copy_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy is optional for a page already live: without it, the page keeps its text.

    Regenerating replaces live text, so it is asked for — never the default (operator,
    2026-10-22: a new title must not rewrite the copy).
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    product = _product(GTIN_A)
    _publish(cfg, product)
    edited = product.model_copy(update={"brand": "Ander Merk"})

    assert units_needing_copy(cfg, [edited]) == set()


def test_units_needing_copy_gives_up_wide_when_state_will_not_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` means "could not decide", and every caller then asks for every unit.

    Erring wide is the same direction ``in_scope`` errs. Narrow here and a run silently writes no
    copy for units it is about to publish, which surfaces as blank pages rather than as an error.
    And it must not *quarantine* the file: ``load_state`` would, and consuming the E19 reset here
    means the operator never sees "every row re-plans as NEW" at the plan gate.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    path = state_path(cfg.client_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert units_needing_copy(cfg, [_product(GTIN_A)]) is None
    assert path.read_text(encoding="utf-8") == "{ not json"  # left exactly as it was


def test_units_needing_copy_gives_up_wide_without_url_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    wordpress = _make_config().wordpress.model_copy(update={"slug_pattern": None})
    cfg = _make_config(wordpress=wordpress)

    assert units_needing_copy(cfg, [_product(GTIN_A)]) is None


def test_units_needing_copy_assigns_categories_before_classifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The category is in the content hash, so skipping it classifies every live unit CHANGED.

    That failure is quiet and expensive: generation would be scoped to "everything" for ever, and
    the narrowing would look like it worked.
    """
    monkeypatch.chdir(tmp_path)
    categories = CategoryConfig(
        terms=["tuin"], brick_category_map={"10003865": "tuin"}, overrides={}
    )
    cfg = _make_config(categories=categories)
    product = _product(GTIN_A).model_copy(update={"gpc_brick_code": "10003865"})
    categorised, _ = assign_categories(categories, [product])
    _publish(cfg, categorised[0])

    assert units_needing_copy(cfg, [product]) == set()


def test_units_needing_copy_leaves_out_a_product_the_plan_will_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowing this function existed without. It classified, and stopped there.

    A product with no confirmed video is *in scope and held* (E24) — it reaches the plan and is
    dropped there, so copy written for it is copy nobody will ever read. On the pilot client that
    was 54 of 74 units. ``in_scope`` deliberately does not filter these out (a held product must
    stay visible in the report); this is where they stop costing a producer call.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        media=MediaConfig(
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
            restrict_to_mapped_gtins=True,
        )
    )

    assert units_needing_copy(cfg, [_product(GTIN_A), _product(GTIN_B)]) == {(GTIN_A, "nl")}


def test_units_needing_copy_still_asks_for_a_gap_the_producer_will_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowing too far is the quieter failure: no copy, then E21, then a page that never lands.

    ``product_name`` is mandatory and missing in fr, but it is marked ``translate: true`` and the
    feed carries nl — so the plan, which checks E23 after generation, will publish this product.
    Asking for its copy is the whole reason it can.
    """
    monkeypatch.chdir(tmp_path)
    wordpress = _make_config().wordpress.model_copy(update={"languages": ["nl", "fr"]})
    cfg = _make_config(
        wordpress=wordpress,
        generator=GeneratorConfig(enabled=True),
        export=ExportConfig(
            path="input/acme.xlsx",
            gdsn_map={
                "product_name": GdsnSource(
                    sheet="S", attribute="3301", localised=True, required=True, translate=True
                )
            },
        ),
    )

    assert units_needing_copy(cfg, [_product(GTIN_A)]) == {(GTIN_A, "nl"), (GTIN_A, "fr")}


def test_units_needing_copy_gives_up_wide_when_the_video_map_will_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold it cannot evaluate must not read as "nothing is held" — check_video_map reports it."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "mapping.yml"
    path.write_text("nl: [", encoding="utf-8")
    cfg = _make_config(media=MediaConfig(video_map_path=str(path), restrict_to_mapped_gtins=True))

    assert units_needing_copy(cfg, [_product(GTIN_A)]) is None


# --- Generated copy for this run ---------------------------------------------


def test_generation_results_count_only_the_units_this_run_would_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the doctor FAILs on every run after the first, which is how a check stops working.

    An already-published unit is not generated for, so counting it as uncovered would report the
    system working correctly as a failure.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
    )
    live, fresh = _product(GTIN_A), _product(GTIN_B)
    _publish(cfg, live)
    save_results(_results_for(cfg, [fresh]))

    result = check_generation_results(cfg, [live, fresh])

    assert result.status is Status.OK
    assert result.data["total"] == 1
    assert result.data["covered"] == 1
    # Named, not silently dropped: a narrowing nobody can see is the one that turns out wrong.
    assert result.data["unchanged"] == 1
    assert "1 in-scope unit(s) are already live and unchanged" in result.detail


def test_generation_results_keep_held_units_apart_from_unchanged_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reasons a unit needs no copy, and only one of them means "finished".

    Summed into ``unchanged`` — the shape this codebase keeps producing — a held product would be
    reported as an already-published one, and the check would say a run is complete when a third
    of it is waiting on a video nobody has been told about.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        media=MediaConfig(
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
            restrict_to_mapped_gtins=True,
        ),
    )
    workable, held = _product(GTIN_A), _product(GTIN_B)
    save_results(_results_for(cfg, [workable]))

    result = check_generation_results(cfg, [workable, held])

    assert result.status is Status.OK
    assert result.data["total"] == 1
    assert result.data["held"] == 1
    assert result.data["unchanged"] == 0
    assert "1 in-scope unit(s) are held by the plan" in result.detail


def test_generation_results_count_blocked_products_by_who_unblocks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The units figure says what the run will do; this one says who has to go and fix something.

    Held units are counted in units everywhere else, and a unit is half a product here (two
    languages), so "46 held" is a number an operator must translate before it means anything —
    and translating it still does not say whether to chase a video or a value in MyGS1. Split by
    the plan's own attribution, first-rule-fired, so a product failing both is chased where the
    plan will actually drop it rather than in two places.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        media=MediaConfig(
            video_map_path=_write_video_map(tmp_path, [GTIN_A, GTIN_C], ["nl"]),
            restrict_to_mapped_gtins=True,
            require_hero_image=True,
        ),
    )
    publishable = _product(GTIN_A).model_copy(update={"image_url": "https://wp.test/a.jpg"})
    no_video = _product(GTIN_B).model_copy(update={"image_url": "https://wp.test/b.jpg"})
    no_image = _product(GTIN_C)  # confirmed video, blank hero — E22, a source-data problem
    save_results(_results_for(cfg, [publishable]))

    result = check_generation_results(cfg, [publishable, no_video, no_image])

    assert result.data["products_to_publish"] == 1
    assert result.data["products_held_video"] == 1
    assert result.data["products_held_data"] == 1


def test_generation_results_count_finished_products_apart_from_blocked_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product with nothing left to do is not a product waiting on somebody."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]),
    )
    live, fresh = _product(GTIN_A), _product(GTIN_B)
    _publish(cfg, live)
    save_results(_results_for(cfg, [fresh]))

    result = check_generation_results(cfg, [live, fresh])

    assert result.data["products_unchanged"] == 1
    assert result.data["products_to_publish"] == 1
    assert result.data["products_held_video"] == 0
    assert result.data["products_held_data"] == 0


def test_generation_results_leave_the_product_split_out_when_it_cannot_be_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``units_needing_copy`` returning ``None`` means "ask for everything", not "nothing is held".

    A breakdown built on that set would name products it never examined, so there is none — and
    the screen falls back to the check's own detail rather than printing a confident zero.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(generator=GeneratorConfig(enabled=True))
    (tmp_path / "output" / "acme" / "data").mkdir(parents=True)
    (tmp_path / "output" / "acme" / "state.json").write_text("{not json", encoding="utf-8")

    result = check_generation_results(cfg, [_product(GTIN_A)])

    # Pins the branch: "ask for everything" is one product times one language, and the FAIL-early
    # path through an unreadable results file would satisfy the absences below without it.
    assert result.data["total"] == 1
    assert "products_held_video" not in result.data
    assert "products_unchanged" not in result.data


def test_generation_results_count_only_the_products_in_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting 224 missing units the run will never need teaches the operator to look away."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A]),
    )
    save_results(ResultsFile(client_id="acme", results=[]))

    result = check_generation_results(cfg, [_product(GTIN_A), _product(GTIN_B)])

    assert result.status is Status.FAIL
    assert result.data["total"] == 1  # one in-scope product × one language
    assert result.data["pending"] == 1


def test_generation_results_are_ok_when_every_in_scope_unit_is_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A]),
    )
    save_results(_results_for(cfg, [_product(GTIN_A)]))

    result = check_generation_results(cfg, [_product(GTIN_A)])

    assert result.status is Status.OK
    assert result.data["covered"] == 1
    assert result.data["pending"] == 0


def test_generation_results_written_for_older_inputs_do_not_count_as_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that makes a forgotten regeneration loud instead of wrong copy on a live page.

    A results file outlives the producer session that wrote it, so a `parse_export` re-run in
    between leaves copy describing data the feed no longer holds. `run_plan` drops it — this is
    what says so before a wave starts rather than after.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A]),
    )
    save_results(ResultsFile(client_id="acme", results=[_written_unit(fingerprint="stale")]))

    result = check_generation_results(cfg, [_product(GTIN_A)])

    assert result.status is Status.FAIL
    assert result.data["pending"] == 1
    assert "prompt_version bump" in result.remedy


def test_a_results_file_for_another_client_is_reported_rather_than_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor exists to say what is wrong before a wave; crashing on it does the opposite.

    Reachable from the operator shell, which saves whatever file is uploaded to *this* client's
    path — so the wrong file is one mis-click away, and it used to take the doctor down with it.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A]),
    )
    path = tmp_path / "output" / "acme" / "data" / "generation_results.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"client_id": "other", "results": []}', encoding="utf-8")

    result = check_generation_results(cfg, [_product(GTIN_A)])

    assert result.status is Status.FAIL
    assert "other" in result.detail


def test_generation_results_without_a_generator_is_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert check_generation_results(_make_config(), [_product()]).status is Status.NA


# --- Process list -------------------------------------------------------------


def test_process_list_with_no_gtins_fails(tmp_path: Path) -> None:
    """Zero GTINs is deliberately an error, not an empty run."""
    cfg = _make_config(process_list=_write_process_list(tmp_path, []))
    assert check_process_list(cfg).status is Status.FAIL


def test_process_list_reports_the_count(tmp_path: Path) -> None:
    cfg = _make_config(process_list=_write_process_list(tmp_path, [GTIN_A, GTIN_B]))
    result = check_process_list(cfg)
    assert result.status is Status.OK
    assert result.data["count"] == 2


# --- Selection vs export ------------------------------------------------------


def _layout(tmp_path: Path) -> tuple[Path, ProcessListConfig]:
    """A client in the shipped layout, with both live files present."""
    import openpyxl  # noqa: PLC0415 — only this helper needs it

    root = tmp_path / "input" / "acme" / "process"
    export = root / "uploads" / "GS1 export" / "export.xlsx"
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_bytes(b"exported")
    selection = root / "selection" / "selections.xlsx"
    selection.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode"])
    sheet.append([GTIN_A])
    workbook.save(selection)
    return export, ProcessListConfig(path=str(selection), gtin_column="Barcode")


def _record_save(export: Path, selection: Path) -> None:
    saved = selection.with_name("selection-20260927T101600.xlsx")
    saved.write_bytes(selection.read_bytes())
    record_selection(
        history_path(export),
        saved,
        product_list=archive_path(selection),
        export=export,
    )


def test_a_selection_chosen_against_this_export_passes(tmp_path: Path) -> None:
    export, listed = _layout(tmp_path)
    _record_save(export, Path(listed.path))
    cfg = _make_config(export=ExportConfig(path=str(export)), process_list=listed)

    assert check_selection_matches_export(cfg).status is Status.OK


def test_a_selection_chosen_against_a_replaced_export_warns_and_names_it(tmp_path: Path) -> None:
    """Nothing warned about this before, and the failure it catches is entirely silent.

    A barcode the current export has no row for produces no plan row, no error and no count — the
    only evidence is a total one smaller than expected.
    """
    # Arrange
    export, listed = _layout(tmp_path)
    dated = export.with_name("export-20260620T090000.xlsx")
    dated.write_bytes(export.read_bytes())
    record_upload(history_path(export), "export", kept=dated, given_name="Q2.xlsx")
    _record_save(export, Path(listed.path))
    export.write_bytes(b"this quarter's export")
    cfg = _make_config(export=ExportConfig(path=str(export)), process_list=listed)

    # Act
    result = check_selection_matches_export(cfg)

    # Assert
    assert result.status is Status.WARN, "a warning, never a failure — only the operator knows"
    assert result.data["chosen_against"] == "export-20260620T090000.xlsx"
    assert result.remedy, "a warning with nothing to do about it is noise"


def test_a_selection_nobody_recorded_is_na_rather_than_passing(tmp_path: Path) -> None:
    """Every batch saved before this was kept. A green line there is a claim nobody made — the
    overclaiming that had to be taken out of the scope sentence once already.
    """
    export, listed = _layout(tmp_path)
    cfg = _make_config(export=ExportConfig(path=str(export)), process_list=listed)

    assert check_selection_matches_export(cfg).status is Status.NA


def test_a_client_with_no_selection_has_nothing_to_compare(tmp_path: Path) -> None:
    export, _ = _layout(tmp_path)
    cfg = _make_config(export=ExportConfig(path=str(export)), process_list=None)

    assert check_selection_matches_export(cfg).status is Status.NA


# --- Video mapping ------------------------------------------------------------


def test_unconfirmed_videos_warn_rather_than_fail_under_the_restriction(tmp_path: Path) -> None:
    """The restriction is what makes a gap safe: the GTIN is excluded, not mis-published.

    Calling a handled condition a failure is how a report earns the right to be ignored.
    """
    folder = tmp_path / "videos" / "nl"
    folder.mkdir(parents=True)
    (folder / "unmapped.mp4").write_bytes(b"x")
    cfg = _make_config(
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
            video_folders={"nl": str(folder)},
        )
    )

    result = check_video_coverage(cfg)

    assert result.status is Status.WARN
    assert "only those can be published" in result.detail


def test_absent_video_files_are_reported_as_absent_files(tmp_path: Path) -> None:
    """The mapping arrived and the multi-gigabyte library did not — a day-one operator machine.

    It used to read ``284 of 0 video file(s) are not yet confirmed against a GTIN``: every gap of
    every kind over the number of files on disk. The fix for "the folders are empty" is to copy
    them across, which is nothing like the fix for "the client has not confirmed these rows", so
    the two say different things now.
    """
    folder = tmp_path / "videos" / "nl"
    folder.mkdir(parents=True)  # exists, but empty — as it is before the library is copied
    cfg = _make_config(
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A], ["nl"]),
            video_folders={"nl": str(folder)},
        )
    )

    result = check_video_coverage(cfg)

    assert result.status is Status.WARN
    assert "no video files found" in result.detail
    assert "not yet confirmed" not in result.detail, "the mapping is confirmed; the files are gone"
    assert "media.video_folders" in result.remedy
    assert result.data["files"] == 0


def test_the_video_check_never_counts_more_than_it_measured(tmp_path: Path) -> None:
    """A regression guard on the shape of the sentence, not on one wording of it.

    ``N of M`` with N > M is not a cosmetic defect: a count that cannot be true teaches its
    reader to skip the line, on the one screen they are meant to work down. Whatever this line
    says in future, it must not claim more of something than it found.
    """
    folder = tmp_path / "videos" / "nl"
    folder.mkdir(parents=True)
    cfg = _make_config(
        media=MediaConfig(
            restrict_to_mapped_gtins=True,
            video_map_path=_write_video_map(tmp_path, [GTIN_A, GTIN_B], ["nl"]),
            video_folders={"nl": str(folder)},
        )
    )

    (folder / "one.mp4").write_bytes(b"x")
    result = check_video_coverage(cfg)

    # True of the wording as it stands: the files it reports are the files it found.
    assert result.data["files"] == 1
    assert "1 video file(s) found" in result.detail

    # And true of any future wording: no ratio may claim more than it measured.
    for numerator, denominator in re.findall(r"(\d+) of (\d+)", result.detail):
        assert int(numerator) <= int(denominator), f"impossible count in: {result.detail}"


def test_a_hand_edited_mapping_fails_with_a_position_not_a_traceback(tmp_path: Path) -> None:
    """A missing file already reported cleanly; the file a human edits is the one that crashed."""
    path = tmp_path / "mapping.yml"
    path.write_text('nl:\n  - {file: "a.mp4", gtin: ""}\n\tstray tab\n', encoding="utf-8")
    cfg = _make_config(media=MediaConfig(video_map_path=str(path)))

    result = check_video_coverage(cfg)

    assert result.status is Status.FAIL
    assert "line 3" in result.detail
    assert result.remedy, "a failure an operator can act on needs to say how"


def test_a_hand_edited_mapping_does_not_crash_the_checks_that_run_first(tmp_path: Path) -> None:
    """``check_scope`` reads the same file, earlier, and used to take the whole doctor down.

    That is why a stray tab produced a traceback instead of one red line: the crash happened
    before the check that would have reported it ever ran.
    """
    path = tmp_path / "mapping.yml"
    path.write_text('nl:\n  - {file: "a.mp4", gtin: ""}\n\tstray tab\n', encoding="utf-8")
    cfg = _make_config(
        media=MediaConfig(restrict_to_mapped_gtins=True, video_map_path=str(path)),
        process_list=None,
    )

    scope = check_scope(cfg, [_product(GTIN_A)])

    assert scope.status in {Status.OK, Status.WARN, Status.FAIL}  # a verdict, not an exception


# --- ffmpeg -------------------------------------------------------------------


def test_ffmpeg_is_not_applicable_when_transcoding_is_off() -> None:
    cfg = _make_config(media=MediaConfig(video_transcode=False))
    assert check_ffmpeg(cfg).status is Status.NA


def test_ffmpeg_missing_binary_fails_when_transcoding_is_on() -> None:
    cfg = _make_config(
        media=MediaConfig(video_transcode=True, ffmpeg_bin="ffmpeg-that-is-not-here")
    )
    result = check_ffmpeg(cfg)
    assert result.status is Status.FAIL
    assert "video_transcode" in result.remedy


# --- WordPress ----------------------------------------------------------------


def _patch_wp(monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> None:
    """Patch WordPressClient with a fake whose whoami/detect behaviour the test chooses."""

    class FakeWP:
        def __init__(self, config: WordPressConfig, **_: Any) -> None:
            self.config = config

        def __enter__(self) -> FakeWP:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def whoami(self) -> WordPressIdentity:
            error = behaviour.get("whoami_error")
            if error is not None:
                raise error
            return WordPressIdentity(
                id=1, slug="bot", roles=list(behaviour.get("roles", ["editor"]))
            )

        def detect_multilingual_plugin(self) -> str:
            return str(behaviour.get("detected", "none"))

        def verify_url(self, url: str) -> bool:
            return bool(behaviour.get("serves", True))

    monkeypatch.setattr("lib.preflight.WordPressClient", FakeWP)


def test_wordpress_401_names_the_six_group_password_trap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commonest cause by a wide margin is a password that lost its quotes in .env.

    The symptom is a 401 with a password the operator knows is correct, so the remedy has to
    say what actually happened rather than "check your credentials".
    """
    _patch_wp(monkeypatch, whoami_error=WordPressAPIError(401, "Unauthorized"))

    result = check_wordpress(_make_config())

    assert result.status is Status.FAIL
    assert "six" in result.remedy or "6" in result.remedy
    assert "truncate" in result.remedy


def test_wordpress_credential_that_cannot_publish_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A demoted bot passes a naive users/me check and then fails mid-run, after rows are live."""
    _patch_wp(monkeypatch, roles=["subscriber"])

    result = check_wordpress(_make_config())

    assert result.status is Status.FAIL
    assert "none of which can publish" in result.detail


def test_wordpress_missing_secret_is_caught_before_any_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wp(monkeypatch, whoami_error=MissingCredentialError("WP_PASS is not set"))
    result = check_wordpress(_make_config())
    assert result.status is Status.FAIL
    assert "WP_PASS" in result.remedy


def test_wordpress_plugin_mismatch_warns_and_says_which_way_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured value wins, so the run proceeds — but one of the two is wrong."""
    _patch_wp(monkeypatch, detected="none")
    cfg = _make_config(
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl"],
            multilingual_plugin="wpml",
        )
    )

    result = check_wordpress(cfg)

    assert result.status is Status.WARN
    assert "translations will not be linked" in result.remedy


# --- GS1 ----------------------------------------------------------------------


def _patch_gs1(monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> None:
    class FakeGS1:
        def __init__(self, config: object, **_: Any) -> None:
            pass

        def __enter__(self) -> FakeGS1:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, gtin: str) -> object | None:
            error = behaviour.get("error")
            if error is not None:
                raise error
            return behaviour.get("record")

    monkeypatch.setattr("lib.preflight.GS1DigitalLinkClient", FakeGS1)


def test_gs1_no_record_is_ok_but_refuses_to_claim_the_contract_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most expensive possible false pass, so it is stated rather than glossed.

    A GTIN the resolver has never seen answers with the same ``400 "No valid contract found
    for Gtin with id: …"`` that the 21011 blocker produces, and the client maps that to
    ``None``. Reporting "contract present" here would hand the operator a green preflight on
    an account where every write fails.
    """
    _patch_gs1(monkeypatch, record=None)

    result = check_gs1(_make_config(), [_product()])

    assert result.status is Status.OK
    assert "cannot distinguish" in result.detail
    assert result.data["registered"] is False


def test_gs1_existing_record_does_prove_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gs1(monkeypatch, record=object())
    result = check_gs1(_make_config(), [_product()])
    assert result.status is Status.OK
    assert "contract is live" in result.detail


def test_gs1_21011_names_the_blocker_and_says_it_is_not_fixable_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gs1(
        monkeypatch,
        error=GS1APIError(
            400,
            "No valid contract found.",
            [{"identifier": GTIN_A, "errors": [{"code": "21011", "message": "No valid contract"}]}],
        ),
    )

    result = check_gs1(_make_config(), [_product()])

    assert result.status is Status.FAIL
    assert result.data["no_contract"] is True
    assert "cannot be fixed in code or config" in result.remedy


def test_gs1_will_not_invent_a_gtin_to_probe_with(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GS1 record can never be deleted, so a typo'd probe GTIN would be permanent."""
    _patch_gs1(monkeypatch, record=None)

    result = check_gs1(_make_config(), [])

    assert result.status is Status.WARN
    assert "will not invent" in result.remedy


# --- Orchestration ------------------------------------------------------------


def test_a_preflight_never_quarantines_a_corrupt_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant that makes this safe to run at any time.

    ``load_state`` recovers from a corrupt file by *moving it aside* (E19) and starting fresh.
    A diagnostic that triggered that would change what the next run does simply by looking.
    """
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "clients.yml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    state = tmp_path / "output" / "acme" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{ truncated", encoding="utf-8")

    run_checks("acme", config_path=config, offline=True)

    assert state.read_text(encoding="utf-8") == "{ truncated"
    assert not list(state.parent.glob("state.json.corrupt.*"))


def test_offline_stops_before_any_credential_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "clients.yml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")

    results = run_checks("acme", config_path=config, offline=True)

    assert {"wordpress", "gs1", "site_serves"}.isdisjoint({r.name for r in results})


def test_a_broken_config_is_the_whole_report_not_a_page_of_cascade(tmp_path: Path) -> None:
    config = tmp_path / "clients.yml"
    config.write_text("clients: {}\n", encoding="utf-8")

    results = run_checks("acme", config_path=config, offline=True)

    assert [r.name for r in results] in (["config"], ["config", "client"])
    assert results[-1].status is Status.FAIL


def test_worst_status_picks_the_most_serious() -> None:
    from lib.preflight import CheckResult  # noqa: PLC0415 — only this test needs the class

    results = [
        CheckResult("a", "A", Status.OK, ""),
        CheckResult("b", "B", Status.WARN, ""),
        CheckResult("c", "C", Status.FAIL, ""),
    ]
    assert worst_status(results) is Status.FAIL
    assert worst_status(results[:2]) is Status.WARN


_MINIMAL_CONFIG = json.dumps(
    {
        "clients": {
            "acme": {
                "display_name": "Acme BV",
                "gs1": {
                    "account_number_test": "8712345000010",
                    "client_id_env_test": "GS1_CID",
                    "client_secret_env_test": "GS1_SEC",
                    "environment": "test",
                },
                "export": {"path": "input/acme.xlsx"},
                "wordpress": {
                    "site_url": "https://wp.test",
                    "username": "bot",
                    "app_password_env": "WP_PASS",
                    "post_type": "product",
                    "default_language": "nl",
                    "languages": ["nl"],
                },
            }
        }
    }
)


def test_a_links_only_batch_marks_the_page_only_checks_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A links run writes no page: its copy, categories, videos and ffmpeg are not its business."""
    from lib.gates import Mode  # noqa: PLC0415
    from lib.preflight import PAGE_ONLY_CHECKS  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    config = tmp_path / "clients.yml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")

    every = run_checks("acme", config_path=config, offline=True)
    links = run_checks("acme", config_path=config, offline=True, mode=Mode.LINKS)

    assert [r.name for r in links] == [r.name for r in every], "same checks, same order"
    for before, after in zip(every, links, strict=True):
        if after.name in PAGE_ONLY_CHECKS:
            assert after.status is Status.NA, after.name
            assert "links only" in after.detail
        else:
            assert after == before, after.name


@pytest.mark.parametrize("mode", ["pages", "both"])
def test_a_batch_that_writes_pages_is_checked_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from lib.gates import Mode  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    config = tmp_path / "clients.yml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")

    assert run_checks("acme", config_path=config, offline=True, mode=Mode(mode)) == run_checks(
        "acme", config_path=config, offline=True
    )


# --- where the prompt and templates come from ---------------------------------------------------


def _client_file(root: Path, *parts: str) -> None:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_client_files_ok_when_the_data_folder_has_them(tmp_path: Path) -> None:
    code, data = tmp_path / "code", tmp_path / "data"
    _client_file(code, "prompts", "acme", "generation.v1.md")
    _client_file(data, "input", "acme", "prompts", "generation.v1.md")
    _client_file(data, "input", "acme", "templates", "product.nl.html")

    result = check_client_files(_make_config(), data_root=data, code_root=code)

    assert result.status is Status.OK
    assert "data folder" in result.detail


def test_client_files_warn_when_only_the_release_has_one(tmp_path: Path) -> None:
    """It works, but it is not in a handover pack and the next release may not ship it."""
    code, data = tmp_path / "code", tmp_path / "data"
    data.mkdir()
    _client_file(code, "prompts", "acme", "generation.v1.md")
    _client_file(data, "input", "acme", "templates", "product.nl.html")

    result = check_client_files(_make_config(), data_root=data, code_root=code)

    assert result.status is Status.WARN
    assert "program folder" in result.detail
    assert result.remedy and "installer" in result.remedy


def test_client_files_na_without_any_of_the_clients_own(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    result = check_client_files(_make_config(), data_root=tmp_path / "data", code_root=tmp_path)
    assert result.status is Status.NA


def test_client_files_na_in_a_development_clone(tmp_path: Path) -> None:
    _client_file(tmp_path, "prompts", "acme", "generation.v1.md")
    result = check_client_files(_make_config(), data_root=tmp_path, code_root=tmp_path)
    assert result.status is Status.NA
    assert "development clone" in result.detail


def test_client_files_in_a_clone_match_the_committed_copy(tmp_path: Path) -> None:
    """A pack received into a clone brought an identical copy: nothing to say."""
    _client_file(tmp_path, "prompts", "acme", "generation.v1.md")
    _client_file(tmp_path, "input", "acme", "prompts", "generation.v1.md")
    result = check_client_files(_make_config(), data_root=tmp_path, code_root=tmp_path)
    assert result.status is Status.NA


def test_client_files_in_a_clone_warn_when_the_packed_copy_differs(tmp_path: Path) -> None:
    """The gitignored copy wins, so an edit to the committed prompt would not be what runs."""
    _client_file(tmp_path, "prompts", "acme", "generation.v1.md")
    received = tmp_path / "input" / "acme" / "prompts" / "generation.v1.md"
    _client_file(tmp_path, "input", "acme", "prompts", "generation.v1.md")
    received.write_text("an older voice", encoding="utf-8")
    _client_file(tmp_path, "input", "acme", "templates", "product.fr.html")

    result = check_client_files(_make_config(), data_root=tmp_path, code_root=tmp_path)

    assert result.status is Status.WARN
    assert "input/acme/prompts/generation.v1.md" in result.detail
    assert "input/acme/templates/product.fr.html" in result.detail
