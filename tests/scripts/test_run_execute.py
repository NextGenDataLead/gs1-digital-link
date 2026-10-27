"""Tests for scripts/run_execute.py (IMPLEMENTATION_SPEC §8.3, §5.4, §6.5, §12 Phase 6).

run_execute orchestrates the real ``TemplateEngine`` and ``render_qr`` but delegates
every HTTP mutation to the WordPress and GS1 clients, whose wire behaviour is already
covered exhaustively by ``tests/lib/test_wp_client.py`` and
``tests/lib/test_gs1_dl_client.py``. So here the two clients are replaced with recording
fakes and the tests assert the *orchestration*: order of operations, state updates,
JSONL logging, exit codes, dry-run side-effect suppression, the confirmed subset, and
§6.5 idempotency. Real HTTP wiring is exercised by the ``staging``-marked integration test.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml

from lib import ticked_snapshot
from lib.config import (
    CategoryConfig,
    ClientConfig,
    ExportConfig,
    GeneratorConfig,
    GS1Config,
    GS1LinkConfig,
    MediaConfig,
    ProcessListConfig,
    QRConfig,
    TaxonomyConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.eligibility import Eligibility
from lib.errors import MediaIntegrityError, WordPressAPIError
from lib.input_layout import archive_path
from lib.issue_report import selection_issues
from lib.provenance import history_path, read_run, record_upload
from lib.records import LocalisedText, Plan, PlanClassification, PlanRow, ProductRecord, State
from lib.run_files import (
    RESULT_NAME,
    SELECTION_NAME,
    SOURCES_NAME,
    TICKED_NAME,
    UPLOAD_NAME,
    iter_logs,
    sibling,
    stamp_of,
)
from lib.state import load_state, save_state
from lib.wp_client import MediaUpload
from scripts import run_execute

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"


# --- Fixtures / builders -----------------------------------------------------


def _make_config(**overrides: Any) -> ClientConfig:
    params: dict[str, Any] = {
        "client_id": "acme",
        "display_name": "Acme BV",
        "gs1": GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
            digital_link_url_pattern="https://id.gs1.org/01/{gtin14}",
        ),
        "export": ExportConfig(path="input/acme.xlsx"),
        "wordpress": WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl", "fr"],
        ),
        "template": TemplateConfig(override_dir=None),  # falls back to templates/_default
        "qr": QRConfig(formats=["svg"], size_mm=20, error_correction="M", dpi=300),
        "gs1_links": [
            GS1LinkConfig(
                link_type="pip", default=True, public=True, title_pattern="{product_name}"
            )
        ],
    }
    params.update(overrides)
    return ClientConfig(**params)


def _product(gtin: str = GTIN_A, *, image_url: str | None = None) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Acme",
        product_name=LocalisedText(values={"nl": "Rugsteun", "fr": "Support"}),
        image_url=image_url,
    )


def _row(gtin: str = GTIN_A, language: str = "nl", *, image_url: str | None = None) -> PlanRow:
    return PlanRow(
        gtin=gtin,
        language=language,
        classification=PlanClassification.NEW,
        title="Rugsteun",
        slug=f"p-{gtin}",
        content_hash="hash-" + gtin,
        target_url=f"https://wp.test/product/p-{gtin}/",
        product=_product(gtin, image_url=image_url),
    )


def _plan(*rows: PlanRow) -> Plan:
    return Plan(
        client_id="acme",
        generated_at=datetime(2026, 7, 12, tzinfo=UTC),
        total=len(rows),
        counts={PlanClassification.NEW: len(rows)},
        rows=list(rows),
    )


def _write_json(path: Path, model: Plan) -> Path:
    path.write_text(json.dumps(model.model_dump(mode="json")), encoding="utf-8")
    return path


def _page_id(slug: str, language: str = "nl") -> int:
    """Deterministic WordPress id per (slug, language), so re-runs are idempotent (§6.5).

    Language is part of the key because the slug deliberately has *no* language component
    — nl and fr both live at ``p-{gtin}`` (that is the point of the ``?lang=`` write, see
    the page-adapter doc §3.1). Keying on the slug alone would hand both languages the
    same id, and every assertion about linking them as translations would pass vacuously.
    """
    return 1000 + int.from_bytes(hashlib.sha256(f"{slug}/{language}".encode()).digest()[:2], "big")


def _page_url(language: str, slug: str, *, post_type: str = "product") -> str:
    """The URL FakeWP returns for a page — default language at the root, others prefixed.

    Mirrors the real site's ``/democlient/{slug}/`` vs ``/fr/democlient/{slug}/`` split, and
    ``state.py:_lang_segment``. Without this the fake hands both languages the same URL and
    "each link points at its own language's page" is untestable.
    """
    prefix = "" if language == "nl" else f"/{language}"
    return f"https://wp.test{prefix}/{post_type}/{slug}/"


class _Recorder:
    def __init__(self) -> None:
        self.wp: list[dict[str, Any]] = []
        self.gs1: list[dict[str, Any]] = []
        self.verified: list[str] = []
        self.translations: list[dict[str, int]] = []
        self.downloaded: list[str] = []
        self.uploaded: list[dict[str, Any]] = []
        self.slug_lookups: list[dict[str, Any]] = []
        self.deleted_media: list[int] = []
        self.term_reads: list[tuple[str, str, str]] = []
        self.alternate_reads: list[tuple[str, str]] = []
        self.scans: list[tuple[str, str]] = []


def _install(  # noqa: PLR0913 — one knob per failure mode the orchestration has to survive
    monkeypatch: pytest.MonkeyPatch,
    cfg: ClientConfig,
    *,
    verify: bool = True,
    wp_error: Exception | None = None,
    wp_error_languages: tuple[str, ...] = ("nl", "fr"),
    unverifiable: tuple[str, ...] = (),
    findable_slugs: dict[tuple[str, str], int] | None = None,
    upload_error: Exception | None = None,
    reused_media: frozenset[int] = frozenset(),
    delete_media_error: Exception | None = None,
    site_terms: dict[tuple[str, str], int] | None = None,
    alternates: dict[tuple[str, str], str] | None = None,
    resolver: Callable[[str, str], tuple[int | None, str | None]] | None = None,
) -> _Recorder:
    """Patch the two clients with recording fakes.

    ``wp_error_languages`` narrows ``wp_error`` to specific languages, so a test can fail
    one language of a GTIN and leave its sibling healthy.

    ``unverifiable`` lists URLs whose ``verify_url`` **raises**, which is what the real
    client does on a non-2xx — it never returns ``False``. ``findable_slugs`` maps
    ``(slug, language)`` to the page id ``find_by_slug`` should report, so a ``--only links``
    test can choose between the three ways a target gets resolved.

    ``upload_error`` makes ``upload_media`` raise. Unlike the media *resolution* steps, which
    degrade to ``None`` under E7, an upload failure is meant to fail the row — publishing a page
    whose video is missing or truncated while reporting success is the worse outcome.

    ``site_terms`` maps ``(term, language)`` to the id the site has for it; anything absent is a
    term the site lacks in that language.

    ``alternates`` maps ``(url, language)`` to the hreflang address that page names; anything
    absent is a page with no translation in that language.

    ``resolver`` stands in for the public resolver the post-run scan check asks. By default it
    answers from the last record written for the GTIN, as GS1 does once it has caught up — so no
    test reaches ``id.gs1.org``, and none waits: the check's sleep is a no-op.
    """
    rec = _Recorder()

    def caught_up(uri: str, language: str) -> tuple[int | None, str | None]:
        gtin = uri.rstrip("/").rsplit("/", 1)[-1]
        written = [kw for kw in rec.gs1 if kw["gtin"] == gtin]
        links = written[-1]["links"] if written else []
        target = next((lk["target_url"] for lk in links if lk["language"] == language), None)
        return (307, target) if target else (404, None)

    def scan(uri: str, language: str) -> tuple[int | None, str | None]:
        rec.scans.append((uri, language))
        return (resolver or caught_up)(uri, language)

    monkeypatch.setattr(run_execute, "_fetch_landing", scan)
    monkeypatch.setattr(run_execute, "_sleep", lambda _seconds: None)
    # Each read moves time a minute on, so a resolver that never catches up exhausts the wait in
    # a couple of rounds instead of spinning for two real minutes.
    ticks = iter(range(0, 10_000, 60))
    monkeypatch.setattr(run_execute, "_clock", lambda: float(next(ticks)))

    class FakeWP:
        def __init__(self, config: WordPressConfig) -> None:
            self._default_language = config.default_language

        def __enter__(self) -> FakeWP:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def upsert_page(self, **kw: Any) -> dict[str, Any]:
            rec.wp.append(kw)
            if wp_error is not None and kw["language"] in wp_error_languages:
                raise wp_error
            language = kw["language"]
            pid = kw["existing_id"] or _page_id(kw["slug"], language)
            return {"id": pid, "link": _page_url(language, kw["slug"], post_type=kw["post_type"])}

        def verify_url(self, url: str) -> bool:
            rec.verified.append(url)
            if url in unverifiable:
                # Shaped like the real client's: it names the call, which _verify_targets
                # then re-raises as a RuntimeError explaining the GS1 refusal.
                raise WordPressAPIError(404, "Not Found", call=f"HEAD {url}")
            return verify

        def find_by_slug(
            self, post_type: str, slug: str, language: str | None = None
        ) -> dict[str, Any] | None:
            rec.slug_lookups.append({"post_type": post_type, "slug": slug, "language": language})
            pid = (findable_slugs or {}).get((slug, language or ""))
            if pid is None:
                return None
            return {"id": pid, "link": _page_url(language or "nl", slug, post_type=post_type)}

        def link_translations(self, translations: dict[str, int]) -> None:
            rec.translations.append(translations)

        def term_id(self, taxonomy: str, term: str, language: str) -> int | None:
            rec.term_reads.append((taxonomy, term, language))
            return (site_terms or {}).get((term, language))

        def alternate_url(self, url: str, language: str) -> str | None:
            rec.alternate_reads.append((url, language))
            return (alternates or {}).get((url, language))

        def download_image(self, url: str) -> bytes | None:
            rec.downloaded.append(url)
            return None if url == "MISSING" else b"imgbytes:" + url.encode()

        def upload_media(
            self, file_path: Any, title: str | None = None, *, key: str | None = None
        ) -> MediaUpload:
            path = str(file_path)
            if upload_error is not None:
                rec.uploaded.append({"path": path, "title": title, "key": key, "id": None})
                raise upload_error
            mid = 5000 + int.from_bytes(hashlib.sha256(path.encode()).digest()[:2], "big")
            rec.uploaded.append({"path": path, "title": title, "key": key, "id": mid})
            # created is False for an id already seen: the real client dedupes by content hash,
            # and a rollback must be able to tell "we added this" from "this was already here".
            first = mid not in {u["id"] for u in rec.uploaded[:-1]}
            return MediaUpload(mid, created=first and mid not in reused_media)

        def delete_media(self, media_id: int) -> bool:
            rec.deleted_media.append(media_id)
            if delete_media_error is not None:
                raise delete_media_error
            return True

        def media_source_url(self, media_id: int) -> str | None:
            return f"https://wp.test/wp-content/uploads/{media_id}.jpg"

    class FakeGS1:
        def __init__(self, config: object) -> None:
            pass

        def __enter__(self) -> FakeGS1:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def safe_upsert(self, **kw: Any) -> None:
            rec.gs1.append(kw)

    monkeypatch.setattr(run_execute, "get_client", lambda _cid: cfg)
    monkeypatch.setattr(run_execute, "WordPressClient", FakeWP)
    monkeypatch.setattr(run_execute, "GS1DigitalLinkClient", FakeGS1)
    return rec


# --- Per-GTIN operations across languages ------------------------------------


def test_both_languages_land_in_one_gs1_link_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One GS1 write per GTIN, carrying every language — not one write per language.

    GS1's CreateOrUpdate **replaces** the links array (confirmed live against the real
    API). The pipeline used to issue one ``safe_upsert`` per (GTIN, language), each with a
    single-element array, so the fr row overwrote the record with only its own link — the
    nl link was destroyed, the Dutch QR resolved nowhere, and the row reported ``ok``.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 0
    assert len(rec.gs1) == 1  # one write for the GTIN, not one per language
    links = {link["language"]: link for link in rec.gs1[0]["links"]}
    assert set(links) == {"nl", "fr"}
    # Each link points at its own language's page.
    assert links["nl"]["target_url"] == _page_url("nl", f"p-{GTIN_A}")
    assert links["fr"]["target_url"] == _page_url("fr", f"p-{GTIN_A}")
    assert links["fr"]["link_title"] == "Support"  # the fr product_name, not the nl one
    # "standaardlink voor nl, niet voor fr" — exactly one default link, and it is nl.
    assert links["nl"]["default_link_type"] is True
    assert links["fr"]["default_link_type"] is False


def test_translations_are_linked_once_per_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pages are linked as a translation group — the third call of §3.1.

    ``link_translations`` existed on the client and was never called from the pipeline, so
    a run left nl and fr as unrelated pages with their own trids.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert rec.translations == [
        {
            "nl": _page_id(f"p-{GTIN_A}", "nl"),
            "fr": _page_id(f"p-{GTIN_A}", "fr"),
        }
    ]


def test_sibling_language_failure_blocks_the_whole_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If one language's page fails, the GTIN gets no GS1 write and no state at all.

    A link set built from the surviving language would **replace** the array and destroy
    the failed language's link. And writing state for the survivor would make the next run
    classify it UNCHANGED, so the GS1 write would never be retried.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg, wp_error=RuntimeError("boom"), wp_error_languages=("fr",))
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 1
    assert rec.gs1 == []  # a partial link set would have destroyed the fr link
    assert rec.translations == []
    assert load_state("acme").entries == {}  # nl must stay retryable
    logs = list((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"))
    outcomes = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert [o["status"] for o in outcomes] == ["error", "error"]


def test_partial_confirm_reconstructs_the_other_language_from_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirming only fr must not drop nl from the link set.

    The orchestrator confirms rows individually, so an operator can apply fr and skip nl.
    Because the array replaces, sending links:[fr] would destroy the nl link — so the nl
    link is rebuilt from the state entry written by the run that created its page.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)

    # A prior run created the nl page.
    both = _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr"))
    nl_only = _write_json(tmp_path / "nl.json", _plan(_row(GTIN_A, "nl")))
    assert run_execute.main(["acme", "--plan", str(nl_only)]) == 0
    rec.gs1.clear()
    rec.translations.clear()

    # Now only the fr row is confirmed.
    confirmed = {
        "plan": both.model_dump(mode="json"),
        "confirmed_gtins_by_lang": [[GTIN_A, "fr"]],
    }
    path = tmp_path / "confirmed.json"
    path.write_text(json.dumps(confirmed), encoding="utf-8")

    assert run_execute.main(["acme", "--confirmed", str(path)]) == 0

    assert len(rec.gs1) == 1
    links = {link["language"]: link for link in rec.gs1[0]["links"]}
    assert set(links) == {"nl", "fr"}  # nl survives, rebuilt from state
    assert links["nl"]["target_url"] == _page_url("nl", f"p-{GTIN_A}")
    assert links["nl"]["default_link_type"] is True
    # The translation group keeps the stored nl page id alongside the fresh fr one.
    assert rec.translations == [
        {"nl": _page_id(f"p-{GTIN_A}", "nl"), "fr": _page_id(f"p-{GTIN_A}", "fr")}
    ]


# --- Category: the page goes in its category, in its own language -------------------
# Found 2026-10-08: the taxonomy was configured and never written, so none of the 38 live pages
# showed on a category page.

_TAX = "product-categories"


def _categorised(**categories: Any) -> ClientConfig:
    cfg = _make_config()
    wordpress = cfg.wordpress.model_copy(
        update={"taxonomies": {_TAX: TaxonomyConfig(map_from_column="category")}}
    )
    return _make_config(
        wordpress=wordpress,
        categories=CategoryConfig(terms=["keuken", "outdoor_dier"], **categories),
    )


def _in(row: PlanRow, category: str | None) -> PlanRow:
    return row.model_copy(update={"product": row.product.model_copy(update={"category": category})})


def test_each_page_gets_its_category_in_its_own_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(
        monkeypatch, _categorised(), site_terms={("keuken", "nl"): 5, ("keuken", "fr"): 22}
    )
    plan = _write_json(
        tmp_path / "plan.json",
        _plan(_in(_row(GTIN_A, "nl"), "keuken"), _in(_row(GTIN_A, "fr"), "keuken")),
    )

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert {c["language"]: c["terms"] for c in rec.wp} == {"nl": {_TAX: [5]}, "fr": {_TAX: [22]}}


def test_a_category_the_site_lacks_refuses_the_product_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page no category page lists looks finished and is not — create the term, or remap."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _categorised(), site_terms={("keuken", "nl"): 5})
    plan = _write_json(tmp_path / "plan.json", _plan(_in(_row(GTIN_A, "nl"), "outdoor_dier")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 1
    assert rec.wp == [] and rec.gs1 == [] and rec.uploaded == []
    error = _read_outcomes(tmp_path)[0]["error"]
    assert "no 'product-categories' term 'outdoor_dier' in nl" in error


def test_with_require_terms_exist_off_a_missing_category_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _categorised(require_terms_exist=False))
    plan = _write_json(tmp_path / "plan.json", _plan(_in(_row(GTIN_A, "nl"), "keuken")))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert rec.wp[0]["terms"] is None
    assert "publishing without it" in caplog.text


def test_a_product_with_no_category_is_published_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmapped brick: the plan already reported it, and there is nothing to look up."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _categorised())
    plan = _write_json(tmp_path / "plan.json", _plan(_in(_row(GTIN_A, "nl"), None)))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert rec.wp[0]["terms"] is None
    assert rec.term_reads == []


def test_a_dry_run_names_the_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _categorised())
    plan = _write_json(tmp_path / "plan.json", _plan(_in(_row(GTIN_A, "nl"), "keuken")))

    with caplog.at_level("INFO"):
        assert run_execute.main(["acme", "--plan", str(plan), "--dry-run"]) == 0
    assert "in category 'keuken'" in caplog.text


# --- Happy path --------------------------------------------------------------


def test_happy_path_one_gtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 0
    # WP upsert (new: existing_id None) -> verify -> GS1 set, in order.
    assert rec.wp[0]["existing_id"] is None
    assert rec.wp[0]["meta"] == {"gtin": GTIN_A}
    assert rec.verified == [f"https://wp.test/product/p-{GTIN_A}/"]
    assert rec.gs1[0]["gtin"] == GTIN_A
    assert rec.gs1[0]["overwrite"] is True
    # GS1 link points at the actual page URL and carries the resolved title. Keyed by
    # language, not index: the link set spans every language of the GTIN, in sorted order.
    links = {link["language"]: link for link in rec.gs1[0]["links"]}
    assert set(links) == {"nl"}  # this plan confirms only nl
    assert links["nl"]["target_url"] == f"https://wp.test/product/p-{GTIN_A}/"
    assert links["nl"]["link_title"] == "Rugsteun"  # title_pattern "{product_name}" for nl
    # State persisted for the row.
    state = load_state("acme")
    entry = state.entries[GTIN_A]["nl"]
    assert entry.wp_page_id == _page_id(f"p-{GTIN_A}")
    assert entry.content_hash == "hash-" + GTIN_A
    assert entry.title == "Rugsteun"  # the next run diffs the title against this (§10.6.2)
    # QR rendered to disk.
    assert (tmp_path / "output" / "acme" / "qr" / f"{GTIN_A}.svg").is_file()
    # One ok outcome logged.
    logs = list((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"))
    outcomes = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert [o["status"] for o in outcomes] == ["ok"]


# --- §6.5 idempotency --------------------------------------------------------


def test_rerun_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    first = _entry_without_timestamp(load_state("acme"))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    second = _entry_without_timestamp(load_state("acme"))

    # §6.5: same confirmed plan twice -> same final state (ids/hashes), no duplicates.
    assert first == second


def _entry_without_timestamp(state: State) -> dict[str, dict[str, dict[str, object]]]:
    out: dict[str, dict[str, dict[str, object]]] = {}
    for gtin, langs in state.entries.items():
        out[gtin] = {}
        for lang, entry in langs.items():
            dumped = entry.model_dump(mode="json")
            dumped.pop("last_run")  # advances every run by design
            out[gtin][lang] = dumped
    return out


# --- Error path --------------------------------------------------------------


def test_verify_failure_marks_error_and_skips_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg, verify=False)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 1
    assert rec.gs1 == []  # never reached GS1 after the failed verify
    assert load_state("acme").entries == {}  # row not persisted
    logs = list((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"))
    outcomes = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert outcomes[0]["status"] == "error"
    assert "did not return 200" in outcomes[0]["error"]


def test_failed_row_records_which_call_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row runs five HTTP calls; the run log has to say which one broke.

    Reproduces issue #60: the row reported ``failed: 403`` and it took a re-run with the
    output captured to a file to learn the failure was a video upload rather than the page.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    call = "POST /wp-json/wp/v2/media (upload media clip-a1b2c3d4e5f6)"
    _install(
        monkeypatch,
        cfg,
        wp_error=WordPressAPIError(403, "<html><title>403 Forbidden</title></html>", call=call),
    )
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 1
    outcome = _read_outcomes(tmp_path, newest=True)[0]
    assert outcome["failed_call"] == call
    assert "403 Forbidden" in outcome["error"]  # the body, not just the status code


def test_failed_call_survives_the_verify_targets_wrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_verify_targets`` re-raises as ``RuntimeError``; the call identity must not be lost.

    That wrap is the one path that already adds context — why a non-serving target refuses a
    permanent GS1 write — so it would be the worst one to drop the call from.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    dead = f"https://wp.test/product/p-{GTIN_A}/"
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A)))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    _install(monkeypatch, cfg, unverifiable=(dead,))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 1
    outcome = _read_outcomes(tmp_path, newest=True)[0]
    assert outcome["failed_call"] == f"HEAD {dead}"
    assert "refusing to point a permanent GS1 record" in outcome["error"]


# --- Dry run -----------------------------------------------------------------


def test_dry_run_performs_no_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--dry-run"])

    assert code == 0
    assert rec.wp == [] and rec.gs1 == []  # no mutating client calls
    assert not (tmp_path / "output" / "acme" / "state.json").exists()  # no state write
    qr_dir = tmp_path / "output" / "acme" / "qr"
    assert not qr_dir.exists() or not list(qr_dir.glob("*"))  # no QR files
    logs = list((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"))
    outcomes = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert outcomes[0]["status"] == "dry-run"


# --- Confirmed subset --------------------------------------------------------


def test_confirmed_subset_executes_only_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl"))
    confirmed = {
        "plan": plan.model_dump(mode="json"),
        "confirmed_gtins_by_lang": [[GTIN_A, "nl"]],
    }
    path = tmp_path / "confirmed.json"
    path.write_text(json.dumps(confirmed), encoding="utf-8")

    code = run_execute.main(["acme", "--confirmed", str(path)])

    assert code == 0
    assert [c["meta"]["gtin"] for c in rec.wp] == [GTIN_A]  # only the confirmed row
    assert set(load_state("acme").entries) == {GTIN_A}


# --- Held rows (§8.3) --------------------------------------------------------


def _held(row: PlanRow) -> PlanRow:
    return row.model_copy(update={"classification": PlanClassification.HELD})


def test_drop_held_skips_held_gtins_by_default() -> None:
    rows = [_held(_row(GTIN_A)), _row(GTIN_B)]

    kept = run_execute._drop_held(rows, revive=False)

    assert [row.gtin for row in kept] == [GTIN_B]


def test_drop_held_keeps_everything_with_revive() -> None:
    rows = [_held(_row(GTIN_A)), _row(GTIN_B)]

    kept = run_execute._drop_held(rows, revive=True)

    assert [row.gtin for row in kept] == [GTIN_A, GTIN_B]


def test_drop_held_drops_the_whole_gtin_not_just_the_held_row() -> None:
    # The resolver write carries every language at once, so publishing the fr row of a
    # held GTIN would write a link set missing nl — the per-language destruction the
    # per-GTIN phase exists to prevent.
    rows = [_held(_row(GTIN_A, "nl")), _row(GTIN_A, "fr"), _row(GTIN_B, "nl")]

    kept = run_execute._drop_held(rows, revive=False)

    assert [(row.gtin, row.language) for row in kept] == [(GTIN_B, "nl")]


def test_held_gtin_is_not_republished_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failure this guards: confirming a plan is a judgement about content, not a
    # licence to undo somebody's unpublish. No WP or GS1 write may reach a held GTIN.
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    path = _write_json(tmp_path / "plan.json", _plan(_held(_row(GTIN_A)), _row(GTIN_B)))

    code = run_execute.main(["acme", "--plan", str(path)])

    assert code == 0
    assert [c["meta"]["gtin"] for c in rec.wp] == [GTIN_B]
    assert [c["gtin"] for c in rec.gs1] == [GTIN_B]
    assert set(load_state("acme").entries) == {GTIN_B}


def test_revive_republishes_a_held_gtin_and_clears_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --revive writes a fresh StateEntry, whose wp_status/retracted defaults are the
    # published condition — so a successful revive clears the hold with no extra code.
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    path = _write_json(tmp_path / "plan.json", _plan(_held(_row(GTIN_A))))

    code = run_execute.main(["acme", "--plan", str(path), "--revive"])

    assert code == 0
    assert [c["meta"]["gtin"] for c in rec.wp] == [GTIN_A]
    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.wp_status == "publish"
    assert entry.retracted is False
    # This run wrote the resolver record too, so the row is fully linked again — the
    # revived-but-still-unresolvable state is what `--revive --only pages` leaves.
    assert entry.gs1_link_set_hash


# --- Config / setup errors ---------------------------------------------------


def test_unknown_client_returns_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    # Real get_client against the repo clients.yml raises for an unknown id.
    code = run_execute.main(["no-such-client", "--plan", str(plan)])

    assert code == 2


def test_requires_plan_or_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):  # argparse mutually-exclusive group is required
        run_execute.main(["acme"])


# --- Media (Phase 9.5) -------------------------------------------------------


def _media_config(**media_kw: Any) -> ClientConfig:
    return _make_config(media=MediaConfig(**media_kw))


def _fake_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real Pillow convert with a stub that writes a stand-in JPEG at the dest."""

    def convert(data: bytes, dest: Path, *, max_dim: int = 1600, quality: int = 85) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg:" + data[:8])
        return dest

    monkeypatch.setattr(run_execute, "convert_image_for_web", convert)


