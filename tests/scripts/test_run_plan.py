"""Tests for scripts/run_plan.py (IMPLEMENTATION_SPEC §8.2, §12 Phase 7).

run_plan is pure orchestration over ``lib.state.diff_against_state`` and the
process-list gate, so these tests drive ``main`` with a fake ``get_client`` and a
temp working directory and assert the emitted ``plan.json``, the stderr summary, the
gate filtering, and the exit codes. Classification logic itself is covered in
``tests/lib/test_state.py``; process-list parsing in ``tests/lib/test_process_list.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml

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
from lib.errors import ConfigError
from lib.gdsn import GdsnSource
from lib.generator import (
    GenerationContext,
    GenerationResult,
    ResultsFile,
    generation_context,
    pending_requests,
    result_item,
    save_results,
)
from lib.link_set import build_links, link_set_hash
from lib.records import (
    ConfirmedPlan,
    LocalisedText,
    Plan,
    PlanClassification,
    PlanSummary,
    ProductRecord,
    SkipReason,
    State,
    StateEntry,
)
from lib.state import diff_against_state, load_state, save_state
from scripts import run_plan

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"
GTIN_C = "02987653007361"
GTIN_D = "02987653007362"

_LIST_HEADER = ["Barcode", "Omschrijving"]  # extra columns are ignored by design


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


def _product(
    gtin: str = GTIN_A,
    brick: str | None = None,
    product_name: dict[str, str] | None = None,
) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Acme",
        product_name=LocalisedText(values=product_name or {"nl": "Rugsteun"}),
        gpc_brick_code=brick,
    )


def _write_products(path: Path, products: list[ProductRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([p.model_dump(mode="json") for p in products])
    path.write_text(payload, encoding="utf-8")


def _write_process_list(tmp_path: Path, rows: list[list[Any]]) -> str:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(_LIST_HEADER)
    for row in rows:
        sheet.append(row)
    path = tmp_path / "process-list.xlsx"
    workbook.save(path)
    return str(path)


def _patch_client(monkeypatch: pytest.MonkeyPatch, cfg: ClientConfig) -> None:
    monkeypatch.setattr(run_plan, "get_client", lambda _cid: cfg)


def _link_written(*products: ProductRecord) -> None:
    """Give each seeded entry the link-set hash a real run would have recorded for it.

    A placeholder like ``"g" * 64`` is a link nobody could have written, so the plan now reads it
    as a GS1 record out of date with its page (``lib.link_set.link_is_stale``) — correctly.
    """
    cfg = run_plan.get_client(None)
    state = load_state("acme")
    for product in products:
        entries = state.entries[product.gtin]
        pages = {
            lang: type("Page", (), {"url": e.wp_url, "title": e.title or ""})()
            for lang, e in entries.items()
        }
        real = link_set_hash(build_links(cfg, product, pages))
        state.entries[product.gtin] = {
            lang: e.model_copy(update={"gs1_link_set_hash": real}) for lang, e in entries.items()
        }
    save_state(state)


def _read_plan() -> Plan:
    return Plan.model_validate(
        json.loads(Path("output/acme/plan.json").read_text(encoding="utf-8"))
    )


def _read_summary() -> PlanSummary:
    return PlanSummary.model_validate(
        json.loads(Path("output/acme/plan.summary.json").read_text(encoding="utf-8"))
    )


# --- Happy path & summary ----------------------------------------------------


def test_writes_plan_with_counts_and_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A), _product(GTIN_B)])

    code = run_plan.main(["acme", "--products", str(products)])

    assert code == 0
    plan = _read_plan()
    assert plan.total == 2
    assert plan.counts[PlanClassification.NEW] == 2
    assert {r.classification for r in plan.rows} == {PlanClassification.NEW}


def test_summary_line_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    assert "1 new, 0 unchanged, 0 changed" in capsys.readouterr().err


def test_default_products_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products(Path("output/acme/data/products.json"), [_product(GTIN_A)])

    assert run_plan.main(["acme"]) == 0
    assert _read_plan().total == 1


# --- Pilot gate (§9.5) -------------------------------------------------------


def _bilingual_config(media: MediaConfig) -> ClientConfig:
    return _make_config(
        media=media,
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl", "fr"],
            slug_pattern="p-{gtin}",
            target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
        ),
    )


def _write_video_map(tmp_path: Path, both: list[str], nl_only: list[str] | None = None) -> str:
    entries = {
        "nl": [{"file": f"{g}_nl.mp4", "gtin": g} for g in [*both, *(nl_only or [])]],
        "fr": [{"file": f"{g}_fr.mp4", "gtin": g} for g in both],
    }
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return str(path)


def _present_state(
    *gtins: str,
    content_hash: str = "h",
    video_file: str | None = None,
    video_failed: str | None = None,
) -> None:
    entry = StateEntry(
        wp_page_id=1,
        wp_url="https://wp.test/x",
        wp_featured_media_id=None,
        content_hash=content_hash,
        gs1_link_set_hash="h",
        last_run=datetime(2026, 1, 1, tzinfo=UTC),
        video_file=video_file,
        video_failed=video_failed,
    )
    save_state(State(client_id="acme", entries={g: {"nl": entry} for g in gtins}))


def _retracted_state(*gtins: str) -> None:
    """State as ``run_unpublish`` leaves it: pages drafted, retracted, no link-set hash."""
    entry = StateEntry(
        wp_page_id=1,
        wp_url="https://wp.test/x",
        wp_featured_media_id=None,
        content_hash="h",
        gs1_link_set_hash="",
        last_run=datetime(2026, 1, 1, tzinfo=UTC),
        wp_status="draft",
        retracted=True,
    )
    save_state(State(client_id="acme", entries={g: {"nl": entry} for g in gtins}))


def test_pilot_gate_restricts_to_mapped_and_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A, GTIN_C], nl_only=[GTIN_B])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_C)  # C is fully mapped but already has a page
    products = tmp_path / "products.json"
    _write_products(products, [_product(g) for g in (GTIN_A, GTIN_B, GTIN_C)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    # Only A gets rows: C is already present (excluded before classification), and B lacks an fr
    # video — which is now a *hold* rather than a silent drop, so it appears in `skipped`.
    plan = _read_plan()
    assert {r.gtin for r in plan.rows} == {GTIN_A}
    held = {s.gtin for s in plan.skipped if s.reason is SkipReason.NO_CONFIRMED_VIDEO}
    assert held == {GTIN_B}
    # Held in *every* language, so the SKU is never half-published.
    assert {s.language for s in plan.skipped if s.gtin == GTIN_B} == {"nl", "fr"}
    err = capsys.readouterr().err
    assert "1 pilot-excluded (already have a page)" in err
    assert "no_confirmed_video" in err  # the hold is tallied, not silently dropped


def _pages_only_state(*gtins: str) -> None:
    """State as ``run_execute --only pages`` leaves it: page live, resolver link never written."""
    entry = StateEntry(
        wp_page_id=1,
        wp_url="https://wp.test/x",
        wp_featured_media_id=None,
        content_hash="h",
        gs1_link_set_hash="",  # the "--only pages" marker
        last_run=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_state(State(client_id="acme", entries={g: {"nl": entry} for g in gtins}))


def test_pilot_gate_keeps_gtin_whose_resolver_link_was_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--only pages`` GTIN stays in the plan so ``--only links`` can finish it.

    Regression: the gate treated *any* state entry as finished, so a pages-only run dropped its
    own GTIN from every later plan — ``_classify`` never ran, the CHANGED (``gs1_link``) path
    could not fire, and the ``/gs1-pages`` → ``/gs1-links`` handoff dead-ended with an empty plan.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _pages_only_state(GTIN_A)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    rows = _read_plan().rows
    assert {r.gtin for r in rows} == {GTIN_A}, "pages-only GTIN must remain plannable"
    assert PlanClassification.CHANGED in {r.classification for r in rows}


def test_pilot_gate_drops_gtin_once_resolver_link_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: once the link set is written the GTIN leaves the queue as before."""
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_A)  # non-empty gs1_link_set_hash
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert _read_plan().rows == []


