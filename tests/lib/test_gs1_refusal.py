"""GS1's 21011 refusal is reported in plain words — and only after it was actually tried.

GS1 can refuse a barcode with ``No valid contract found`` that is on the client's account all
the same, so nothing predicts the refusal before a run; the report only says it clearly
afterwards.
"""

from __future__ import annotations

from lib.issue_report import NO_CONTRACT, RUN_ERROR, run_issues
from lib.scope_report import IN_SCOPE, ScopeRow, UnitResult

GTIN = "00292222002959"


def _row(error: str) -> ScopeRow:
    return ScopeRow([GTIN], GTIN, IN_SCOPE, {"nl": UnitResult("error", "", error)})


def test_a_no_contract_refusal_is_named_without_the_raw_error() -> None:
    error = (
        "POST /digitallinkv2/v2/digitallink GS1APIError('GS1 API error 400 … "
        '[{"code": "21011", "message": "No valid contract found."}]\')'
    )

    _, issues = run_issues([_row(error)], {GTIN: "voegstrijker"}, writes_pages=False)

    assert [i.category for i in issues] == [NO_CONTRACT]
    assert "GS1APIError" not in issues[0].reason, "the client reads this; no raw error"
    assert "21011" in issues[0].reason


def test_any_other_error_stays_a_publishing_failure() -> None:
    _, issues = run_issues([_row("WP 500 on POST /pages")], {}, writes_pages=False)

    assert [i.category for i in issues] == [RUN_ERROR]