def _write_video_map(
    tmp_path: Path, entries: dict[str, list[dict[str, str]]], *, on_disk: bool = True
) -> Path:
    """Write mapping.yml, and — unless ``on_disk`` is false — each file into ``v{language}/``."""
    path = tmp_path / "mapping.yml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    if on_disk:
        for language, rows in entries.items():
            folder = tmp_path / f"v{language}"
            folder.mkdir(exist_ok=True)
            for row in rows:
                (folder / row["file"]).write_bytes(b"video")
    return path


def test_hero_image_downloaded_converted_uploaded_and_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    rec = _install(monkeypatch, cfg)
    _fake_convert(monkeypatch)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert rec.downloaded == ["https://cdn/x.jpg"]
    assert len(rec.uploaded) == 1
    hero_id = rec.uploaded[0]["id"]
    kw = rec.wp[0]
    assert kw["featured_media"] == hero_id
    assert kw["acf"]["product_header_image"] == hero_id
    assert kw["acf"]["product_regular_image"] == hero_id


def test_media_is_named_by_barcode_so_a_rename_does_not_upload_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming 38 products re-uploaded 109 identical files when the title named them (2026-10-22).

    The title is still the attachment's title; the key, which names the file, is the barcode.
    """
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _media_config())
    _fake_convert(monkeypatch)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(image_url="https://cdn/x.jpg")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    (hero,) = rec.uploaded
    assert hero["key"] == GTIN_A
    assert "Rugsteun" in hero["title"]


def test_image_write_shape_url_uses_source_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(image_write_shape="url")
    rec = _install(monkeypatch, cfg)
    _fake_convert(monkeypatch)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    hero_id = rec.uploaded[0]["id"]
    kw = rec.wp[0]
    # featured_media is always the attachment id; only the ACF image fields switch to a URL.
    assert kw["featured_media"] == hero_id
    assert kw["acf"]["product_header_image"] == f"https://wp.test/wp-content/uploads/{hero_id}.jpg"


def test_missing_image_still_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="MISSING")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert rec.uploaded == []  # nothing uploaded
    kw = rec.wp[0]
    assert kw["featured_media"] is None
    assert "product_header_image" not in kw["acf"]
    assert rec.verified  # the page was still created and verified (E7)


def test_video_set_on_correct_language_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(
        tmp_path,
        {
            "nl": [{"file": "vid_nl.mp4", "gtin": GTIN_A}],
            "fr": [{"file": "vid_fr.mp4", "gtin": GTIN_A}],
        },
    )
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl"), "fr": str(tmp_path / "vfr")},
        video_map_path=str(mapping),
        video_transcode=False,  # prepare_video returns the source path unchanged
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    acf_by_lang = {kw["language"]: kw["acf"] for kw in rec.wp}
    nl_video = acf_by_lang["nl"]["product_header_video_file"]
    fr_video = acf_by_lang["fr"]["product_header_video_file"]
    assert nl_video != fr_video  # each language got its own video attachment
    # the uploaded paths were the language-correct files
    paths = {u["path"] for u in rec.uploaded}
    assert any(Path(p).as_posix().endswith("vnl/vid_nl.mp4") for p in paths)
    assert any(Path(p).as_posix().endswith("vfr/vid_fr.mp4") for p in paths)


def test_no_matching_video_leaves_field_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(tmp_path, {"nl": [], "fr": []})
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl")},
        video_map_path=str(mapping),
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert "product_header_video_file" not in rec.wp[0]["acf"]


def test_state_records_featured_media_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    rec = _install(monkeypatch, cfg)
    _fake_convert(monkeypatch)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    hero_id = rec.uploaded[0]["id"]
    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.wp_featured_media_id == hero_id


def test_media_rerun_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    rec1 = _install(monkeypatch, cfg)
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    rec2 = _install(monkeypatch, cfg)
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    # deterministic converter + content-hash dedupe → the same attachment id both runs.
    assert rec1.uploaded[0]["id"] == rec2.uploaded[0]["id"]
    assert rec2.wp[0]["featured_media"] == rec1.uploaded[0]["id"]


def test_media_uploaded_by_a_row_whose_page_write_fails_is_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan case: media on the site, referenced by no page and recorded in no state.

    Nothing else finds these. ``scripts.reconcile`` compares *pages*, and state never mentions
    an attachment whose row failed — so an orphan is invisible to every other check the tool has.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    rec = _install(monkeypatch, cfg, wp_error=WordPressAPIError(500, "boom"))
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 1

    uploaded = [u["id"] for u in rec.uploaded]
    assert uploaded  # something was uploaded before the page write failed
    assert rec.deleted_media == uploaded  # and every bit of it was taken back down


def test_a_reused_attachment_is_never_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedup means an upload often returns an earlier run's attachment. Deleting that would
    break a page that is live and correct — the failing row did not create it, so it is not
    the failing row's to remove.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    hero = 5000 + int.from_bytes(
        hashlib.sha256(str(Path("output/acme/media/images") / f"{GTIN_A}.jpg").encode()).digest()[
            :2
        ],
        "big",
    )
    rec = _install(
        monkeypatch,
        cfg,
        wp_error=WordPressAPIError(500, "boom"),
        reused_media=frozenset({hero}),  # an earlier run put this one there
    )
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 1

    # Not vacuous: the upload happened and returned exactly the id marked as pre-existing.
    assert [u["id"] for u in rec.uploaded] == [hero]
    assert rec.deleted_media == []


def test_media_is_kept_when_the_page_exists_and_only_verification_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window closes when the page write returns, not when the row does.

    ``verify_url`` fails *after* the page exists and already carries the attachments. Rolling
    back there would turn a failed row into a live page with broken media — strictly worse than
    the failure it is cleaning up after.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    rec = _install(monkeypatch, cfg, verify=False)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 1

    assert rec.wp  # the page write happened
    assert rec.deleted_media == []  # so its media stays


def test_a_failed_rollback_warns_and_keeps_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The operator needs the id to clean up by hand, and still needs to know why the row failed.
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    _install(
        monkeypatch,
        cfg,
        wp_error=WordPressAPIError(500, "boom"),
        delete_media_error=WordPressAPIError(403, "no"),
    )
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    with caplog.at_level(logging.WARNING, logger="scripts.run_execute"):
        assert run_execute.main(["acme", "--plan", str(plan)]) == 1

    assert "remove it by hand" in caplog.text
    outcome = _read_outcomes(tmp_path, newest=True)[0]
    assert "500" in outcome["error"]  # the page-write failure, not the cleanup failure


def test_a_truncated_upload_fails_the_row_instead_of_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No page at all, rather than a page whose media is a fragment.

    The media *resolution* steps degrade to ``None`` under E7 so a source problem cannot stop a
    publish. An upload that WordPress stored short is the opposite case: the file is wrong, and
    a page published against it looks entirely healthy — 200, QR resolves — until someone
    presses play.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _fake_convert(monkeypatch)
    rec = _install(
        monkeypatch,
        cfg,
        upload_error=MediaIntegrityError(
            Path("x.jpg"), 8_000_000, 1_500_000, 42, deleted=True, call="POST /wp-json/wp/v2/media"
        ),
    )
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 1
    assert rec.wp == []  # no page written
    assert load_state("acme").entries == {}  # and nothing recorded as published
    outcome = _read_outcomes(tmp_path, newest=True)[0]
    assert outcome["status"] == "error"
    assert outcome["failed_call"] == "POST /wp-json/wp/v2/media"
    assert "1500000 bytes" in outcome["error"]  # says what was stored vs sent


def test_state_and_log_record_which_video_each_page_got(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filename for a page that got one, ``""`` for one that did not — and the two differ.

    ``""`` is what lets a page published without a video come back when the client confirms one;
    a page recording nothing (``None``) would be indistinguishable from one written before the
    field existed, and never be reconsidered.
    """
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(tmp_path, {"nl": [{"file": "vid_nl.mp4", "gtin": GTIN_A}], "fr": []})
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl"), "fr": str(tmp_path / "vfr")},
        video_map_path=str(mapping),
    )
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    entries = load_state("acme").entries[GTIN_A]
    assert entries["nl"].video_file == "vid_nl.mp4"
    assert entries["fr"].video_file == ""
    (log,) = (tmp_path / "output" / "acme" / "runs").glob("*/run.jsonl")
    logged = {o["language"]: o["video_file"] for o in map(json.loads, log.read_text().splitlines())}
    assert logged == {"nl": "vid_nl.mp4", "fr": ""}


