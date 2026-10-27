"""Tests for ui/publish_outcome.py — what the dialog after a real run says.

Operator, on the e2e run: the real run finished with a toast and the red button still pressable, so
there was nothing to be confident about. The run's own log is the account of what happened; this
turns it into a dialog that has to be closed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lib.records import RunOutcome
from ui.publish_outcome import verdict

_AT = datetime(2026, 10, 7, tzinfo=UTC)


def _row(gtin: str, language: str, status: str, *, gs1: bool = False) -> RunOutcome:
    return RunOutcome(gtin=gtin, language=language, ts=_AT, status=status, gs1_set=gs1)


def _six(status: str = "ok", *, gs1: bool = True) -> list[RunOutcome]:
    return [
        _row(gtin, language, status, gs1=gs1)
        for gtin in ("02987653004488", "02987653005898", "02987653007151")
        for language in ("nl", "fr")
    ]


def test_the_e2e_run_reads_as_published_with_its_gs1_records() -> None:
    headline, body, kind = verdict(_six(), returncode=0, permanent=True)

    assert (headline, kind) == ("Published", "quiet")
    assert "6 page(s) published, 0 errors" in body
    assert "3 barcode(s)" in body


def test_a_pages_run_does_not_mention_gs1() -> None:
    _, body, _ = verdict(_six(gs1=False), returncode=0, permanent=False)

    assert "GS1" not in body


def test_one_error_is_a_failure_whatever_the_exit_code() -> None:
    rows = _six()
    rows[3] = _row("02987653005898", "fr", "error")

    headline, body, kind = verdict(rows, returncode=0, permanent=True)

    assert (headline, kind) == ("Run failed", "danger")
    assert "1 of 6" in body


def test_a_non_zero_exit_with_no_rows_is_a_failure_not_a_success() -> None:
    headline, _, kind = verdict([], returncode=2, permanent=True)

    assert (headline, kind) == ("Run failed", "danger")


def test_no_rows_and_a_clean_exit_is_not_called_published() -> None:
    """Writing nothing and reporting success is the outcome indistinguishable from working."""
    headline, _, kind = verdict([], returncode=0, permanent=False)

    assert headline != "Published"
    assert kind == "warn"