def test_publish_without_video_plans_the_gtin_missing_a_french_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A], nl_only=[GTIN_B])
    media = MediaConfig(
        restrict_to_mapped_gtins=True, publish_without_video=True, video_map_path=vmap
    )
    _patch_client(monkeypatch, _bilingual_config(media))
    products = tmp_path / "products.json"
    _write_products(products, [_product(g) for g in (GTIN_A, GTIN_B)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    plan = _read_plan()
    assert {r.gtin for r in plan.rows} == {GTIN_A, GTIN_B}
    assert not [s for s in plan.skipped if s.reason is SkipReason.NO_CONFIRMED_VIDEO]


def test_a_live_gtin_whose_video_has_since_arrived_comes_back_into_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropped as "already present", it would never reach the classification that sees the video.

    It went live with no nl video (``video_file=""``); the client has since confirmed one. Without
    the arrival check the pilot gate removes it before classification and the page serves no video
    on every later run, while each run reports nothing to do.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    media = MediaConfig(
        restrict_to_mapped_gtins=True, publish_without_video=True, video_map_path=vmap
    )
    _patch_client(monkeypatch, _bilingual_config(media))
    _present_state(GTIN_A, video_file="")
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    rows = {r.language: r for r in _read_plan().rows if r.gtin == GTIN_A}
    assert rows["nl"].classification is PlanClassification.CHANGED
    assert rows["nl"].diff is not None
    assert rows["nl"].diff["video"] == ("none", f"{GTIN_A}_nl.mp4")


def test_a_video_that_failed_comes_back_once_its_file_is_in_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26: 15 files were copied to the wrong folder; the run published 22 pages bare.

    Moving the files into place changed nothing — every row planned UNCHANGED and the shell had
    nothing to confirm. A failure recorded before the reason was (``video_failed_retry`` unset) is
    retried, but only once the file is where the run reads it.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    folder = tmp_path / "videos" / "NL"
    media = MediaConfig(
        restrict_to_mapped_gtins=True,
        publish_without_video=True,
        video_map_path=vmap,
        video_folders={"nl": str(folder)},
    )
    _patch_client(monkeypatch, _bilingual_config(media))
    clip = f"{GTIN_A}_nl.mp4"
    _present_state(GTIN_A, video_file="", video_failed=clip)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert _read_plan().rows == []  # still not in its folder: nothing to retry yet

    folder.mkdir(parents=True)
    (folder / clip).write_bytes(b"video")
    assert run_plan.main(["acme", "--products", str(products)]) == 0

    rows = {r.language: r for r in _read_plan().rows if r.gtin == GTIN_A}
    assert rows["nl"].classification is PlanClassification.CHANGED
    assert rows["nl"].diff is not None
    assert rows["nl"].diff["video"] == ("none", clip)


def test_a_live_gtin_with_its_video_recorded_stays_out_of_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    media = MediaConfig(
        restrict_to_mapped_gtins=True, publish_without_video=True, video_map_path=vmap
    )
    _patch_client(monkeypatch, _bilingual_config(media))
    _present_state(GTIN_A, video_file=f"{GTIN_A}_nl.mp4")
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    assert _read_plan().rows == []


def test_include_published_replans_a_finished_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The counterpart to the drop: source data can change after a product goes live.

    Without the flag the gate removes the GTIN before classification, so the plan is empty and a
    run reports success having written nothing — indistinguishable from "nothing to do". With it,
    the GTIN reaches ``diff_against_state`` and its moved content hash classifies it CHANGED.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_A)  # published and resolvable — "finished"
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products), "--include-published"]) == 0

    rows = _read_plan().rows
    assert {r.gtin for r in rows} == {GTIN_A}
    assert {r.classification for r in rows} == {PlanClassification.CHANGED}