def test_a_live_run_leaves_a_note_naming_the_pages_without_a_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(tmp_path, {"nl": [{"file": "vid_nl.mp4", "gtin": GTIN_A}], "fr": []})
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl"), "fr": str(tmp_path / "vfr")},
        video_map_path=str(mapping),
    )
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    (note,) = (tmp_path / "output" / "acme" / "runs").glob("*/data-quality.md")
    text = note.read_text()
    assert f"| {GTIN_A} | fr |" in text
    assert f"| {GTIN_A} | nl |" not in text


def test_a_video_that_will_not_prepare_keeps_the_pages_record_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACF field is left alone, so the page still shows what it had; state must say so.

    The file is confirmed but absent from its folder, so ffmpeg is never asked. Recording ``""``
    would list a page that still has its old video as "without a video". The failure is this
    machine's, not the file's, so it is marked for a retry once the file is in its folder.
    """
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(
        tmp_path, {"nl": [{"file": "gone.mpg", "gtin": GTIN_A}]}, on_disk=False
    )
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl")},
        video_map_path=str(mapping),
        video_transcode=True,
    )
    _install(monkeypatch, cfg)
    monkeypatch.setattr(run_execute, "prepare_video", _must_not_prepare)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.video_failed == "gone.mpg"
    assert entry.video_failed_retry is True
    assert entry.video_file == ""  # a new page: it had no video before either
    (note,) = (tmp_path / "output" / "acme" / "runs").glob("*/data-quality.md")
    assert "| gone.mpg |" in note.read_text()


def test_a_failed_replacement_keeps_the_video_the_page_already_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACF field is not cleared, so the page still serves ``old.mp4`` — and state says so."""
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(tmp_path, {"nl": [{"file": "old.mp4", "gtin": GTIN_A}]})
    cfg = _media_config(video_folders={"nl": str(tmp_path / "vnl")}, video_map_path=str(mapping))
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    assert load_state("acme").entries[GTIN_A]["nl"].video_file == "old.mp4"  # precondition

    _write_video_map(tmp_path, {"nl": [{"file": "new.mpg", "gtin": GTIN_A}]})
    monkeypatch.setattr(run_execute, "prepare_video", lambda *a, **k: None)
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.video_file == "old.mp4"
    assert entry.video_failed == "new.mpg"
    assert entry.video_failed_retry is False  # ffmpeg read it and refused: the file's fault


