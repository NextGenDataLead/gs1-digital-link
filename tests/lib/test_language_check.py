"""Tests for the cross-language check (``lib.language_check``) — report §4e's data.

What is pinned: which fields are compared (every localised source, both maps), that the values are
read raw, that "missing" is decided from the record and never stored, that a kept judgment is only
shown while its fingerprint matches the export, and that a producer cannot add a finding about a
field or language it was never shown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.config import ClientConfig, ExportConfig, GS1Config, WordPressConfig
from lib.errors import GeneratorError, LLMAPIError
from lib.gdsn import GdsnSource
from lib.language_check import (
    VERDICT_DIFFERS,
    VERDICT_MISSING,
    VERDICT_WRONG_LANGUAGE,
    CheckedProduct,
    CheckFile,
    CheckRequest,
    CheckResult,
    ComparedField,
    Finding,
    accepted,
    check_and_save,
    check_request,
    compared_fields,
    field_values,
    language_report,
    load_checks,
    missing_findings,
    pending_checks,
    run_checks,
    save_checks,
)
from lib.records import LocalisedText, ProductRecord

LANGS = ["nl", "fr"]
GTIN = "08713195006109"
FIELDS = [
    ComparedField(field="description_short", label="MarketingInformation attr 1083"),
    ComparedField(field="functional_name", label="TradeItemDescription attr 3301"),
]


def _product(
    gtin: str = GTIN,
    *,
    short: dict[str, str] | None = None,
    functional: dict[str, str] | None = None,
) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Pet spa", "fr": "Pet spa"}),
        description_short=LocalisedText(values=short) if short is not None else None,
        extras_localised=(
            {"functional_name": LocalisedText(values=functional)} if functional else {}
        ),
    )


def _request(product: ProductRecord) -> CheckRequest:
    request = check_request(product, FIELDS, LANGS, "nl")
    assert request is not None
    return request


class _Checker:
    """A producer that returns canned findings and counts what it was asked."""

    def __init__(self, findings: list[Finding] | None = None) -> None:
        self.findings = findings or []
        self.asked: list[str] = []

    def compare_languages(self, request: CheckRequest) -> CheckResult:
        self.asked.append(request.gtin)
        return CheckResult(findings=self.findings)


# --- which fields, read how ---------------------------------------------------------


def test_compared_fields_are_every_localised_source_in_both_maps_in_config_order() -> None:
    export = ExportConfig(
        format="gdsn",
        path="x.xlsx",
        gdsn_map={
            "product_name": GdsnSource(
                sheet="TradeItemDescription", attribute="3309", localised=True
            ),
            "brand": GdsnSource(sheet="TradeItemDescription", attribute="3336"),
        },
        gdsn_extras={
            "functional_name": GdsnSource(
                sheet="TradeItemDescription", attribute="3301", localised=True
            ),
            "material": GdsnSource(sheet="BrickGPCCommercialData", attribute="Material"),
        },
    )

    fields = compared_fields(export)

    assert [f.field for f in fields] == ["product_name", "functional_name"]
    assert fields[0].label == "TradeItemDescription attr 3309"


def test_field_values_reads_a_mapped_field_and_an_extra_and_drops_blanks() -> None:
    product = _product(short={"nl": " Borstel ", "fr": "  "}, functional={"nl": "dierenborstel"})

    assert field_values(product, "description_short") == {"nl": "Borstel"}
    assert field_values(product, "functional_name") == {"nl": "dierenborstel"}
    assert field_values(product, "not_a_field") == {}


def test_field_values_keeps_a_wrong_language_value_because_that_is_a_finding() -> None:
    product = _product(functional={"nl": "microvezeldoek", "fr": "Schoonmaakdoek"})

    assert field_values(product, "functional_name")["fr"] == "Schoonmaakdoek"


# --- missing: decided here ------------------------------------------------------------


def test_a_value_carried_in_one_language_only_is_missing_in_the_other() -> None:
    product = _product(short={"nl": "Borstel met vernevelaar"})

    assert missing_findings(product, FIELDS, LANGS) == [
        Finding(field="description_short", language="fr", verdict=VERDICT_MISSING)
    ]


def test_a_field_blank_in_every_language_is_not_missing_anywhere() -> None:
    assert missing_findings(_product(), FIELDS, LANGS) == []


# --- what is sent, and its fingerprint ---------------------------------------------


def test_only_fields_carried_in_two_languages_are_sent() -> None:
    product = _product(short={"nl": "a", "fr": "b"}, functional={"nl": "c"})

    request = _request(product)

    assert [f.field for f in request.fields] == ["description_short"]
    assert request.fields[0].values == {"nl": "a", "fr": "b"}


def test_a_product_with_nothing_to_compare_sends_no_request() -> None:
    assert check_request(_product(short={"nl": "a"}), FIELDS, LANGS, "nl") is None


def test_the_fingerprint_moves_with_any_compared_value_and_only_then() -> None:
    base = _request(_product(short={"nl": "a", "fr": "b"}))
    same = _request(_product(short={"nl": "a", "fr": "b"}))
    edited = _request(_product(short={"nl": "a", "fr": "c"}))

    assert base.fingerprint == same.fingerprint
    assert base.fingerprint != edited.fingerprint


# --- kept or asked ------------------------------------------------------------------


def test_a_kept_result_is_reused_only_while_its_fingerprint_matches() -> None:
    unchanged = _request(_product("08713195000001", short={"nl": "a", "fr": "b"}))
    changed = _request(_product("08713195000002", short={"nl": "a", "fr": "NEW"}))
    existing = CheckFile(
        client_id="noviplast",
        provenance="api:test",
        results=[
            CheckedProduct(gtin=unchanged.gtin, fingerprint=unchanged.fingerprint),
            CheckedProduct(gtin=changed.gtin, fingerprint="old"),
        ],
    )

    pending = pending_checks([unchanged, changed], existing)

    assert [r.gtin for r in pending.requests] == [changed.gtin]
    assert [k.gtin for k in pending.kept] == [unchanged.gtin]


def test_a_finding_about_a_field_or_language_never_sent_is_dropped() -> None:
    request = _request(_product(short={"nl": "a", "fr": "b"}))
    result = CheckResult(
        findings=[
            Finding(field="description_short", language="fr", verdict=VERDICT_DIFFERS, note="x"),
            Finding(field="functional_name", language="fr", verdict=VERDICT_DIFFERS, note="y"),
            Finding(field="description_short", language="de", verdict=VERDICT_DIFFERS, note="z"),
            Finding(field="description_short", language="fr", verdict="maybe", note="w"),
        ]
    )

    kept = accepted(request, result)

    assert [f.note for f in kept.findings] == ["x"]
    assert kept.fingerprint == request.fingerprint


def test_a_name_identical_in_every_language_is_never_the_wrong_language() -> None:
    """Seen live: "Desk lamp" in both slots came back as English in the French one."""
    request = _request(_product(short={"nl": "Desk lamp", "fr": "Desk lamp"}))
    result = CheckResult(
        findings=[Finding(field="description_short", language="fr", verdict=VERDICT_WRONG_LANGUAGE)]
    )

    assert accepted(request, result).findings == []


@pytest.mark.parametrize(
    ("nl", "fr"),
    [("Desk lamp", "desk lamp."), ("Plasma Lighter", "Plasma lighter")],
)
def test_case_and_punctuation_do_not_make_a_name_a_finding(nl: str, fr: str) -> None:
    request = _request(_product(short={"nl": nl, "fr": fr}))
    result = CheckResult(
        findings=[Finding(field="description_short", language="fr", verdict=VERDICT_WRONG_LANGUAGE)]
    )

    assert accepted(request, result).findings == []


def test_a_sentence_pasted_unchanged_into_every_slot_stays_a_finding() -> None:
    """The real mistake the identical-name rule must not hide: Dutch copied into the French slot."""
    sentence = "Borstel met vernevelaar voor de vacht van je huisdier"
    request = _request(_product(short={"nl": sentence, "fr": sentence}))
    finding = Finding(field="description_short", language="fr", verdict=VERDICT_WRONG_LANGUAGE)

    assert accepted(request, CheckResult(findings=[finding])).findings == [finding]


def test_run_checks_asks_once_per_request_in_order() -> None:
    first = _request(_product("08713195000001", short={"nl": "a", "fr": "b"}))
    second = _request(_product("08713195000002", short={"nl": "a", "fr": "b"}))
    checker = _Checker()

    judged = run_checks([first, second], checker)

    assert checker.asked == [first.gtin, second.gtin]
    assert [r.gtin for r in judged.results] == [first.gtin, second.gtin]
    assert judged.failed == []


def test_one_failed_call_does_not_lose_the_others() -> None:
    first = _request(_product("08713195000001", short={"nl": "a", "fr": "b"}))
    second = _request(_product("08713195000002", short={"nl": "a", "fr": "b"}))

    class _Flaky(_Checker):
        def compare_languages(self, request: CheckRequest) -> CheckResult:
            if request.gtin == first.gtin:
                raise LLMAPIError(500, "boom")
            return super().compare_languages(request)

    judged = run_checks([first, second], _Flaky())

    assert [r.gtin for r in judged.results] == [second.gtin]
    assert [gtin for gtin, _ in judged.failed] == [first.gtin]


# --- what the report shows ----------------------------------------------------------


def test_the_report_joins_missing_values_to_a_matching_judgment() -> None:
    product = _product(
        short={"nl": "Borstel voor je huisdier", "fr": "Brosse pour vos cheveux"},
        functional={"nl": "dierenborstel"},
    )
    request = _request(product)
    finding = Finding(
        field="description_short", language="fr", verdict=VERDICT_DIFFERS, note="hair"
    )
    checks = CheckFile(
        client_id="noviplast",
        provenance="api:test",
        results=[CheckedProduct(gtin=GTIN, fingerprint=request.fingerprint, findings=[finding])],
    )

    report = language_report([product], FIELDS, LANGS, "nl", checks)

    assert report.products == report.judged == 1
    assert [(r.label, [f.verdict for f in r.findings]) for r in report.rows] == [
        ("MarketingInformation attr 1083", [VERDICT_DIFFERS]),
        ("TradeItemDescription attr 3301", [VERDICT_MISSING]),
    ]
    assert report.rows[0].values == {
        "nl": "Borstel voor je huisdier",
        "fr": "Brosse pour vos cheveux",
    }


def test_a_judgment_about_values_the_export_no_longer_holds_is_not_shown() -> None:
    product = _product(short={"nl": "a", "fr": "edited since"}, functional={"nl": "x"})
    stale = Finding(
        field="description_short", language="fr", verdict=VERDICT_WRONG_LANGUAGE, note="old"
    )
    checks = CheckFile(
        client_id="noviplast",
        provenance="api:test",
        results=[CheckedProduct(gtin=GTIN, fingerprint="before-the-edit", findings=[stale])],
    )

    report = language_report([product], FIELDS, LANGS, "nl", checks)

    assert report.judged == 0
    assert [f.verdict for row in report.rows for f in row.findings] == [VERDICT_MISSING]


def test_a_product_with_nothing_to_compare_counts_as_judged() -> None:
    report = language_report([_product(short={"nl": "a"})], FIELDS, LANGS, "nl", None)

    assert (report.products, report.judged) == (1, 1)


def test_without_a_check_file_nothing_comparable_is_judged() -> None:
    report = language_report([_product(short={"nl": "a", "fr": "b"})], FIELDS, LANGS, "nl", None)

    assert (report.products, report.judged, report.rows) == (1, 0, [])


# --- file IO ------------------------------------------------------------------------


def test_the_check_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "language_check.json"
    checks = CheckFile(
        client_id="noviplast",
        provenance="api:test",
        results=[CheckedProduct(gtin=GTIN, fingerprint="f")],
    )

    save_checks(path, checks)

    assert load_checks(path) == checks
    assert load_checks(tmp_path / "absent.json") is None


def test_an_unreadable_check_file_is_an_error_not_an_empty_one(tmp_path: Path) -> None:
    path = tmp_path / "language_check.json"
    path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(GeneratorError, match="language-check"):
        load_checks(path)


# --- stopping, and what survives a stop ------------------------------------------------


def _requests(count: int) -> list[CheckRequest]:
    return [
        _request(_product(f"0871319500000{n}", short={"nl": "a", "fr": "b"})) for n in range(count)
    ]


class _Failing(_Checker):
    """Answers the first ``good`` requests, then fails every call with ``status``."""

    def __init__(self, good: int, status: int) -> None:
        super().__init__()
        self.good, self.status = good, status

    def compare_languages(self, request: CheckRequest) -> CheckResult:
        if len(self.asked) >= self.good:
            self.asked.append(request.gtin)
            raise LLMAPIError(self.status, "no")
        return super().compare_languages(request)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_failure_that_hits_every_product_stops_at_once(status: int) -> None:
    checker = _Failing(good=0, status=status)

    with pytest.raises(LLMAPIError):
        run_checks(_requests(5), checker)

    assert len(checker.asked) == 1


def test_three_failures_in_a_row_stop_the_run() -> None:
    checker = _Failing(good=0, status=500)

    with pytest.raises(LLMAPIError):
        run_checks(_requests(10), checker)

    assert len(checker.asked) == 3  # noqa: PLR2004 — the consecutive-failure limit


def _cfg() -> ClientConfig:
    return ClientConfig(
        client_id="democlient",
        display_name="Democlient",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(
            format="gdsn",
            path="x.xlsx",
            gdsn_map={
                "description_short": GdsnSource(
                    sheet="MarketingInformation", attribute="1083", localised=True
                )
            },
        ),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            default_language="nl",
            languages=["nl", "fr"],
        ),
    )


def test_judgments_already_made_are_written_when_the_run_stops(tmp_path: Path) -> None:
    path = tmp_path / "language_check.json"
    products = [_product(f"0871319500000{n}", short={"nl": "a", "fr": "b"}) for n in range(4)]

    with pytest.raises(LLMAPIError):
        check_and_save(_cfg(), products, path, _Failing(good=2, status=401), "api:test")

    checks = load_checks(path)
    assert checks is not None
    assert [item.gtin for item in checks.results] == [products[0].gtin14, products[1].gtin14]


def test_a_product_off_this_runs_list_keeps_its_judgment(tmp_path: Path) -> None:
    """The complete report covers live products no longer on the list; their verdicts stay."""
    path = tmp_path / "language_check.json"
    earlier, now = (_product(f"0871319500000{n}", short={"nl": "a", "fr": "b"}) for n in (1, 2))
    check_and_save(_cfg(), [earlier], path, _Checker(), "api:test")

    run = check_and_save(_cfg(), [now], path, _Checker(), "api:test")

    checks = load_checks(path)
    assert checks is not None
    assert {item.gtin for item in checks.results} == {earlier.gtin14, now.gtin14}
    assert (run.asked, run.kept) == (1, 0)