def test_include_published_is_not_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-planning live pages must be chosen, never inherited from a bare invocation."""
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_A)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert _read_plan().rows == []
    assert _read_summary().included_published is False


def test_include_published_says_so_on_stderr_and_in_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CHANGED row means something different here, so the flag may not scroll away silently.

    It rides in ``PlanSummary`` as well as on stderr for the reason the E19 reset does: an
    operator reading ``plan.summary.json`` an hour later has no other way to tell a first publish
    from a rewrite of a live page.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_A)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products), "--include-published"]) == 0

    err = capsys.readouterr().err
    assert "--include-published" in err
    assert "rewrites a LIVE page" in err
    summary = _read_summary()
    assert summary.included_published is True
    # The stderr line is carried verbatim, so a second reader shows the same words.
    assert "rewrites a LIVE page" in summary.text


def test_include_published_widens_what_is_considered_not_what_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untouched live product still classifies UNCHANGED, and UNCHANGED is never executed.

    This is what makes the flag safe to reach for: it re-admits finished GTINs to *classification*
    and lets the content hash decide, rather than forcing a rewrite of everything already live.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    product = _product(GTIN_A)
    _write_products(products, [product])

    # Seed state whose content hash matches what this product hashes to right now.
    assert run_plan.main(["acme", "--products", str(products), "--include-published"]) == 0
    unchanged_hash = _read_plan().rows[0].content_hash
    _present_state(GTIN_A, content_hash=unchanged_hash)
    _link_written(product)

    assert run_plan.main(["acme", "--products", str(products), "--include-published"]) == 0
    rows = _read_plan().rows
    assert rows, "the GTIN must be considered, not dropped"
    assert {r.classification for r in rows} == {PlanClassification.UNCHANGED}


def test_pilot_gate_returns_a_retracted_gtin_to_the_queue_as_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A knock-on of ``run_unpublish`` blanking the link-set hash, and the better outcome.

    The gate calls a GTIN finished when every language carries a link-set hash. A retracted
    one no longer does, so instead of disappearing into the ``already_present`` tally it
    reaches ``_classify`` and reports **HELD** — which is the thing the operator is actually
    looking at when they read the plan-review gate. ``run_execute`` still drops it without
    ``--revive``, so nothing about what runs has changed.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _retracted_state(GTIN_A)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    rows = _read_plan().rows
    assert {r.classification for r in rows} == {PlanClassification.HELD}


def test_pilot_gate_noop_when_flag_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=False, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A), _product(GTIN_B)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert {r.gtin for r in _read_plan().rows} == {GTIN_A, GTIN_B}  # unrestricted


def test_an_unrestricted_client_with_a_mapping_does_not_drop_live_gtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate it now gets for arrivals is unenforced; "already present" is a pilot-only drop."""
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(MediaConfig(restrict_to_mapped_gtins=False, video_map_path=vmap))
    _patch_client(monkeypatch, cfg)
    _present_state(GTIN_A)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert GTIN_A in {r.gtin for r in _read_plan().rows}


# --- Process-list gate --------------------------------------------------------