def _must_not_prepare(*_a: object, **_k: object) -> None:
    raise AssertionError("a file that is not in its folder must not reach ffmpeg")


def test_no_ffmpeg_is_this_machines_failure_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing ffmpeg must be enough to get the video onto the page on the next run."""
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(tmp_path, {"nl": [{"file": "clip.mpg", "gtin": GTIN_A}]})
    cfg = _media_config(
        video_folders={"nl": str(tmp_path / "vnl")},
        video_map_path=str(mapping),
        video_transcode=True,
        ffmpeg_bin="no-such-ffmpeg-here",
    )
    _install(monkeypatch, cfg)
    monkeypatch.setattr(run_execute, "prepare_video", _must_not_prepare)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert (entry.video_failed, entry.video_failed_retry) == ("clip.mpg", True)


def test_a_dry_run_leaves_no_data_quality_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing went live, so nothing went live incomplete."""
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan), "--dry-run"]) == 0

    assert not list((tmp_path / "output" / "acme" / "runs").glob("*/data-quality.md"))


def _pilot_map(tmp_path: Path, both: list[str]) -> str:
    """Write a mapping.yml confirming each GTIN in `both` in both nl and fr; return its path."""
    entries = {
        "nl": [{"file": f"{g}_nl.mp4", "gtin": g} for g in both],
        "fr": [{"file": f"{g}_fr.mp4", "gtin": g} for g in both],
    }
    return str(_write_video_map(tmp_path, entries))


def test_pilot_restrict_blocks_unmapped_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]),  # only GTIN_A mapped in both langs
        restrict_to_mapped_gtins=True,
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(
        tmp_path / "plan.json",
        _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr"), _row(GTIN_B, "nl"), _row(GTIN_B, "fr")),
    )

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    written = {kw["meta"]["gtin"] for kw in rec.wp}
    assert written == {GTIN_A}  # GTIN_B never written — hard-blocked


def test_pilot_restrict_off_processes_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]), restrict_to_mapped_gtins=False
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert {kw["meta"]["gtin"] for kw in rec.wp} == {GTIN_A, GTIN_B}


def test_pilot_restrict_all_blocked_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]), restrict_to_mapped_gtins=True
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_B, "nl"), _row(GTIN_B, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert rec.wp == []  # nothing published


def test_publish_without_video_writes_the_unmapped_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]),
        restrict_to_mapped_gtins=True,
        publish_without_video=True,
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert {kw["meta"]["gtin"] for kw in rec.wp} == {GTIN_A, GTIN_B}


