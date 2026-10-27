"""The Preflight screen's verdict must not claim more than it checked (issue #118).

The screen runs the offline checks on arrival, and ``lib.preflight.run_checks`` returns before
appending ``site_serves``, ``wordpress`` and ``gs1``. So the verdict that reads as an unqualified
all-clear was reachable having tested no credential at all.

That matters because those three checks exist nowhere else in a batch. ``run_execute`` sets
``resolved_gs1 = None`` when ``--dry-run``, so the dry run never mints a token either — a wrong
credential survives every gate and surfaces at the first real write, with some rows already live
and, in ``links`` or ``both`` mode, records that cannot be deleted.

Needs NiceGUI, because ``ui.pages.preflight`` imports it — so this runs in the second CI job.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from lib import preflight  # noqa: E402
from ui.pages.preflight import _CREDENTIAL_CHECKS, _verdict  # noqa: E402

OFFLINE_ONLY = "Offline checks only — no credential was tested."


def check(name: str, status: str) -> dict[str, Any]:
    return {"name": name, "title": name, "detail": "", "status": status}


def offline(*statuses: str) -> list[dict[str, Any]]:
    """A payload with no credential check in it — what arriving at the screen produces."""
    return [check(f"offline_{i}", s) for i, s in enumerate(statuses)]


def with_credentials(*statuses: str) -> list[dict[str, Any]]:
    return offline(*statuses) + [check(n, "ok") for n in sorted(_CREDENTIAL_CHECKS)]


def test_all_clear_offline_does_not_read_as_an_all_clear() -> None:
    """The defect, exactly: "Ready." with nothing authenticated."""
    verdict, kind = _verdict(offline("ok", "ok"))
    assert verdict.startswith("Ready.")
    assert OFFLINE_ONLY in verdict, verdict
    assert kind == "quiet"


def test_warnings_offline_are_qualified_too() -> None:
    verdict, kind = _verdict(offline("ok", "warn"))
    assert "read the warnings" in verdict
    assert OFFLINE_ONLY in verdict, verdict
    assert kind == "warn"


def test_a_full_run_is_not_qualified() -> None:
    """Once the three have run, the verdict has earned the right to be unqualified."""
    for statuses in (("ok", "ok"), ("ok", "warn")):
        verdict, _ = _verdict(with_credentials(*statuses))
        assert OFFLINE_ONLY not in verdict, verdict


def test_a_failure_is_never_qualified() -> None:
    """ "Not ready" is already actionable; the credential question is not yet the problem.

    Diluting the one verdict that means *stop* would cost more than the caveat buys.
    """
    for payload in (offline("fail"), with_credentials("fail")):
        verdict, kind = _verdict(payload)
        assert verdict == "Not ready. Fix the failures below before publishing."
        assert kind == "danger"


@pytest.mark.parametrize("present", sorted(_CREDENTIAL_CHECKS))
def test_any_one_credential_check_counts_as_having_looked(present: str) -> None:
    """Partial is not qualified: `--offline` drops all three together, so one implies the run.

    Keyed on presence rather than on a flag threaded down from the caller, because the payload is
    whatever the subprocess printed and the screen has no other evidence of what was asked for.
    """
    verdict, _ = _verdict(offline("ok") + [check(present, "ok")])
    assert OFFLINE_ONLY not in verdict, verdict


def test_the_names_match_what_lib_preflight_actually_emits() -> None:
    """A rename in `lib` would otherwise make the caveat permanent and silent."""
    source = inspect.getsource(preflight)
    for name in _CREDENTIAL_CHECKS:
        assert f'"{name}",' in source, (
            f"ui.pages.preflight expects a check named {name!r}, which lib/preflight.py no longer "
            "emits — the verdict would say 'no credential was tested' forever"
        )