def test_gate_keeps_every_listed_gtin_and_excludes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Membership is the whole rule: listed -> planned, absent -> excluded."""
    # Arrange: A and B listed, C and D not.
    list_path = _write_process_list(tmp_path, [[GTIN_A, "Widget"], [GTIN_B, "Gadget"]])
    cfg = _make_config(process_list=ProcessListConfig(path=list_path))
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(g) for g in (GTIN_A, GTIN_B, GTIN_C, GTIN_D)])

    # Act
    code = run_plan.main(["acme", "--products", str(products)])

    # Assert
    assert code == 0
    plan = _read_plan()
    assert sorted({r.gtin for r in plan.rows}) == sorted([GTIN_A, GTIN_B])
    err = capsys.readouterr().err
    assert "2 excluded (not on the process list)" in err


def test_gate_joins_13_digit_barcode_to_14_digit_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control file carries the 13-digit barcode; the product GTIN is 14-digit.
    list_path = _write_process_list(tmp_path, [[GTIN_A.lstrip("0"), "Widget"]])
    cfg = _make_config(process_list=ProcessListConfig(path=list_path))
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    plan = _read_plan()
    assert [r.gtin for r in plan.rows] == [GTIN_A]


def test_missing_control_file_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(process_list=ProcessListConfig(path=str(tmp_path / "missing.xlsx")))
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 2
    assert "config error" in capsys.readouterr().err


# --- Classification against seeded state --------------------------------------


def test_unchanged_and_changed_from_seeded_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A), _product(GTIN_B)])

    # First plan (empty state) yields NEW rows; use their hashes/URLs to seed state so
    # A re-plans UNCHANGED (matching hash) and B re-plans CHANGED (stale hash).
    run_plan.main(["acme", "--products", str(products)])
    first = _read_plan()
    row_a = next(r for r in first.rows if r.gtin == GTIN_A)
    row_b = next(r for r in first.rows if r.gtin == GTIN_B)
    save_state(
        State(
            client_id="acme",
            entries={
                GTIN_A: {"nl": _entry(row_a.content_hash, row_a.target_url)},
                GTIN_B: {"nl": _entry("stale-hash", row_b.target_url)},
            },
        )
    )
    _link_written(row_a.product, row_b.product)

    run_plan.main(["acme", "--products", str(products)])
    plan = _read_plan()

    classifications = {r.gtin: r.classification for r in plan.rows}
    assert classifications[GTIN_A] is PlanClassification.UNCHANGED
    assert classifications[GTIN_B] is PlanClassification.CHANGED


def _entry(content_hash: str, wp_url: str) -> StateEntry:
    return StateEntry(
        wp_page_id=1,
        wp_url=wp_url,
        wp_featured_media_id=None,
        content_hash=content_hash,
        gs1_link_set_hash="g" * 64,
        last_run=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_corrupt_state_warns_replans_as_new_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """E19: a corrupt state file does not abort the plan — but the reset must be loud.

    Without the warning the operator sees only "1 new" where they expected "1 unchanged",
    with nothing to explain it, and confirming would rewrite live pages and resolver targets.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    state_file = tmp_path / "output" / "acme" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{ truncated", encoding="utf-8")

    code = run_plan.main(["acme", "--products", str(products)])

    assert code == 0
    assert _read_plan().counts[PlanClassification.NEW] == 1
    err = capsys.readouterr().err
    assert "prior state was corrupt and has been reset" in err
    assert "rewrite live pages and resolver targets" in err
    assert list(state_file.parent.glob("state.json.corrupt.*"))  # bad file preserved


def test_healthy_state_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    assert "corrupt" not in capsys.readouterr().err


# --- Category assignment (Phase 7.5) -----------------------------------------


def _categories(**kwargs: Any) -> CategoryConfig:
    kwargs.setdefault("terms", ["tuin", "keuken"])
    return CategoryConfig(**kwargs)


def test_assigns_category_from_brick_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(categories=_categories(brick_category_map={"10003865": "tuin"}))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, brick="10003865")])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert _read_plan().rows[0].product.category == "tuin"
    # The report is written even when it found nothing.
    issues = json.loads(Path("output/acme/data/category_issues.json").read_text(encoding="utf-8"))
    assert issues == []


def test_override_wins_in_run_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        categories=_categories(
            brick_category_map={"10003865": "tuin"}, overrides={GTIN_A: "keuken"}
        )
    )
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, brick="10003865")])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert _read_plan().rows[0].product.category == "keuken"


def test_category_change_reclassifies_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The category is part of the content hash: planning it onto a product that had none
    # must reclassify the row as CHANGED, not UNCHANGED.
    monkeypatch.chdir(tmp_path)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, brick="10003865")])

    _patch_client(monkeypatch, _make_config())  # no categories -> baseline hash, category None
    run_plan.main(["acme", "--products", str(products)])
    baseline = _read_plan().rows[0]
    assert baseline.product.category is None
    save_state(
        State(
            client_id="acme",
            entries={GTIN_A: {"nl": _entry(baseline.content_hash, baseline.target_url)}},
        )
    )

    cfg = _make_config(categories=_categories(brick_category_map={"10003865": "tuin"}))
    _patch_client(monkeypatch, cfg)
    run_plan.main(["acme", "--products", str(products)])
    row = _read_plan().rows[0]

    assert row.product.category == "tuin"
    assert row.classification is PlanClassification.CHANGED


def test_unmapped_brick_warns_and_leaves_category_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(categories=_categories(brick_category_map={"10003865": "tuin"}))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, brick="99999999")])

    code = run_plan.main(["acme", "--products", str(products)])

    assert code == 0
    row = _read_plan().rows[0]
    assert row.product.category is None  # never guessed
    assert "1 product(s) with unmapped category (left unset)" in capsys.readouterr().err
    issues = json.loads(Path("output/acme/data/category_issues.json").read_text(encoding="utf-8"))
    assert len(issues) == 1
    assert issues[0]["issue"] == "category_unmapped"


def test_no_category_report_without_categories_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, brick="10003865")])

    assert run_plan.main(["acme", "--products", str(products)]) == 0
    assert not Path("output/acme/data/category_issues.json").exists()


# --- Error paths (all exit 2) ------------------------------------------------