def test_two_videos_in_one_language_stay_blocked_when_publishing_without_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hard allowlist agrees with the plan: even a hand-made ``--plan`` cannot publish it."""
    monkeypatch.chdir(tmp_path)
    mapping = _write_video_map(
        tmp_path,
        {
            "nl": [{"file": "a.mp4", "gtin": GTIN_A}, {"file": "a.mpg", "gtin": GTIN_A}],
            "fr": [{"file": "a_fr.mp4", "gtin": GTIN_A}],
        },
    )
    cfg = _media_config(
        video_map_path=str(mapping), restrict_to_mapped_gtins=True, publish_without_video=True
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert {kw["meta"]["gtin"] for kw in rec.wp} == {GTIN_B}


def test_a_mapping_that_will_not_load_blocks_every_gtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist fails safe: unreadable mapping, empty allowlist, nothing written.

    Asserted rather than assumed, because the property is easy to lose. It survived only as
    long as the handler's exception tuple happened to match what the loader raised — and it did
    not: a malformed mapping raised ``yaml.YAMLError``, which ``except (OSError, ValueError)``
    never caught. Publishing the whole catalogue is one wrong except-clause away.
    """
    monkeypatch.chdir(tmp_path)
    mapping = tmp_path / "broken.yml"
    mapping.write_text(f'nl:\n  - {{file: "a.mp4", gtin: "{GTIN_A}"}}\n\tstray tab\n', "utf-8")
    cfg = _media_config(video_map_path=str(mapping), restrict_to_mapped_gtins=True)
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    assert rec.wp == [], "an unreadable allowlist must block everything, not nothing"


def test_dry_run_uploads_no_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(
        tmp_path / "plan.json", _plan(_row(GTIN_A, "nl", image_url="https://cdn/x.jpg"))
    )

    assert run_execute.main(["acme", "--plan", str(plan), "--dry-run"]) == 0

    assert rec.downloaded == []
    assert rec.uploaded == []


# --- Production write guard --------------------------------------------------


def _prod_config() -> ClientConfig:
    return _make_config(
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            account_number_production="8712345000003",
            client_id_env_production="GS1_CID",
            client_secret_env_production="GS1_SEC",
            environment="production",
            digital_link_url_pattern="https://id.gs1.org/01/{gtin14}",
        )
    )


def test_production_run_refuses_without_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _prod_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row(), _row(language="fr")))

    code = run_execute.main(["acme", "--plan", str(plan)])

    assert code == 2
    assert rec.wp == [] and rec.gs1 == []  # nothing was written
    err = capsys.readouterr().err.lower()
    assert "production" in err
    assert "--i-understand-production" in err


def test_production_run_proceeds_with_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _prod_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row(), _row(language="fr")))

    code = run_execute.main(["acme", "--plan", str(plan), "--i-understand-production"])

    assert code == 0
    assert rec.wp  # pages were written


def test_dry_run_bypasses_production_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _prod_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--dry-run"])

    assert code == 0
    assert rec.wp == [] and rec.gs1 == []  # dry-run writes nothing; ack not required


# --- --only: the three publish flows -----------------------------------------
#
# `/gs1-pages`, `/gs1-links` and `/gs1-publish` are three thin skills over one flag. The
# tests below are about what each leg does and — more importantly — what it must *not* do:
# a `--only pages` run that quietly wrote a GS1 record would be unrecoverable, since a
# Digital Link can never be deleted.


def test_only_pages_writes_no_resolver_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/gs1-pages` is the reversible flow: WordPress only, nothing permanent."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "pages"])

    assert code == 0
    assert len(rec.wp) == 2  # both pages upserted
    assert rec.translations  # and linked as translations — that is a WordPress write
    assert rec.gs1 == []  # nothing permanent happened
    assert not (tmp_path / "output" / "acme" / "qr").exists()  # QR belongs to the links leg
    outcomes = _read_outcomes(tmp_path)
    assert [o["status"] for o in outcomes] == ["ok", "ok"]
    assert [o["gs1_set"] for o in outcomes] == [False, False]


def test_only_pages_records_state_with_an_empty_link_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty hash is the record of "page published, resolver link never written".

    Without it the next ``run_plan`` reads a matching ``content_hash``, classifies the row
    UNCHANGED, and a follow-up ``/gs1-links`` finds nothing to publish — silently.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    entry = load_state("acme").entries[GTIN_A]["nl"]
    assert entry.gs1_link_set_hash == ""
    assert entry.content_hash == "hash-" + GTIN_A  # the page half really was written
    assert entry.wp_page_id == _page_id(f"p-{GTIN_A}")


def test_only_pages_does_not_blank_an_existing_link_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running pages over a fully published product must not look like the link vanished."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    published = load_state("acme").entries[GTIN_A]["nl"].gs1_link_set_hash
    assert published  # a real digest

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    assert load_state("acme").entries[GTIN_A]["nl"].gs1_link_set_hash == published


def test_only_links_touches_no_wordpress_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/gs1-links` points the resolver at pages that already exist; it writes none."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    # A prior run published the page, so state knows where it lives.
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    rec.wp.clear()
    rec.translations.clear()
    rec.verified.clear()

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    assert rec.wp == []  # no page upserted
    assert rec.translations == []  # translation linking is a WordPress write
    assert len(rec.gs1) == 1
    assert rec.gs1[0]["links"][0]["target_url"] == f"https://wp.test/product/p-{GTIN_A}/"
    assert (tmp_path / "output" / "acme" / "qr" / f"{GTIN_A}.svg").is_file()
    # The target came from state, not from a page written just now, so it was verified.
    assert rec.verified == [f"https://wp.test/product/p-{GTIN_A}/"]


def test_pages_then_links_lands_the_same_state_as_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-step flow converges on what `/gs1-publish` writes in one go."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0
    in_one_go = _entry_without_timestamp(load_state("acme"))

    (tmp_path / "output" / "acme" / "state.json").unlink()
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0

    assert _entry_without_timestamp(load_state("acme")) == in_one_go


def _scan_check(tmp_path: Path) -> list[dict[str, Any]]:
    runs = sorted((tmp_path / "output" / "acme" / "runs").iterdir())
    return json.loads((runs[-1] / "scan-check.json").read_text(encoding="utf-8"))


def test_a_links_run_checks_every_language_lands_on_its_own_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the write, each language is scanned the way a phone does and compared to its page.

    The check exists because a written record said nothing about where a French phone lands:
    38 records sent it to the Dutch page for weeks and every run reported ``ok``.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert rec.scans == []  # a pages run writes no record, so there is nothing to scan

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    uri = f"https://id.gs1.org/01/{GTIN_A}"
    assert sorted(rec.scans) == [(uri, "fr"), (uri, "nl")]
    check = _scan_check(tmp_path)
    assert [(c["language"], c["ok"]) for c in check] == [("fr", True), ("nl", True)]
    assert "all 2 scan(s) land on their own page" in capsys.readouterr().err
    outcomes = {o["language"]: o for o in _read_outcomes(tmp_path, newest=True)}
    assert outcomes["fr"]["resolver_target"] == check[0]["expected"]


def test_a_scan_landing_on_the_other_language_warns_but_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record is written and the page serves: a wrong landing is a finding, not an error."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr")))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    nl_page = load_state("acme").entries[GTIN_A]["nl"].wp_url
    rec = _install(monkeypatch, cfg, resolver=lambda _uri, _language: (307, nl_page))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    err = capsys.readouterr().err
    assert "WARNING scan check: 1 of 2 scan(s) do not land on their own page" in err
    assert f"{GTIN_A} (fr): expected" in err
    assert len(rec.scans) > 2  # the wrong one was asked again before it was reported
    assert {c["language"]: c["ok"] for c in _scan_check(tmp_path)} == {"nl": True, "fr": False}


def test_a_dry_run_scans_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan), "--dry-run"]) == 0

    assert rec.scans == []


def test_only_links_revive_clears_the_retraction_it_just_undid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted ``run_unpublish``: resolver retracted, pages never drafted.

    ``safe_upsert(is_enabled=True)`` writes the record back, so after this the product is
    fully live. ``_commit_state`` used to update the hash and leave ``retracted`` set, so
    the row classified HELD on every later run and only ``--revive`` got it through — each
    time, forever. Driven through ``main`` rather than by copying the entry by hand: the
    claim is about what the command writes.
    """
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    state = load_state("acme")
    entry = state.entries[GTIN_A]["nl"]
    assert entry.retracted is False  # precondition: a pages run records no retraction
    state.entries[GTIN_A]["nl"] = entry.model_copy(
        update={"retracted": True, "gs1_link_set_hash": ""}
    )
    save_state(state)

    held = _write_json(tmp_path / "held.json", _plan(_held(_row())))
    code = run_execute.main(["acme", "--plan", str(held), "--only", "links", "--revive"])

    assert code == 0
    written = load_state("acme").entries[GTIN_A]["nl"]
    assert written.retracted is False, "the resolver record was written back and enabled"
    assert written.gs1_link_set_hash


def test_only_links_revive_leaves_a_drafted_product_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the OR, and why clearing the flag above is safe.

    Writing the resolver record back says nothing about the pages. A fully unpublished
    product revived one leg at a time must stay held on ``wp_status`` until the pages are
    published too.
    """
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    state = load_state("acme")
    entry = state.entries[GTIN_A]["nl"]
    state.entries[GTIN_A]["nl"] = entry.model_copy(
        update={"retracted": True, "wp_status": "draft", "gs1_link_set_hash": ""}
    )
    save_state(state)

    held = _write_json(tmp_path / "held.json", _plan(_held(_row())))
    assert run_execute.main(["acme", "--plan", str(held), "--only", "links", "--revive"]) == 0

    assert load_state("acme").entries[GTIN_A]["nl"].wp_status == "draft"


def test_only_links_refuses_a_target_that_does_not_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the precondition: no permanent record aimed at a 404.

    A GS1 record can never be deleted, so this refusal is the last chance to stop a QR
    being printed against a dead URL. The healthy GTIN alongside it must still publish.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    dead = f"https://wp.test/product/p-{GTIN_A}/"
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A), _row(GTIN_B)))

    # Both pages published fine; GTIN_A's went dark afterwards (deleted, drafted, renamed).
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    rec = _install(monkeypatch, cfg, unverifiable=(dead,))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 1
    assert [entry["gtin"] for entry in rec.gs1] == [GTIN_B]  # only the healthy GTIN
    outcomes = {o["gtin"]: o for o in _read_outcomes(tmp_path, newest=True)}
    assert outcomes[GTIN_A]["status"] == "error"
    assert "refusing to point a permanent GS1 record" in outcomes[GTIN_A]["error"]
    assert outcomes[GTIN_B]["status"] == "ok"
    # The blocked GTIN keeps its empty link hash, so the next plan still offers to finish it.
    assert load_state("acme").entries[GTIN_A]["nl"].gs1_link_set_hash == ""


