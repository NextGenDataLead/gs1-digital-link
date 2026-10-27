"""Tests for scripts/run_generate.py (generator SPEC, commit 4).

run_generate is the producer-agnostic spine: it computes the units this run needs copy for and
moves them to a producer via emit/validate or an injected ``LLMClient``. No LLM is involved here —
the producer is simulated with a fake client or a hand-written results file. The contract and the
merge are covered in ``tests/lib/test_generator.py``; these tests drive ``main`` and the seam over
a temp working directory and a fake ``get_client``.

Nothing is cached, so several of these assert an *absence*: no store is written, and a unit stays
pending however much copy already exists for it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from lib.config import (
    ClientConfig,
    ExportConfig,
    GeneratorConfig,
    GS1Config,
    MediaConfig,
    ProcessListConfig,
    WordPressConfig,
)
from lib.generator import (
    GenerationContext,
    GenerationRequest,
    GenerationResult,
    load_results,
    pending_requests,
)
from lib.records import LocalisedText, ProductRecord, State, StateEntry
from lib.state import diff_against_state, save_state, state_path
from scripts import run_generate

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"


# --- Builders ----------------------------------------------------------------


def _write_process_list(tmp_path: Path, gtins: list[str]) -> ProcessListConfig:
    """A process list naming exactly ``gtins`` — the operator's statement of scope."""
    import openpyxl  # noqa: PLC0415 — only this helper needs it

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode"])
    for gtin in gtins:
        sheet.append([gtin])
    path = tmp_path / "process-list.xlsx"
    workbook.save(path)
    return ProcessListConfig(path=str(path), gtin_column="Barcode")


def _ctx(*languages: str) -> GenerationContext:
    """The context `run_generate` builds for `_make_config` — nothing opted into translation."""
    return GenerationContext(languages=list(languages), default_language="nl", prompt_version="v1")


def _make_config(  # noqa: PLR0913 — one argument per client fact a test needs to vary
    languages: list[str] | None = None,
    generator: GeneratorConfig | None = None,
    process_list: ProcessListConfig | None = None,
    media: MediaConfig | None = None,
    **wordpress: Any,
) -> ClientConfig:
    """A client config. The URL patterns are set because a publishing client has them.

    ``run_generate`` reads them now: narrowing generation to the rows a run would write means
    classifying, and a classification needs the slug and target URL a row would get. A client
    without them cannot be narrowed, which is its own test.
    """
    return ClientConfig(
        client_id="democlient",
        display_name="Democlient",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(path="input/democlient.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            default_language="nl",
            languages=languages or ["nl"],
            **{
                "slug_pattern": "p-{gtin}",
                "target_url_pattern": "{site_url}/{lang_segment}{post_type}/{slug}/",
                **wordpress,
            },
        ),
        generator=generator,
        process_list=process_list,
        media=media,
    )


def _product(
    gtin: str = GTIN_A, *, long_text: str | None = None, short_1067: str | None = None
) -> ProductRecord:
    extras: dict[str, str] = {"material": "kunststof"}
    kwargs: dict[str, Any] = {
        "gtin": gtin,
        "brand": "Democlient",
        "product_name": LocalisedText(values={"nl": "Bewateringpin"}),
        "net_content": "6 H87",
        "description_short": LocalisedText(values={"nl": long_text or "Water voor je planten"}),
        "extras": extras,
    }
    if short_1067 is not None:
        kwargs["description_long"] = LocalisedText(values={"nl": short_1067})
    return ProductRecord(**kwargs)


def _write_products(client_id: str, products: list[ProductRecord]) -> None:
    path = Path("output") / client_id / "data" / "products.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.model_dump(mode="json") for p in products]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_results(client_id: str, results: list[dict[str, Any]], **extra: Any) -> None:
    path = Path("output") / client_id / "data" / "generation_results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"client_id": client_id, "results": results, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_client(monkeypatch: pytest.MonkeyPatch, cfg: ClientConfig) -> None:
    monkeypatch.setattr(run_generate, "get_client", lambda _cid: cfg)


def _read_requests_file() -> run_generate.RequestsFile:
    path = Path("output/democlient/data/generation_requests.json")
    return run_generate.RequestsFile.model_validate(json.loads(path.read_text(encoding="utf-8")))