def test_missing_products_file_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())

    assert run_plan.main(["acme", "--products", str(tmp_path / "nope.json")]) == 2
    assert "config error" in capsys.readouterr().err


def test_invalid_products_json_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    products.write_text("{ not json", encoding="utf-8")

    assert run_plan.main(["acme", "--products", str(products)]) == 2


def test_unknown_client_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def _raise(_cid: str) -> ClientConfig:
        raise ConfigError("unknown client_id 'nope'")

    monkeypatch.setattr(run_plan, "get_client", _raise)

    assert run_plan.main(["nope"]) == 2


def test_missing_target_url_pattern_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wordpress = WordPressConfig(
        site_url="https://wp.test",
        username="bot",
        app_password_env="WP_PASS",
        default_language="nl",
        languages=["nl"],
        slug_pattern="p-{gtin}",
        # target_url_pattern intentionally unset
    )
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(wordpress=wordpress))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 2
    assert "target_url_pattern" in capsys.readouterr().err


# --- Generated-content merge (generator SPEC, commit 7) ----------------------

_GEN_NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _bilingual_wp() -> WordPressConfig:
    return WordPressConfig(
        site_url="https://wp.test",
        username="bot",
        app_password_env="WP_PASS",
        post_type="product",
        default_language="nl",
        languages=["nl", "fr"],
        slug_pattern="p-{gtin}",
        target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
    )


#: An export whose title attribute is opted into language-gap filling. `run_plan` derives what
#: may be filled from `gdsn_map`/`gdsn_extras`, so a config without this fills nothing — which is
#: the behaviour `test_e18_without_the_field_opted_in_still_skips_french` locks down.
def _translating_export() -> ExportConfig:
    return ExportConfig(
        path="input/acme.xlsx",
        gdsn_map={
            "product_name": GdsnSource(
                sheet="TradeItemDescription", attribute="3301", localised=True, translate=True
            )
        },
    )


def _ctx(language: str) -> GenerationContext:
    """The context `run_plan` will build for `_translating_export`, for writing results against."""
    return generation_context(
        [language], "nl", "v1", _translating_export().gdsn_map, _translating_export().gdsn_extras
    )


def _copy_with(gtin: str, language: str, **result: Any) -> None:
    """Write a generation_results.json holding this run's copy for (gtin, language)."""
    _copy_multi(gtin, {language: result})


def _copy_multi(gtin: str, entries: dict[str, dict[str, Any]]) -> None:
    """Write this run's results, one item per language (lang -> GenerationResult kwargs)."""
    items = []
    for language, result in entries.items():
        request = pending_requests([_product(gtin)], _ctx(language))[0]
        items.append(result_item(request, GenerationResult(**result)))
    save_results(ResultsFile(client_id="acme", results=items))


def _plan_and_publish(products: Path) -> None:
    """Plan once and record the resulting row as the live page, so the next run compares to it."""
    run_plan.main(["acme", "--products", str(products)])
    row = next(r for r in _read_plan().rows if r.gtin == GTIN_A)
    save_state(
        State(client_id="acme", entries={GTIN_A: {"nl": _entry(row.content_hash, row.target_url)}})
    )
    _link_written(row.product)


def test_regenerated_copy_alone_does_not_reclassify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-wording the same source data must leave a published page alone.

    The generated copy is on the record and does reach the page, but it is not a change signal:
    a producer asked twice answers differently both times, so a hash that covered it would
    rewrite the whole live site on every run having changed nothing.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    _copy_with(GTIN_A, "nl", usps=["Tagline", "Bullet"])
    _plan_and_publish(products)

    _copy_with(GTIN_A, "nl", usps=["A sharper tagline", "Bullet"])
    run_plan.main(["acme", "--products", str(products)])

    row = next(r for r in _read_plan().rows if r.gtin == GTIN_A)
    assert row.classification is PlanClassification.UNCHANGED
    # The new copy is still on the row — only the classification ignores it.
    assert row.product.generated_tagline is not None
    assert row.product.generated_tagline.values["nl"] == "A sharper tagline"