def test_only_links_finds_an_unmanaged_page_by_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no state, the page is located live — and still verified before the write."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg, findable_slugs={(f"p-{GTIN_A}", "nl"): 4242})
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    assert rec.slug_lookups == [{"post_type": "product", "slug": f"p-{GTIN_A}", "language": "nl"}]
    assert rec.gs1[0]["links"][0]["target_url"] == f"https://wp.test/product/p-{GTIN_A}/"
    assert rec.verified == [f"https://wp.test/product/p-{GTIN_A}/"]
    # State stays empty: this tool did not publish that page, so it cannot claim its content.
    assert load_state("acme").entries == {}


def test_only_links_falls_back_to_the_planned_target_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site whose slugs don't match `slug_pattern` is the ordinary pre-existing case."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)  # nothing findable, no state
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    assert rec.slug_lookups  # the lookup was attempted first
    # PlanRow.target_url, built by diff_against_state from wordpress.target_url_pattern.
    assert rec.gs1[0]["links"][0]["target_url"] == f"https://wp.test/product/p-{GTIN_A}/"
    assert rec.verified == [f"https://wp.test/product/p-{GTIN_A}/"]
    assert load_state("acme").entries == {}


def test_only_links_refuses_an_unverifiable_planned_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is the most dangerous source, so it is the one that must fail closed."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    dead = f"https://wp.test/product/p-{GTIN_A}/"
    rec = _install(monkeypatch, cfg, unverifiable=(dead,))
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 1
    assert rec.gs1 == []
    assert load_state("acme").entries == {}


def test_both_flow_verifies_a_language_rebuilt_from_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A language not written this run is used unverified no longer.

    ``_known_pages`` rebuilds it from ``state.json``, which records where a page *was* — the
    resolver link is then aimed at a URL nothing checked. One code path now covers this and
    the ``--only links`` case.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    both = _plan(_row(GTIN_A, "nl"), _row(GTIN_A, "fr"))
    nl_only = _write_json(tmp_path / "nl.json", _plan(_row(GTIN_A, "nl")))
    assert run_execute.main(["acme", "--plan", str(nl_only)]) == 0
    rec.verified.clear()

    confirmed = tmp_path / "confirmed.json"
    confirmed.write_text(
        json.dumps(
            {"plan": both.model_dump(mode="json"), "confirmed_gtins_by_lang": [[GTIN_A, "fr"]]}
        ),
        encoding="utf-8",
    )
    assert run_execute.main(["acme", "--confirmed", str(confirmed)]) == 0

    # fr was written and verified by the pages leg; nl came from state and was verified too.
    assert sorted(rec.verified) == sorted(
        [_page_url("fr", f"p-{GTIN_A}"), _page_url("nl", f"p-{GTIN_A}")]
    )


def test_dry_run_only_links_renders_nothing_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run builds no clients, so it cannot verify — it says so instead."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--dry-run", "--only", "links"])

    assert code == 0
    assert rec.wp == [] and rec.gs1 == [] and rec.verified == []
    assert load_state("acme").entries == {}
    assert [o["status"] for o in _read_outcomes(tmp_path)] == ["dry-run"]


def test_production_refusal_for_pages_claims_no_permanent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--only pages` writes live pages but creates nothing permanent; the message must agree.

    An operator who reads "permanent GS1 records" on a run that creates none learns to read
    past this message — which is the one thing the guard cannot afford.
    """
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _prod_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "pages"])

    assert code == 2
    err = capsys.readouterr().err
    assert "live pages" in err
    assert "permanent GS1 records" not in err
    assert "--i-understand-production" in err  # still gated: it is a live site


# --- The run log -------------------------------------------------------------


def test_a_crash_mid_run_keeps_the_rows_that_already_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that dies part-way leaves the completed rows on disk, not an empty file.

    The log used to be written once, after every GTIN had finished, so anything that killed
    the process discarded the whole record — including rows already committed to
    ``state.json``. The operator was then left with live pages, permanent GS1 records, and
    no account of which ones.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl"), _row(GTIN_B, "nl")))

    real = run_execute._execute_gtin

    def die_on_b(cfg_: Any, gtin: str, *args: Any, **kw: Any) -> Any:
        if gtin == GTIN_B:
            raise KeyboardInterrupt  # stands in for any hard stop: ^C, OOM, a killed shell
        return real(cfg_, gtin, *args, **kw)

    monkeypatch.setattr(run_execute, "_execute_gtin", die_on_b)

    with pytest.raises(KeyboardInterrupt):
        run_execute.main(["acme", "--plan", str(plan)])

    outcomes = _read_outcomes(tmp_path)
    assert [(o["gtin"], o["status"]) for o in outcomes] == [(GTIN_A, "ok")]


def test_the_log_path_is_announced_before_the_run_not_only_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The path is printed at the start too, so it can be tailed while the run is in flight.

    It is derived from a timestamp only this process knows, so a parent that is not told
    cannot work out where the run is reporting to — and a run that dies never reaches the
    closing line.
    """
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    # First and last, not exactly two: a links run reports its scan check in between.
    lines = capsys.readouterr().err.strip().splitlines()
    opening, closing = lines[0], lines[-1]
    logs = list((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"))
    assert str(logs[0].relative_to(tmp_path)) in opening
    assert "error(s)" not in opening  # the count is not known yet — do not imply it is
    assert "1 row(s), 0 error(s)" in closing


def test_two_runs_in_the_same_second_do_not_share_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filename is a timestamp to the second; two runs inside one must not interleave.

    This does not make concurrent runs supported (E20) — ``state.json`` still races. It
    stops one run's log from being scrambled by another's, which incremental writing would
    otherwise make routine rather than rare.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _make_config()
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row()))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0

    logs = sorted((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"), key=_mtime)
    assert len(logs) == 2
    assert all(len(path.read_text().splitlines()) == 1 for path in logs)
    # And **both are visible**. The collision used to suffix the log inside one directory —
    # `{stamp}/run-1.jsonl` — which `iter_logs` matches with neither of its globs, so the second
    # run vanished from the Runs screen and from `newest_log`. Two runs in one second is exactly
    # what a re-run after a failure looks like, and that run is the one an operator most needs to
    # see: live pages and permanent records may already exist for the rows that landed.
    seen = sorted(path.resolve() for path in iter_logs("acme"))
    assert seen == sorted(path.resolve() for path in logs), (
        "a same-second run must not be invisible"
    )
    assert len({stamp_of(path) for path in logs}) == 2, "and must not share the first one's label"
    for path in logs:
        assert sibling(path, SOURCES_NAME).parent == path.parent, (
            "each run's own documents belong beside its own log"
        )


def _read_outcomes(tmp_path: Path, *, newest: bool = False) -> list[dict[str, Any]]:
    """The RunOutcome dicts from the run log (the newest one, when several runs happened).

    Ordered by mtime, not by name: two runs inside one test start in the same second, so the
    second one lands in the collision-suffixed directory ``{stamp}-1/`` — which sorts *before*
    ``{stamp}/`` lexicographically, because ``-`` precedes the path separator.
    """
    logs = sorted((tmp_path / "output" / "acme" / "runs").glob("**/*.jsonl"), key=_mtime)
    path = logs[-1] if newest else logs[0]
    return [json.loads(line) for line in path.read_text().splitlines()]


def _mtime(path: Path) -> int:
    return path.stat().st_mtime_ns


# --- E21 at execute time ------------------------------------------------------
#
# Copy is written per run for the rows a run executes, so an UNCHANGED row legitimately carries
# none. Both interactive surfaces already refuse to confirm UNCHANGED, but `--plan` confirms every
# row in the file (docs/setup.md and the pilot handoff both use it), and a hand-written or stale
# plan.json can carry anything. The module docstring has claimed "E21 still applies on top" since
# `--only` was added; nothing enforced it until here.


def _with_copy(row: PlanRow) -> PlanRow:
    """The row as `run_plan` builds it for a client with a generator: copy already merged on."""
    product = row.product.model_copy(
        update={"generated_tagline": LocalisedText(values={row.language: "Slogan"})}
    )
    return row.model_copy(update={"product": product})


def _unchanged(row: PlanRow) -> PlanRow:
    return row.model_copy(update={"classification": PlanClassification.UNCHANGED})


def test_a_row_with_no_generated_copy_is_dropped_when_a_generator_is_configured() -> None:
    rows = [_unchanged(_row(GTIN_A)), _with_copy(_row(GTIN_B))]

    kept = run_execute._drop_without_copy(rows, generator_configured=True)

    assert [row.gtin for row in kept] == [GTIN_B]


def test_dropping_a_copy_less_row_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """Silently is how a page goes blank. The drop is correct; being quiet about it is not."""
    with caplog.at_level("WARNING"):
        run_execute._drop_without_copy([_unchanged(_row(GTIN_A))], generator_configured=True)

    assert GTIN_A in caplog.text
    assert "no generated copy" in caplog.text


def test_a_generator_less_client_keeps_every_row() -> None:
    """A blank tagline is the normal condition there, so the guard must not fire at all.

    The same condition `run_plan` derives E21 from (`cfg.generator is not None`). Read without it,
    this guard would drop every row of every client that publishes feed copy only — a run that
    does nothing and reports success.
    """
    rows = [_row(GTIN_A), _row(GTIN_B)]

    assert run_execute._drop_without_copy(rows, generator_configured=False) == rows


def test_the_guard_is_per_language_not_per_gtin() -> None:
    """A tagline for nl says nothing about fr, and the row being executed is one language."""
    row = _row(GTIN_A, "fr")
    nl_only = row.model_copy(
        update={
            "product": row.product.model_copy(
                update={"generated_tagline": LocalisedText(values={"nl": "Slogan"})}
            )
        }
    )

    assert run_execute._drop_without_copy([nl_only], generator_configured=True) == []


def test_a_copy_less_row_never_reaches_wordpress_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--plan` confirms every row in the file, so this is the path the guard exists for."""
    monkeypatch.chdir(tmp_path)
    rec = _install(
        monkeypatch,
        _make_config(generator=GeneratorConfig(enabled=True)),
        alternates={(_OLD_NL, "fr"): _OLD_FR},
    )
    path = _write_json(
        tmp_path / "plan.json", _plan(_unchanged(_row(GTIN_A)), _with_copy(_row(GTIN_B)))
    )

    code = run_execute.main(["acme", "--plan", str(path)])

    assert code == 0
    assert [c["meta"]["gtin"] for c in rec.wp] == [GTIN_B]
    assert set(load_state("acme").entries) == {GTIN_B}


# --- A page the operator listed (process_list.target_url_column) ---------------
# Older product pages live under their own slugs (/democlient/notenkraker-2/), so neither state
# nor `slug_pattern` can find them. The selection list's `Link naar site` names the nl page — the
# record's default link, so every scan resolves to it — and fr is whatever translation that page
# itself advertises, when it advertises one.

_OLD_NL = "https://wp.test/product/rugsteun-oud/"
_OLD_FR = "https://wp.test/fr/product/rugsteun-oud/"


def _listed(row: PlanRow, url: str = _OLD_NL) -> PlanRow:
    return row.model_copy(update={"listed_url": url})


def test_only_links_points_gs1_at_the_listed_page_and_its_own_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), alternates={(_OLD_NL, "fr"): _OLD_FR})
    plan = _write_json(
        tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl")), _listed(_row(GTIN_A, "fr")))
    )

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    assert rec.wp == []
    links = {link["language"]: link for link in rec.gs1[0]["links"]}
    assert {lang: link["target_url"] for lang, link in links.items()} == {
        "nl": _OLD_NL,
        "fr": _OLD_FR,
    }
    # nl is the default link, so every scan resolves to the Dutch page.
    assert links["nl"]["default_link_type"] is True
    assert links["fr"]["default_link_type"] is False
    assert sorted(rec.verified) == sorted([_OLD_NL, _OLD_FR])  # both checked before the write
    # A page this tool does not manage gets no state, exactly as for any unmanaged page.
    assert GTIN_A not in load_state("acme").entries


def test_a_listed_page_with_no_french_translation_gets_the_nl_link_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """notenkraker-2: no fr hreflang. Its QR still has somewhere to go — the Dutch page."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    plan = _write_json(
        tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl")), _listed(_row(GTIN_A, "fr")))
    )

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "links"])

    assert code == 0
    links = rec.gs1[0]["links"]
    assert [(link["language"], link["target_url"]) for link in links] == [("nl", _OLD_NL)]
    assert links[0]["default_link_type"] is True
    assert rec.alternate_reads == [(_OLD_NL, "fr")]
    assert "names no fr translation" in caplog.text