class _FakeClient:
    """A producer that echoes a deterministic tagline + bullet per request."""

    def generate_copy(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(usps=[f"Tagline {request.language}", "Bullet"])


# --- emit --------------------------------------------------------------------


def test_emit_writes_pending_requests_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])  # no 1067 -> generate

    code = run_generate.main(["democlient"])

    assert code == 0
    payload = _read_requests_file()
    assert payload.client_id == "democlient"
    assert len(payload.requests) == 1
    assert payload.requests[0].mode == "generate"


def test_emit_coverage_line_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])

    run_generate.main(["democlient", "--emit"])

    err = capsys.readouterr().err
    assert "0 tighten, 1 generate" in err
    assert "0/1 unit(s) to publish have copy; 1 without" in err


def test_emit_writes_no_store_of_any_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--emit` used to write the cache too, to persist the verbatim prefill. Both halves are gone.

    Asserted as an absence because that is the change: a second file appearing beside the requests
    is how the old behaviour would come back, and it would come back silently.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product(short_1067="Kort en krachtig")])

    run_generate.main(["democlient", "--emit"])

    written = {p.name for p in Path("output/democlient/data").iterdir()}
    assert written == {"products.json", "generation_requests.json"}


def test_emit_never_asks_for_copy_the_feed_already_supplies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A short 1067 *is* the copy, so the unit costs no producer call and still counts as covered.

    This is what became of `prefill_from_feed`. It wrote those units into the cache; now nothing
    is written at all and `merge_generated` derives them from the feed at plan time. The coverage
    line has to agree, or the operator is told to go and generate something that needs nothing.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product(short_1067="Kort en krachtig")])

    run_generate.main(["democlient"])

    assert _read_requests_file().requests == []
    assert "1/1 unit(s) to publish have copy; 0 without" in capsys.readouterr().err


def test_emit_writes_empty_requests_file_when_nothing_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product(short_1067="Kort en krachtig")])

    run_generate.main(["democlient"])

    assert Path("output/democlient/data/generation_requests.json").exists()
    assert _read_requests_file().requests == []


def test_emit_asks_again_for_a_unit_that_already_has_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always-regenerate, at the command the operator actually runs.

    The cache's freshness skip lived here: a unit with a matching fingerprint was dropped from the
    emitted requests, which is precisely the reuse the operator asked to remove.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    run_generate.main(["democlient", "--emit"])
    request = _read_requests_file().requests[0]
    _write_results(
        "democlient",
        [
            {
                "gtin": request.gtin,
                "language": request.language,
                "usps": ["Slogan", "Punt"],
                "input_fingerprint": request.input_fingerprint,
            }
        ],
    )

    run_generate.main(["democlient", "--emit"])

    assert [r.gtin for r in _read_requests_file().requests] == [GTIN_A]


# --- validate ----------------------------------------------------------------


def test_validate_accepts_results_that_answer_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    _write_results("democlient", [{"gtin": GTIN_A, "language": "nl", "usps": ["Slogan", "Punt"]}])

    code = run_generate.main(["democlient", "--validate"])

    assert code == 0
    assert "validated 1 result(s), rejected 0; 1/1 unit(s) to publish have copy; 0 without" in (
        capsys.readouterr().err
    )


def test_validate_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It replaced `--ingest`, which folded results into the cache. There is nothing to fold into.

    What is left is the report, and a check that reports must not also mutate the thing it is
    reporting on — otherwise running it twice is not the same as running it once.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    _write_results("democlient", [{"gtin": GTIN_A, "language": "nl", "usps": ["Slogan", "Punt"]}])
    path = Path("output/democlient/data/generation_results.json")
    before = path.read_text(encoding="utf-8")

    run_generate.main(["democlient", "--validate"])

    assert path.read_text(encoding="utf-8") == before
    assert not Path("output/democlient/data/generated_cache.json").exists()


def test_emit_then_validate_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])

    run_generate.main(["democlient", "--emit"])
    request = _read_requests_file().requests[0]
    _write_results(
        "democlient",
        [
            {
                "gtin": request.gtin,
                "language": request.language,
                "usps": ["Slogan", "Punt"],
                "input_fingerprint": request.input_fingerprint,
            }
        ],
    )

    code = run_generate.main(["democlient", "--validate"])

    assert code == 0
    assert load_results("democlient").results[0].usps == ["Slogan", "Punt"]


def test_validate_counts_a_result_with_no_pending_unit_as_surplus_not_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It used to be counted as ``rejected``. Rejected means "wanted and unusable"; this is not.

    The two were the same number while every in-scope unit was generated for every run, because
    the only way to hold a result the run had not asked for was to have the wrong file. Now the
    ordinary case is a file written against a wider batch, and a run that reports rejections when
    nothing is wrong teaches the operator to skip the line.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    _write_results(
        "democlient", [{"gtin": GTIN_B, "language": "nl", "usps": ["X"]}]
    )  # unknown gtin

    code = run_generate.main(["democlient", "--validate"])

    assert code == 0
    err = capsys.readouterr().err
    assert "validated 0 result(s), rejected 0, 1 surplus (not needed this run)" in err


def test_validate_rejects_a_stale_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The same decision `run_plan` will make, said early enough to act on.

    Rejecting here is not a second implementation of the rule: it reads the same echoed
    fingerprint against the same freshly computed one, so a result this rejects is exactly a
    result the plan would drop.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    _write_results(
        "democlient",
        [
            {
                "gtin": GTIN_A,
                "language": "nl",
                "usps": ["Slogan"],
                "input_fingerprint": "stale-does-not-match",
            }
        ],
    )

    with caplog.at_level("WARNING"):
        run_generate.main(["democlient", "--validate"])

    assert "stale result" in caplog.text
    assert "run_plan will drop it too" in caplog.text


def test_validate_names_a_unit_answered_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Surfaced, not absorbed — #94's removed dedupe is the precedent.

    `run_plan` takes the last entry, deterministically, so nothing breaks. What is wrong is that
    only one of the two was reviewed, and a count reading "2 validated" would claim twice the
    review that actually happened.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    _write_results(
        "democlient",
        [
            {"gtin": GTIN_A, "language": "nl", "usps": ["Eerste"]},
            {"gtin": GTIN_A, "language": "nl", "usps": ["Tweede"]},
        ],
    )

    with caplog.at_level("WARNING"):
        assert run_generate.main(["democlient", "--validate"]) == 0

    assert "answered more than once" in caplog.text
    # One *unit* was answered, whatever the file's line count says.
    assert "validated 1 result(s), rejected 0" in capsys.readouterr().err


def test_validate_rejects_wrong_client_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    path = Path("output/democlient/data/generation_results.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client_id": "other", "results": []}), encoding="utf-8")

    code = run_generate.main(["democlient", "--validate"])

    assert code == 2
    assert "config error" in capsys.readouterr().err


def test_validate_places_a_results_file_given_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate *and place*: only one path is the one `run_plan` reads.

    A producer may write anywhere, and asking the operator to copy the file into position by hand
    is a step to forget — after which the plan silently uses whatever was there before.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])
    elsewhere = tmp_path / "from-the-producer.json"
    elsewhere.write_text(
        json.dumps(
            {
                "client_id": "democlient",
                "results": [{"gtin": GTIN_A, "language": "nl", "usps": ["Slogan", "Punt"]}],
            }
        ),
        encoding="utf-8",
    )

    code = run_generate.main(["democlient", "--validate", "--results", str(elsewhere)])

    assert code == 0
    assert load_results("democlient").results[0].usps == ["Slogan", "Punt"]


def test_validate_missing_results_file_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "No copy yet" is a state to report, not a crash — the doctor says the same thing.

    `--ingest` exited 2 here because it had nothing to ingest. `--validate` answers a question,
    and "none of it is written yet" is a valid answer to that question.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product()])

    code = run_generate.main(["democlient", "--validate"])

    assert code == 0
    assert "0/1 unit(s) to publish have copy; 1 without" in capsys.readouterr().err


# --- the LLMClient seam ------------------------------------------------------


def test_run_producer_returns_the_items_to_write() -> None:
    requests = pending_requests([_product()], _ctx("nl"))

    items = run_generate.run_producer(requests, _FakeClient())

    assert len(items) == 1
    assert items[0].usps == ["Tagline nl", "Bullet"]
    assert items[0].gtin == GTIN_A
    assert items[0].input_fingerprint == requests[0].input_fingerprint


# --- API backend (--backend api) ---------------------------------------------


def _tool_response(usps: list[str]) -> dict[str, Any]:
    tool_use = {"type": "tool_use", "id": "t1", "name": "produce_copy", "input": {"usps": usps}}
    return {"stop_reason": "tool_use", "content": [tool_use]}


def _write_voice(prompt_version: str = "v1") -> None:
    path = Path("prompts") / "democlient" / f"generation.{prompt_version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!-- prompt_version: v1 -->\nvoice", encoding="utf-8")


def test_backend_api_without_generator_config_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())  # no generator block
    _write_products("democlient", [_product()])

    code = run_generate.main(["democlient", "--backend", "api"])

    assert code == 2
    assert "generator" in capsys.readouterr().err


def test_backend_api_writes_the_results_file_via_mocked_messages_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Both producers now write the same file, which is what keeps the headless path usable.

    It used to fill the cache directly and never touch the seam, so removing the cache would have
    stranded it. `provenance` is where the difference between the two survives, and the report
    reads it.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_KEY", "sk-test")
    cfg = _make_config(
        generator=GeneratorConfig(
            enabled=True, model="claude-sonnet-5", prompt_version="v1", api_key_env="TEST_KEY"
        )
    )
    _patch_client(monkeypatch, cfg)
    _write_products("democlient", [_product()])  # 1 product, nl -> 1 generate request
    _write_voice()
    httpx_mock.add_response(json=_tool_response(["Tagline", "Bullet"]))

    code = run_generate.main(["democlient", "--backend", "api"])

    assert code == 0
    results = load_results("democlient")
    assert results.provenance == "api:claude-sonnet-5"
    assert results.results[0].usps == ["Tagline", "Bullet"]
    assert not Path("output/democlient/data/generated_cache.json").exists()


# --- shared failure paths ----------------------------------------------------


def test_missing_products_file_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())

    assert run_generate.main(["democlient"]) == 2


# --- scope -------------------------------------------------------------------


def test_emit_only_asks_for_copy_the_run_would_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: this command worked from the whole catalogue and never knew scope existed.

    The doctor and ``--emit`` answered the same question two orders of magnitude apart — 10
    against 224 on the pilot client. That is real tokens and real time spent writing copy for
    products nobody is publishing, and a content-review gate with hundreds of units in it, which
    is the surest way to make a review gate go unread.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(process_list=_write_process_list(tmp_path, [GTIN_A])))
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    assert run_generate.main(["democlient", "--emit"]) == 0

    payload = _read_requests_file()
    assert [request.gtin for request in payload.requests] == [GTIN_A]


def test_emit_and_the_doctor_report_the_same_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's actual acceptance test, and it is a cross-check rather than a restatement.

    ``check_generation_results`` and ``--emit`` compute what this run needs independently, in
    different modules. They agree only because both narrow through ``lib.preflight.in_scope``;
    before that they disagreed by 22x on real data, and nothing anywhere noticed.
    """
    from lib.preflight import check_generation_results  # noqa: PLC0415 — only this test needs it

    monkeypatch.chdir(tmp_path)
    cfg = _make_config(
        generator=GeneratorConfig(enabled=True),
        process_list=_write_process_list(tmp_path, [GTIN_A]),
    )
    _patch_client(monkeypatch, cfg)
    products = [_product(GTIN_A), _product(GTIN_B)]
    _write_products("democlient", products)

    assert run_generate.main(["democlient", "--emit"]) == 0

    emitted = len(_read_requests_file().requests)
    assert emitted == check_generation_results(cfg, products).data["pending"]


def test_validating_an_out_of_scope_result_says_that_is_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A results file written before the process list was pruned still holds those units.

    They are correctly ignored, but a vague reason would send a reader to the feed to explain a
    scope decision. Two causes now — out of scope, or the feed supplies the copy — because
    "already cached fresh" cannot happen when nothing is cached.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(process_list=_write_process_list(tmp_path, [GTIN_A])))
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])
    _write_results(
        "democlient", [{"gtin": GTIN_B, "language": "nl", "usps": ["Tagline", "Bullet"]}]
    )

    with caplog.at_level("WARNING"):
        assert run_generate.main(["democlient", "--validate"]) == 0

    assert "not in scope for this run" in caplog.text


def test_validating_a_feed_verbatim_result_says_that_is_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other cause, and the one a reader would otherwise misread as "out of scope"."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product(short_1067="Kort en krachtig")])
    _write_results("democlient", [{"gtin": GTIN_A, "language": "nl", "usps": ["Iets anders"]}])

    with caplog.at_level("WARNING"):
        assert run_generate.main(["democlient", "--validate"]) == 0

    assert "the feed supplies this unit's copy verbatim" in caplog.text


# --- generation is scoped to the rows this run would write --------------------
#
# Copy is written per run for the rows a run executes — NEW and CHANGED. An UNCHANGED row is never
# confirmed and never executed, so asking a producer for it spends tokens on text nothing reads.
# This narrowing is *scope*, not reuse: what excludes a unit is that nothing will be published for
# it, never that copy for it already exists.


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


def test_emit_skips_a_unit_that_is_already_live_and_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    live, fresh = _product(GTIN_A), _product(GTIN_B)
    _write_products("democlient", [live, fresh])
    _publish(cfg, live)

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert [(r.gtin, r.language) for r in _read_requests_file().requests] == [(GTIN_B, "nl")]
    err = capsys.readouterr().err
    assert "emitted 1 request(s)" in err
    # The narrowing is stated, not silent: "1 request" over a two-product scope needs a reason.
    assert "1 already live and unchanged" in err


def _write_video_map(tmp_path: Path, confirmed: list[str], languages: list[str]) -> MediaConfig:
    """A video map confirming ``confirmed`` in every one of ``languages``, wired to restrict."""
    import yaml  # noqa: PLC0415 — only this helper needs it

    entries = {
        language: [{"file": f"{g}_{language}.mp4", "gtin": g, "confirmed": True} for g in confirmed]
        for language in languages
    }
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return MediaConfig(video_map_path=str(path), restrict_to_mapped_gtins=True)


def test_emit_skips_a_unit_whose_product_the_plan_will_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 74-vs-20 fix: a held product never becomes a row, so its copy is never read.

    ``in_scope`` deliberately keeps a video-less GTIN visible — it is asked-for work that cannot
    yet be done, which the report has to say. That is a different question from whether a producer
    should be paid to write copy for it, and this is where the two part company.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(media=_write_video_map(tmp_path, [GTIN_A], ["nl"]))
    _patch_client(monkeypatch, cfg)
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert [(r.gtin, r.language) for r in _read_requests_file().requests] == [(GTIN_A, "nl")]
    err = capsys.readouterr().err
    assert "1 held by the plan" in err
    # Not folded into the unchanged count: nothing here is live, and saying so would be a lie the
    # operator acts on — they would look for a published page instead of a missing video.
    assert "already live and unchanged" not in err


def test_validating_a_held_result_says_held_rather_than_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The two send an operator to different places: one needs nothing done, one needs a video."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(media=_write_video_map(tmp_path, [GTIN_A], ["nl"]))
    _patch_client(monkeypatch, cfg)
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])
    _write_results(
        "democlient", [{"gtin": GTIN_B, "language": "nl", "usps": ["Tagline", "Bullet"]}]
    )

    with caplog.at_level("WARNING"):
        assert run_generate.main(["democlient", "--validate"]) == 0

    assert "held by the plan (no_confirmed_video)" in caplog.text


def test_emit_asks_again_for_a_unit_whose_feed_data_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHANGED is written, so it needs copy — the other half of the rule."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    product = _product(GTIN_A)
    _publish(cfg, product)
    _write_products("democlient", [product.model_copy(update={"brand": "Ander Merk"})])

    assert run_generate.main(["democlient", "--emit", "--include-published"]) == 0

    assert [r.gtin for r in _read_requests_file().requests] == [GTIN_A]


def test_emit_leaves_a_changing_live_page_its_text_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    product = _product(GTIN_A)
    _publish(cfg, product)
    _write_products("democlient", [product.model_copy(update={"brand": "Ander Merk"})])

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert _read_requests_file().requests == []
    err = capsys.readouterr().err
    assert "1 live and changing, keeping their live text" in err
    assert "already live and unchanged" not in err


def test_emit_asks_for_everything_when_there_is_no_state_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat pin: a first run has no state, so every unit is NEW and nothing narrows."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(languages=["nl", "fr"]))
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert len(_read_requests_file().requests) == 4


def test_emit_asks_for_everything_when_state_will_not_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Erring wide. Narrowing on a guess would write no copy for a page about to be published."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    _write_products("democlient", [_product(GTIN_A)])
    path = state_path("democlient")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert len(_read_requests_file().requests) == 1
    assert path.read_text(encoding="utf-8") == "{ not json"  # never quarantined from here


def test_validate_counts_a_surplus_result_apart_from_a_rejected_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A results file wider than this run's batch is normal now, and is not a rejection.

    Under scoped generation the operator regenerates against a shrinking batch, so surplus copy is
    the expected state of a file written a run ago. Counting it as "rejected" puts an alarming
    number on a healthy run, which is how an operator learns to stop reading the number.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    live, fresh = _product(GTIN_A), _product(GTIN_B)
    _write_products("democlient", [live, fresh])
    _publish(cfg, live)
    _write_results(
        "democlient",
        [
            {"gtin": GTIN_A, "language": "nl", "usps": ["Oud"]},
            {"gtin": GTIN_B, "language": "nl", "usps": ["Nieuw"]},
        ],
    )

    assert run_generate.main(["democlient", "--validate"]) == 0

    err = capsys.readouterr().err
    assert "validated 1 result(s), rejected 0" in err
    assert "1 surplus (not needed this run)" in err


def test_validating_an_unchanged_unit_says_that_is_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The third cause of "no pending unit", and the one that is now the commonest."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _patch_client(monkeypatch, cfg)
    product = _product(GTIN_A)
    _write_products("democlient", [product])
    _publish(cfg, product)
    _write_results("democlient", [{"gtin": GTIN_A, "language": "nl", "usps": ["Oud"]}])

    with caplog.at_level("WARNING"):
        assert run_generate.main(["democlient", "--validate"]) == 0

    assert "already live and unchanged" in caplog.text


def test_emit_asks_for_everything_when_the_client_has_no_url_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No patterns, no classification, no narrowing — and the run still works."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(slug_pattern=None, target_url_pattern=None)
    _patch_client(monkeypatch, cfg)
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    assert run_generate.main(["democlient", "--emit"]) == 0

    assert len(_read_requests_file().requests) == 2


# --- --gtins: the operator names the products --------------------------------


def test_gtins_writes_for_exactly_the_named_products_in_every_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path that lets a screen drive generation from the **site** instead of the ledger.

    ``state.json`` records what this machine wrote and does not travel, so a second operator's
    copy can be confidently wrong about a page that is live and correct. Asked for named products,
    this reads no state at all and asks for every language of each — deliberately wider than the
    default, because a unit the ledger would call UNCHANGED is exactly what somebody picking a
    product to regenerate means to overwrite.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(languages=["nl", "fr"]))
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    code = run_generate.main(["democlient", "--emit", "--gtins", GTIN_B])

    assert code == 0
    units = {(request.gtin, request.language) for request in _read_requests_file().requests}
    assert units == {(GTIN_B, "nl"), (GTIN_B, "fr")}


def test_gtins_accepts_a_comma_separated_list_and_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both forms, because the shell sends one string and a person types several flags."""
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    run_generate.main(["democlient", "--emit", "--gtins", f"{GTIN_A},{GTIN_B}"])
    both = {request.gtin for request in _read_requests_file().requests}

    run_generate.main(["democlient", "--emit", "--gtins", GTIN_A, "--gtins", GTIN_B])

    assert both == {GTIN_A, GTIN_B}
    assert {request.gtin for request in _read_requests_file().requests} == both


def test_gtins_picks_within_the_process_list_never_around_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Scope is not a suggestion. A GTIN the operator has not put in scope is named back to them.

    Writing it quietly would make ``--gtins`` a way around the process list, which is the one
    control deciding what a run may touch at all.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(process_list=_write_process_list(tmp_path, [GTIN_A])))
    _write_products("democlient", [_product(GTIN_A), _product(GTIN_B)])

    run_generate.main(["democlient", "--emit", "--gtins", f"{GTIN_A},{GTIN_B}"])

    assert {request.gtin for request in _read_requests_file().requests} == {GTIN_A}
    assert GTIN_B in caplog.text


def test_gtins_is_not_the_same_as_no_gtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` means "use the ledger"; an empty set means "the operator named nothing".

    Conflating them turns a typo into a regeneration of the whole batch, which is real money and
    a rewrite of live pages.
    """
    assert run_generate._named_gtins(None) is None
    assert run_generate._named_gtins([]) == frozenset()
    assert run_generate._named_gtins(["  "]) == frozenset()


def test_gtins_never_reports_a_negative_unchanged_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The coverage line derives "already live and unchanged" by subtraction.

    On this path the named set deliberately includes units the ledger would call unchanged, and
    every held unit as well, so the subtraction went **negative** — it printed "-4 already live
    and unchanged". A count that can be negative is a count nobody should be shown.
    """
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(languages=["nl", "fr"]))
    _write_products("democlient", [_product(GTIN_A)])

    run_generate.main(["democlient", "--emit", "--gtins", GTIN_A])

    assert "-" not in capsys.readouterr().err.split("without")[-1]