def test_planning_twice_over_one_results_file_gives_the_same_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_plan` reads the run's copy and never consumes it, so re-planning is free.

    Delete-after-read was a considered design for the results file and was rejected for exactly
    this: re-running `run_plan` is a normal thing to do — after pruning the process list, after a
    gate sends the operator back — and it must not force regenerating all 74 units.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    _copy_with(GTIN_A, "nl", usps=["Tagline", "Bullet"])
    results = Path("output/acme/data/generation_results.json")
    before = results.read_text(encoding="utf-8")

    run_plan.main(["acme", "--products", str(products)])
    first = _read_plan()
    run_plan.main(["acme", "--products", str(products)])
    second = _read_plan()

    assert results.read_text(encoding="utf-8") == before  # untouched
    assert [r.content_hash for r in first.rows] == [r.content_hash for r in second.rows]
    assert first.rows[0].product.generated_tagline == second.rows[0].product.generated_tagline


def test_copy_written_for_older_inputs_is_dropped_and_the_unit_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The validity check, end to end: stale copy must not reach a page, and must not go quiet.

    A results file outlives the producer session that wrote it. Without this, a `parse_export`
    re-run between the two publishes copy describing data the feed no longer holds — the failure
    the fingerprint is kept for, now that it is no longer a reuse key.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    _copy_with(GTIN_A, "nl", usps=["Tagline", "Bullet"])

    # The feed changes after the copy was written — a different name is a different fingerprint.
    _write_products(products, [_product(GTIN_A, product_name={"nl": "Andere naam"})])
    with caplog.at_level("WARNING"):
        run_plan.main(["acme", "--products", str(products)])

    plan = _read_plan()
    assert plan.rows == []
    assert [s.reason for s in plan.skipped] == [SkipReason.NO_GENERATED_COPY]
    # Held because the copy was *rejected*, not because none was written — those are different
    # failures and only one of them means "you forgot to regenerate".
    assert "stale copy" in caplog.text
    assert GTIN_A in caplog.text


def test_a_source_edit_still_reclassifies_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: excluding copy must not blind the plan to a real feed change.

    The brick is the cleanest probe — it is in the content hash but is not a generation input, so
    the copy is byte-identical across both runs and the CHANGED can only have come from the feed.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    _copy_with(GTIN_A, "nl", usps=["Tagline", "Bullet"])
    _plan_and_publish(products)

    _write_products(products, [_product(GTIN_A, brick="10000123")])
    run_plan.main(["acme", "--products", str(products)])

    row = next(r for r in _read_plan().rows if r.gtin == GTIN_A)
    assert row.classification is PlanClassification.CHANGED


def test_e18_cached_french_name_is_planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        wordpress=_bilingual_wp(),
        generator=GeneratorConfig(enabled=True),
        export=_translating_export(),
    )
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])  # nl name only
    # fr fill (name + copy) lifts E18; nl needs its own copy to survive E21.
    _copy_multi(
        GTIN_A,
        {
            "nl": {"usps": ["Slogan NL"]},
            "fr": {"usps": ["Slogan"], "translations": {"product_name": "Nom FR"}},
        },
    )

    run_plan.main(["acme", "--products", str(products)])

    planned = {(r.gtin, r.language) for r in _read_plan().rows}
    assert (GTIN_A, "fr") in planned  # E18 no longer fires — the fr name came from the cache
    assert (GTIN_A, "nl") in planned


def test_e18_without_cache_still_skips_french(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        wordpress=_bilingual_wp(),
        generator=GeneratorConfig(enabled=True),
        export=_translating_export(),
    )
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])  # nl name only, no fr name/fill
    _copy_with(GTIN_A, "nl", usps=["Slogan NL"])  # nl copy so it survives E21

    run_plan.main(["acme", "--products", str(products)])

    planned = {(r.gtin, r.language) for r in _read_plan().rows}
    assert (GTIN_A, "fr") not in planned  # E18 backstop: no fr name anywhere -> skipped
    assert (GTIN_A, "nl") in planned


def test_e18_without_the_field_opted_in_still_skips_french(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filling is a client's decision, taken in `clients.yml`, not something run_plan assumes.

    Same cache as the passing case above, but an export whose `product_name` carries no
    `translate: true`, and fr is skipped. Two guards make that so and this test does not separate
    them — the gap is never requested (`translation_gaps`) *and* the seeded fr entry fingerprints
    differently, because the opt-in changes `translation_sources`. Each is isolated in
    `tests/lib/test_generator.py`; what this asserts is the end-to-end consequence.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(wordpress=_bilingual_wp(), generator=GeneratorConfig(enabled=True))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    _copy_multi(
        GTIN_A,
        {
            "nl": {"usps": ["Slogan NL"]},
            "fr": {"usps": ["Slogan"], "translations": {"product_name": "Nom FR"}},
        },
    )

    run_plan.main(["acme", "--products", str(products)])

    planned = {(r.gtin, r.language) for r in _read_plan().rows}
    assert (GTIN_A, "fr") not in planned
    # nl is planned, so the cache is readable and the run reached classification — the fr row is
    # missing because of this config, not because the whole merge fell over.
    assert (GTIN_A, "nl") in planned


# --- Plan-time drops (E18/E21/E22) -------------------------------------------


def test_dropped_units_are_written_into_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit dropped before classification is named in ``plan.json``, not just logged.

    ``plan.total`` is ``len(rows)``, so a drop used to make the plan under-report the work
    by exactly the units that had gone missing — and the only trace was a WARNING line.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(wordpress=_bilingual_wp(), generator=GeneratorConfig(enabled=True))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    _copy_with(GTIN_A, "nl", usps=["Slogan NL"])  # nl has copy; fr has neither name nor copy

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    plan = _read_plan()
    assert plan.total == 1  # unchanged meaning: total still counts executable rows only
    assert [(s.gtin, s.language, s.reason) for s in plan.skipped] == [
        (GTIN_A, "fr", SkipReason.MISSING_PRODUCT_NAME)
    ]


def test_summary_names_the_reason_not_just_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "2 skipped" is a number to shrug at; naming the reason is an instruction."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(wordpress=_bilingual_wp(), generator=GeneratorConfig(enabled=True))
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A, product_name={"nl": "Rugsteun", "fr": "Support"})])

    run_plan.main(["acme", "--products", str(products)])  # generator on, cache empty -> E21 ×2

    err = capsys.readouterr().err
    assert "0 new, 0 unchanged, 0 changed" in err  # an empty plan...
    assert "2 skipped (2 no_generated_copy)" in err  # ...and why, on the same line


# --- The machine-readable summary ---------------------------------------------


def test_summary_file_carries_the_stderr_line_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One wording, two readers. A paraphrase in a second place is a paraphrase that drifts."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    assert _read_summary().text == capsys.readouterr().err.strip()


def test_summary_file_is_written_even_when_there_is_nothing_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing file must mean "run_plan did not run", so an empty tally can mean "found none"."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    summary = _read_summary()
    assert summary.skipped == {} and summary.excluded == {}
    assert summary.state_reset_from_corrupt is False
    assert summary.state_corrupt_backup is None


def test_summary_file_records_the_e19_reset_and_where_the_bad_file_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset reaches a reader who was not at the terminal — with the evidence.

    An operator told a reset happened asks where the old file went, and only ``load_state``
    knows: the name is stamped with the moment of the reset.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])
    state_file = tmp_path / "output" / "acme" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{ truncated", encoding="utf-8")

    run_plan.main(["acme", "--products", str(products)])

    summary = _read_summary()
    assert summary.state_reset_from_corrupt is True
    assert summary.state_corrupt_backup is not None
    assert Path(summary.state_corrupt_backup).read_text(encoding="utf-8") == "{ truncated"
    assert summary.text.startswith("WARNING:")  # the reset leads, above the counts


def test_summary_file_tallies_skips_and_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        wordpress=_bilingual_wp(),
        generator=GeneratorConfig(enabled=True),
        process_list=ProcessListConfig(
            path=_write_process_list(tmp_path, [[GTIN_A, "keep"]]), gtin_column="Barcode"
        ),
    )
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(
        products,
        [
            _product(GTIN_A, product_name={"nl": "Rugsteun", "fr": "Support"}),
            _product(GTIN_B),  # not on the list
        ],
    )

    run_plan.main(["acme", "--products", str(products)])

    summary = _read_summary()
    assert summary.skipped == {SkipReason.NO_GENERATED_COPY: 2}  # cache empty -> E21 ×2
    assert summary.excluded == {"not_listed": 1}
    assert summary.total == 0  # and the plan itself is empty, which alone would say nothing


def test_a_plan_written_before_skipped_existed_still_loads(tmp_path: Path) -> None:
    """``skipped`` defaults to empty, so the live ``plan.confirmed.json`` keeps validating."""
    legacy = {
        "client_id": "acme",
        "generated_at": "2026-07-12T00:00:00Z",
        "total": 0,
        "counts": {},
        "rows": [],
    }
    plan = Plan.model_validate(legacy)
    assert plan.skipped == []
    assert ConfirmedPlan.model_validate({"plan": legacy, "confirmed_gtins_by_lang": []})


def test_generated_issues_report_written_when_generator_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])  # no 1083 -> a missing_generation_input note

    run_plan.main(["acme", "--products", str(products)])

    report = Path("output/acme/data/generated_issues.json")
    assert report.exists()
    issues = json.loads(report.read_text(encoding="utf-8"))
    assert any(i["issue"] == "missing_generation_input" for i in issues)


def test_no_generated_issues_file_without_generator_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())  # no generator block
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    assert not Path("output/acme/data/generated_issues.json").exists()


# --- an already-live unit is a row, not a skip --------------------------------
#
# Copy is written per run for the rows a run executes, so an UNCHANGED unit arrives with none.
# Reporting it as `no_generated_copy` turned a correct skip into a work item — 20 of them on the
# pilot client — and lost the "Unchanged: N" count the operator reads at the plan gate.


def _publish(cfg: ClientConfig, product: ProductRecord) -> None:
    """Record ``product`` in state as live and matching, the way a successful run would."""
    rows, _ = diff_against_state(
        [product], State(client_id=cfg.client_id, entries={}), ["nl"], cfg.wordpress
    )
    row = rows[0]
    save_state(
        State(
            client_id=cfg.client_id,
            entries={
                product.gtin: {
                    "nl": StateEntry(
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
    _link_written(row.product)


def test_an_already_live_unit_with_no_copy_is_planned_unchanged_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(generator=GeneratorConfig(enabled=True))
    _patch_client(monkeypatch, cfg)
    product = _product(GTIN_A)
    _publish(cfg, product)
    products = tmp_path / "products.json"
    _write_products(products, [product])

    run_plan.main(["acme", "--products", str(products)])

    plan = _read_plan()
    assert [r.classification for r in plan.rows] == [PlanClassification.UNCHANGED]
    assert plan.skipped == []
    assert "0 new, 1 unchanged, 0 changed" in capsys.readouterr().err


def test_a_new_unit_with_no_copy_is_still_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold that keeps a blank page offline is untouched for the rows a run would write."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    run_plan.main(["acme", "--products", str(products)])

    plan = _read_plan()
    assert plan.rows == []
    assert [s.reason for s in plan.skipped] == [SkipReason.NO_GENERATED_COPY]


# --- --links-only and the listed page ------------------------------------------


def test_links_only_keeps_a_unit_with_no_generated_copy_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A links run renders no page, so the tagline E21 protects is not needed for it."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products), "--links-only"]) == 0

    plan = _read_plan()
    assert [row.gtin for row in plan.rows] == [GTIN_A]
    assert plan.links_only is True
    assert _read_summary().links_only is True
    assert "--links-only" in capsys.readouterr().err


def test_without_links_only_the_copy_rule_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    plan = _read_plan()
    assert plan.rows == [] and plan.links_only is False


def test_a_listed_page_rides_on_every_row_of_its_gtin_and_nowhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode", "Link naar site"])
    sheet.append([GTIN_A[1:], "https://wp.test/product/rugsteun-oud/"])  # 13 digits, as typed
    sheet.append([GTIN_B, None])
    list_path = tmp_path / "selection.xlsx"
    workbook.save(list_path)
    cfg = _make_config(
        process_list=ProcessListConfig(path=str(list_path), target_url_column="Link naar site")
    )
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A), _product(GTIN_B)])

    assert run_plan.main(["acme", "--products", str(products)]) == 0

    listed = {row.gtin: row.listed_url for row in _read_plan().rows}
    assert listed == {GTIN_A: "https://wp.test/product/rugsteun-oud/", GTIN_B: None}
    # Not folded into target_url, which stays what this tool would publish at.
    assert {row.target_url for row in _read_plan().rows if row.gtin == GTIN_A} == {
        f"https://wp.test/product/p-{GTIN_A}/"
    }
    assert "1 GTIN(s) with a listed page" in capsys.readouterr().err


def test_an_off_site_listed_page_stops_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode", "Link naar site"])
    sheet.append([GTIN_A, "https://elsewhere.example/x/"])
    list_path = tmp_path / "selection.xlsx"
    workbook.save(list_path)
    cfg = _make_config(
        process_list=ProcessListConfig(path=str(list_path), target_url_column="Link naar site")
    )
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_A)])

    assert run_plan.main(["acme", "--products", str(products)]) == 2
    assert "not on the site's host" in capsys.readouterr().err


def test_a_links_only_plan_is_not_held_for_a_page_s_video_or_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator, 2026-10-09: only a bad link may stop a links-only product.

    ``02987653001517`` has a page on the site already and no confirmed video; a links run writes
    its GS1 record and nothing else, so the video and image rules are not its business.
    """
    monkeypatch.chdir(tmp_path)
    vmap = _write_video_map(tmp_path, both=[GTIN_A])
    cfg = _bilingual_config(
        MediaConfig(restrict_to_mapped_gtins=True, video_map_path=vmap, require_hero_image=True)
    )
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_B, product_name={"nl": "Set", "fr": "Set"})])

    assert run_plan.main(["acme", "--products", str(products), "--links-only"]) == 0
    linked = _read_plan()
    assert run_plan.main(["acme", "--products", str(products)]) == 0
    paged = _read_plan()

    assert {row.gtin for row in linked.rows} == {GTIN_B}
    assert linked.skipped == []
    assert paged.rows == [], "a page run still holds it"
    assert paged.skipped


def test_a_links_only_plan_keeps_a_language_with_no_product_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator, 2026-10-09: "We need at least the Dutch URL" — not a name in every language."""
    monkeypatch.chdir(tmp_path)
    cfg = _bilingual_config(None)
    _patch_client(monkeypatch, cfg)
    products = tmp_path / "products.json"
    _write_products(products, [_product(GTIN_B, product_name={"nl": "Set"})])

    assert run_plan.main(["acme", "--products", str(products), "--links-only"]) == 0
    linked = _read_plan()
    assert run_plan.main(["acme", "--products", str(products)]) == 0
    paged = _read_plan()

    assert {(row.language, row.title) for row in linked.rows} == {("nl", "Set"), ("fr", "Set")}
    assert linked.skipped == []
    assert [s.reason for s in paged.skipped] == [SkipReason.MISSING_PRODUCT_NAME]


# --- A GS1 record out of date with its page (2026-10-22) ---------------------------------------
# A pages run records the new title and content hash and keeps the old link-set hash, because it
# wrote no link. Judged on the content hash alone the row was UNCHANGED, and renaming 38 products
# left 38 GS1 records with the old names while a links run said there was nothing to do.


def test_a_renamed_product_plans_its_gs1_link_as_out_of_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    before = _product(GTIN_A)
    _publish(cfg, before)  # live, with the link written for this name
    renamed = before.model_copy(update={"product_name": LocalisedText(values={"nl": "Dizzy Dog"})})
    # What a pages run then records: the page's new title and content hash, the old link hash.
    (row,), _ = diff_against_state(
        [renamed], State(client_id="acme", entries={}), ["nl"], cfg.wordpress
    )
    state = load_state("acme")
    entry = state.entries[GTIN_A]["nl"]
    state.entries[GTIN_A]["nl"] = entry.model_copy(
        update={"content_hash": row.content_hash, "title": row.title}
    )
    save_state(state)
    products = tmp_path / "products.json"
    _write_products(products, [renamed])

    assert run_plan.main(["acme", "--products", str(products), "--links-only"]) == 0

    (planned,) = _read_plan().rows
    assert planned.classification is PlanClassification.CHANGED
    assert planned.diff is not None and run_plan.STALE_LINK_DIFF in planned.diff


def test_a_gs1_link_that_matches_its_page_stays_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild must reproduce what was written, or every live product reads out of date."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    product = _product(GTIN_A)
    _publish(cfg, product)
    products = tmp_path / "products.json"
    _write_products(products, [product])

    assert run_plan.main(["acme", "--products", str(products), "--links-only"]) == 0

    assert [r.classification for r in _read_plan().rows] == [PlanClassification.UNCHANGED]