def test_confirming_only_the_nl_row_of_a_listed_page_writes_nl_without_reading_its_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), alternates={(_OLD_NL, "fr"): _OLD_FR})
    plan = _write_json(tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl"))))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0
    assert [link["target_url"] for link in rec.gs1[0]["links"]] == [_OLD_NL]
    assert rec.alternate_reads == []


def test_confirming_only_the_fr_row_of_a_listed_page_still_links_nl_as_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), alternates={(_OLD_NL, "fr"): _OLD_FR})
    plan = _write_json(tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "fr"))))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0
    links = {link["language"]: link for link in rec.gs1[0]["links"]}
    assert links["nl"]["target_url"] == _OLD_NL
    assert links["nl"]["default_link_type"] is True
    assert links["fr"]["target_url"] == _OLD_FR


def test_a_listed_page_that_does_not_serve_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), unverifiable=(_OLD_NL,))
    plan = _write_json(tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl"))))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 1
    assert rec.gs1 == []


def test_a_listed_page_that_differs_from_the_tools_own_page_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two answers to one permanent record: stop, and say which two."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    plain = _write_json(tmp_path / "plain.json", _plan(_row(GTIN_A, "nl")))
    assert run_execute.main(["acme", "--plan", str(plain), "--only", "pages"]) == 0
    listed = _write_json(tmp_path / "listed.json", _plan(_listed(_row(GTIN_A, "nl"))))

    code = run_execute.main(["acme", "--plan", str(listed), "--only", "links"])

    assert code == 1
    assert rec.gs1 == []
    error = _read_outcomes(tmp_path, newest=True)[0]["error"]
    assert _OLD_NL in error and "already published its own" in error


def test_a_slug_findable_page_that_differs_from_the_listed_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(), findable_slugs={(f"p-{GTIN_A}", "nl"): 77})
    plan = _write_json(tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl"))))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 1
    assert rec.gs1 == []


def test_a_listed_page_that_is_the_tools_own_takes_the_ordinary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is in dispute, so state's page (with its id) is used and state is kept."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    own = f"https://wp.test/product/p-{GTIN_A}"  # no trailing slash: still the same page
    plan = _write_json(tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl"), own)))
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0
    assert rec.gs1[0]["links"][0]["target_url"] == f"{own}/"
    assert load_state("acme").entries[GTIN_A]["nl"].gs1_link_set_hash


@pytest.mark.parametrize("only", [["--only", "pages"], []])
def test_a_run_that_writes_pages_refuses_a_gtin_with_a_different_listed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, only: list[str]
) -> None:
    """Publishing p-{gtin} beside the listed page would leave the product with two pages."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    plan = _write_json(
        tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl")), _row(GTIN_B, "nl"))
    )

    code = run_execute.main(["acme", "--plan", str(plan), *only])

    assert code == 1
    assert [c["meta"]["gtin"] for c in rec.wp] == [GTIN_B]  # the other GTIN is unaffected
    assert all(write["gtin"] != GTIN_A for write in rec.gs1)


def test_a_links_only_plan_is_refused_by_a_mode_that_writes_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config())
    plan = _plan(_row(GTIN_A)).model_copy(update={"links_only": True})
    path = _write_json(tmp_path / "plan.json", plan)

    assert run_execute.main(["acme", "--plan", str(path), "--only", "pages"]) == 2
    assert run_execute.main(["acme", "--plan", str(path)]) == 2
    assert rec.wp == [] and rec.gs1 == []
    assert "--links-only" in capsys.readouterr().err


def test_a_links_run_keeps_a_row_with_no_generated_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E21 protects a page; a links run renders none, and the link title is the product name."""
    monkeypatch.chdir(tmp_path)
    rec = _install(
        monkeypatch,
        _make_config(generator=GeneratorConfig(enabled=True)),
        alternates={(_OLD_NL, "fr"): _OLD_FR},
    )
    plan = _plan(_listed(_row(GTIN_A, "nl")), _listed(_row(GTIN_A, "fr")))
    path = _write_json(tmp_path / "plan.json", plan.model_copy(update={"links_only": True}))

    assert run_execute.main(["acme", "--plan", str(path), "--only", "links"]) == 0
    assert len(rec.gs1) == 1


def test_a_dry_run_names_the_listed_page_as_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, _make_config())
    plan = _write_json(
        tmp_path / "plan.json", _plan(_listed(_row(GTIN_A, "nl")), _listed(_row(GTIN_A, "fr")))
    )

    with caplog.at_level("INFO"):
        code = run_execute.main(["acme", "--plan", str(plan), "--dry-run", "--only", "links"])

    assert code == 0
    assert f"/nl: would point GS1 https://id.gs1.org/01/{GTIN_A} at the listed page {_OLD_NL}" in (
        caplog.text
    )
    # Operator, 2026-10-09: the French link goes to the French version the page names. The dry
    # run used to say "one nl link for every language", which is what the real run does not do.
    assert f"the fr version the listed page {_OLD_NL} names" in caplog.text
    assert "for every language" not in caplog.text


# --- What a run keeps beside its log -----------------------------------------


def test_a_run_keeps_both_the_selection_it_consumed_and_the_upload_it_came_from(
    tmp_path: Path,
) -> None:
    """Both, because they answer different questions — and the second one silently never landed.

    ``_keep_selection`` asked for ``selection/uploaded.xlsx``, a name nothing has written since
    ``uploads/`` and ``selection/`` were split apart. Its ``is_file`` skip meant the copy simply
    did not happen, on every run, and the only symptom was a result sheet that said "no uploaded
    list archived" — which reads like a client who has never used the upload button.
    """
    # Arrange: the live selection, and the upload it was pruned from, where the shell files them.
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    control.parent.mkdir(parents=True)
    control.write_bytes(b"the ticked list")
    upload = archive_path(control)
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"the list as it was sent")
    cfg = _make_config(process_list=ProcessListConfig(path=str(control)))
    log = tmp_path / "output" / "acme" / "runs" / "20260927T101500Z" / "run.jsonl"
    log.parent.mkdir(parents=True)

    # Act
    run_execute._keep_selection(cfg, log, mode="both", dry_run=False)

    # Assert
    assert (log.parent / SELECTION_NAME).read_bytes() == b"the ticked list"
    assert (log.parent / UPLOAD_NAME).read_bytes() == b"the list as it was sent", (
        "the upload the selection came from is the only thing that can name a dropped row"
    )


def test_a_run_whose_client_never_uploaded_a_list_still_keeps_the_selection(
    tmp_path: Path,
) -> None:
    """No archived upload is an ordinary state — a hand-placed list, or the CLI path."""
    # Arrange
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    control.parent.mkdir(parents=True)
    control.write_bytes(b"the ticked list")
    cfg = _make_config(process_list=ProcessListConfig(path=str(control)))
    log = tmp_path / "output" / "acme" / "runs" / "20260927T101500Z" / "run.jsonl"
    log.parent.mkdir(parents=True)

    # Act
    run_execute._keep_selection(cfg, log, mode="both", dry_run=False)

    # Assert
    assert (log.parent / SELECTION_NAME).exists()
    assert not (log.parent / UPLOAD_NAME).exists()
    assert not archive_path(control).parent.exists(), (
        "asking where the upload lives must not create input/uploads/ — a run writes to output/"
    )


