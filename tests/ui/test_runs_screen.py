"""The Runs screen's error line (issue #60).

The screen renders one line per failed row, and that line was the operator's whole view of a
failure: ``02987653007717 (fr): WordPressAPIError('WordPress API error 403')``. It did not say
which of the row's five HTTP calls had failed, so answering that meant leaving the shell and
re-running with the output piped to a file.

Needs NiceGUI, because ``ui.pages.runs`` imports it — so this runs in the second CI job, not the
required one. See ``tests/ui/test_pages_contract.py`` for why the suite is split that way.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from lib.records import RunOutcome  # noqa: E402
from ui.pages.runs import _error_line  # noqa: E402


def _outcome(error: str, failed_call: str | None = None) -> RunOutcome:
    return RunOutcome(
        gtin="02987653007717",
        language="fr",
        ts=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        status="error",
        error=error,
        failed_call=failed_call,
    )


def test_names_the_failing_call_first() -> None:
    line = _error_line(
        _outcome(
            "WordPressAPIError('WordPress API error 403 …')",
            failed_call="POST /wp-json/wp/v2/media (upload media clip-a1b2c3d4e5f6)",
        )
    )

    assert line.startswith("02987653007717 (fr): POST /wp-json/wp/v2/media")
    assert "upload media clip-a1b2c3d4e5f6" in line


def test_a_row_with_no_recorded_call_reads_as_it_always_did() -> None:
    # Blocked siblings and template errors are not calls, and older run logs have no such field.
    line = _error_line(_outcome("blocked: language(s) fr of this GTIN failed"))

    assert line == "02987653007717 (fr): blocked: language(s) fr of this GTIN failed"