def test_a_run_records_which_export_its_ticks_were_chosen_against(tmp_path: Path) -> None:
    """The answer to "which export did that run use?", which nothing could give before.

    The copies beside the log say which *rows* ran. They cannot say which export those ticks were
    chosen against, because a selection carries no trace of it — and `input/` has moved on by the
    time anybody asks.
    """
    # Arrange: an export and a list, both recorded as uploads, and a selection saved against them.
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    control.parent.mkdir(parents=True)
    control.write_bytes(b"the ticked list")
    export = tmp_path / "input" / "acme" / "process" / "uploads" / "GS1 export" / "export.xlsx"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"exported")
    dated = export.with_name("export-20260927T101500.xlsx")
    dated.write_bytes(export.read_bytes())
    ledger = history_path(export)
    record_upload(ledger, "export", kept=dated, given_name="GDSN_export_2026-09-27.xlsx", rows=12)

    cfg = _make_config(
        export=ExportConfig(path=str(export)),
        process_list=ProcessListConfig(path=str(control)),
    )
    log = tmp_path / "output" / "acme" / "runs" / "20260927T101500Z" / "run.jsonl"
    log.parent.mkdir(parents=True)

    # Act
    run_execute._keep_selection(cfg, log, mode="both", dry_run=False)

    # Assert
    recorded = read_run(log.parent / SOURCES_NAME)
    assert recorded is not None
    assert recorded.dry_run is False
    assert recorded.mode == "both"
    assert recorded.sources["export"].name == "export-20260927T101500.xlsx"
    assert recorded.sources["export"].given_name == "GDSN_export_2026-09-27.xlsx"
    assert recorded.sources["selection"].sha256, "the selection is identified even with no record"


def test_a_dry_run_says_so_in_its_record(tmp_path: Path) -> None:
    """A rehearsal reads the same selection as a real publish and publishes nothing.

    Conflating the two would report a draft selection as finished, which is the direction that costs
    something.
    """
    # Arrange
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    control.parent.mkdir(parents=True)
    control.write_bytes(b"the ticked list")
    export = tmp_path / "input" / "acme" / "process" / "uploads" / "GS1 export" / "export.xlsx"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"exported")
    cfg = _make_config(
        export=ExportConfig(path=str(export)),
        process_list=ProcessListConfig(path=str(control)),
    )
    log = tmp_path / "output" / "acme" / "runs" / "20260927T101500Z" / "run.jsonl"
    log.parent.mkdir(parents=True)

    # Act
    run_execute._keep_selection(cfg, log, mode="pages", dry_run=True)

    # Assert
    recorded = read_run(log.parent / SOURCES_NAME)
    assert recorded is not None
    assert recorded.dry_run is True
    assert recorded.mode == "pages"


def test_a_legacy_run_with_no_record_reads_as_none_not_as_an_error(tmp_path: Path) -> None:
    assert read_run(tmp_path / "runs" / "20260819T234001Z" / SOURCES_NAME) is None


def test_a_real_run_writes_its_own_result_sheet_and_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, through ``main``: the run's directory holds all four of its own documents.

    The sheet used to be built by the shell after streaming the run, so a publish driven from
    anywhere else produced none — and the moment it is most wanted is the moment a run has
    half-failed.
    """
    # Arrange: a client in the shipped layout, with a selection and a parsed catalogue.
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    _write_scope_list(control, [GTIN_A])
    export = tmp_path / "input" / "acme" / "process" / "uploads" / "GS1 export" / "export.xlsx"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"exported")
    products = tmp_path / "output" / "acme" / "data" / "products.json"
    products.parent.mkdir(parents=True)
    products.write_text(json.dumps([_product().model_dump(mode="json")]), encoding="utf-8")
    cfg = _make_config(
        export=ExportConfig(path=str(export)),
        process_list=ProcessListConfig(path=str(control)),
    )
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    # Act
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    # Assert
    run_dir = next((tmp_path / "output" / "acme" / "runs").glob("*/"))
    assert (run_dir / "run.jsonl").is_file(), "what it did"
    assert (run_dir / SOURCES_NAME).is_file(), "what it read"
    assert (run_dir / SELECTION_NAME).is_file(), "the rows it ran"
    assert (run_dir / RESULT_NAME).is_file(), "the per-SKU outcome, without anyone asking"
    assert (run_dir / "issues.pdf").is_file(), "the client's short report"
    assert (run_dir / "issues.xlsx").is_file()


@pytest.mark.parametrize("saved_again", [False, True])
def test_the_issue_report_covers_what_was_ticked_when_the_ticks_match_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_again: bool
) -> None:
    """Operator, 2026-10-09: the report after a run is about the ticked products, as on Data.

    Next saves only the products that can run, so the ticked one that could not is known only from
    the snapshot saved beside the selection — and only while that selection is unchanged.
    """
    # Arrange
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    _write_scope_list(control, [GTIN_A])
    missing = "00290000000999"
    ticked_snapshot.write(
        control,
        [(GTIN_A, "Rugsteun"), (missing, "Onbekend")],
        selection_issues([(missing, "Onbekend")], set(), Eligibility()),
    )
    if saved_again:
        _write_scope_list(control, [GTIN_A, GTIN_B])
    products = tmp_path / "output" / "acme" / "data" / "products.json"
    products.parent.mkdir(parents=True)
    products.write_text(json.dumps([_product().model_dump(mode="json")]), encoding="utf-8")
    cfg = _make_config(process_list=ProcessListConfig(path=str(control)))
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    # Act
    assert run_execute.main(["acme", "--plan", str(plan)]) == 0

    # Assert
    run_dir = next((tmp_path / "output" / "acme" / "runs").glob("*/"))
    failed = [
        row[0]
        for row in openpyxl.load_workbook(run_dir / "issues.xlsx")["Failed products"].iter_rows(
            min_row=2, values_only=True
        )
    ]
    if saved_again:
        assert not (run_dir / TICKED_NAME).exists(), "another batch's ticks are not borrowed"
        assert missing not in failed
    else:
        assert (run_dir / TICKED_NAME).is_file()
        assert missing in failed, "the ticked product that could not run is in the report"


def test_a_result_sheet_that_cannot_be_built_does_not_change_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs after live pages and permanent GS1 records already exist.

    A report that raised here would turn a publish that worked into one that reads as failed, with
    no way to tell which from the outside.
    """
    # Arrange: no products.json, so building the sheet raises.
    monkeypatch.chdir(tmp_path)
    control = tmp_path / "input" / "acme" / "process" / "selection" / "selections.xlsx"
    _write_scope_list(control, [GTIN_A])
    cfg = _make_config(process_list=ProcessListConfig(path=str(control)))
    _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_A, "nl")))

    # Act
    code = run_execute.main(["acme", "--plan", str(plan)])

    # Assert
    assert code == 0, "a missing report must not report the publish as failed"
    run_dir = next((tmp_path / "output" / "acme" / "runs").glob("*/"))
    assert not (run_dir / RESULT_NAME).exists()
    assert (run_dir / "run.jsonl").is_file(), "the run's own record is untouched by the report"


def _write_scope_list(path: Path, gtins: list[str]) -> None:
    import openpyxl  # noqa: PLC0415 — only this helper needs it

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Barcode"])
    for gtin in gtins:
        sheet.append([gtin])
    workbook.save(path)


def test_a_links_run_is_not_stopped_by_the_video_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The video gate is about a page; a links run writes none (operator, 2026-10-09).

    Its one precondition is a target that serves, which ``_verify_targets`` still checks.
    """
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]), restrict_to_mapped_gtins=True
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_B, "nl")))
    # The page exists already: a pages run with the gate off put it there.
    open_cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]), restrict_to_mapped_gtins=False
    )
    _install(monkeypatch, open_cfg)
    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0
    rec = _install(monkeypatch, cfg)

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "links"]) == 0

    assert [call["links"][0]["target_url"] for call in rec.gs1] == [
        f"https://wp.test/product/p-{GTIN_B}/"
    ]
    assert rec.verified, "the target was still checked before the write"


def test_a_pages_run_still_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _media_config(
        video_map_path=_pilot_map(tmp_path, [GTIN_A]), restrict_to_mapped_gtins=True
    )
    rec = _install(monkeypatch, cfg)
    plan = _write_json(tmp_path / "plan.json", _plan(_row(GTIN_B, "nl")))

    assert run_execute.main(["acme", "--plan", str(plan), "--only", "pages"]) == 0

    assert rec.wp == []


# --- A live page planned without copy keeps its text ---------------------------------------
# Copy is optional for a page already live (operator, 2026-10-22): a new title goes out without new
# copy, and the page keeps the tagline and description it carries.


def _keeps_live(row: PlanRow) -> PlanRow:
    return row.model_copy(
        update={"classification": PlanClassification.CHANGED, "keeps_live_copy": True}
    )


def test_a_row_that_keeps_its_live_text_survives_the_copy_guard() -> None:
    row = _keeps_live(_row(GTIN_A))

    assert run_execute._drop_without_copy([row], generator_configured=True) == [row]


def test_a_live_page_without_new_copy_is_written_without_its_copy_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload omits the copy fields, so WordPress leaves the live ones as they are."""
    monkeypatch.chdir(tmp_path)
    cfg = _make_config(generator=GeneratorConfig(enabled=True))
    cfg = cfg.model_copy(
        update={
            "wordpress": cfg.wordpress.model_copy(
                update={"acf_map": {"product_title": "generated_tagline"}}
            )
        }
    )
    rec = _install(monkeypatch, cfg)
    first = _write_json(tmp_path / "first.json", _plan(_with_copy(_row())))
    assert run_execute.main(["acme", "--plan", str(first), "--only", "pages"]) == 0
    assert rec.wp[-1]["acf"]["product_title"] == "Slogan"  # precondition: copy was written once

    renamed = _keeps_live(_row()).model_copy(update={"title": "Dizzy Dog"})
    second = _write_json(tmp_path / "second.json", _plan(renamed))
    assert run_execute.main(["acme", "--plan", str(second), "--only", "pages"]) == 0

    assert rec.wp[-1]["title"] == "Dizzy Dog"
    assert "product_title" not in (rec.wp[-1]["acf"] or {})


def test_keeping_live_text_is_refused_for_a_page_the_ledger_does_not_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that says "keep the text" about a page that is not there would publish a blank one."""
    monkeypatch.chdir(tmp_path)
    rec = _install(monkeypatch, _make_config(generator=GeneratorConfig(enabled=True)))
    plan = _write_json(tmp_path / "plan.json", _plan(_keeps_live(_row())))

    code = run_execute.main(["acme", "--plan", str(plan), "--only", "pages"])

    assert code != 0
    assert rec.wp == []
    assert GTIN_A not in load_state("acme").entries
