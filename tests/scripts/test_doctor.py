"""Tests for scripts/doctor.py.

The checks themselves live in ``lib.preflight`` and are tested there. What this file asserts
is the wrapper's contract: the exit code an automated caller reads, the ``--json`` shape a UI
parses, and the rendering rules that decide whether an operator can act on the report — that
a remedy is shown, and that skipped checks are counted out loud rather than quietly absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.preflight import CheckResult, Status
from scripts import doctor


def _results(*statuses: Status) -> list[CheckResult]:
    return [
        CheckResult(
            name=f"check{n}",
            title=f"Check {n}",
            status=status,
            detail="what is true",
            remedy="what to do" if status is Status.FAIL else "",
        )
        for n, status in enumerate(statuses)
    ]


def _patch_checks(monkeypatch: pytest.MonkeyPatch, results: list[CheckResult]) -> dict[str, object]:
    """Patch run_checks and record the keyword arguments the CLI passed it."""
    seen: dict[str, object] = {}

    def fake(client_id: str | None = None, **kwargs: object) -> list[CheckResult]:
        seen.update(kwargs, client_id=client_id)
        return results

    monkeypatch.setattr(doctor, "run_checks", fake)
    return seen


def test_a_failure_exits_1_so_a_caller_can_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_checks(monkeypatch, _results(Status.OK, Status.FAIL))

    code = doctor.main(["acme"])

    assert code == 1
    assert "not ready" in capsys.readouterr().out


def test_warnings_alone_do_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warning is something to read, not something to stop for — or it will be ignored."""
    _patch_checks(monkeypatch, _results(Status.OK, Status.WARN))

    code = doctor.main(["acme"])

    assert code == 0
    assert "ready, but read the warnings" in capsys.readouterr().out


def test_the_verdict_counts_skipped_checks_out_loud(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report that quietly shows fewer lines reads as a clean bill for things nobody checked."""
    _patch_checks(monkeypatch, _results(Status.OK, Status.NA, Status.NA))

    doctor.main(["acme"])

    assert "2 not applicable" in capsys.readouterr().out


def test_a_failure_prints_its_remedy_not_just_its_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_checks(monkeypatch, _results(Status.FAIL))

    doctor.main(["acme"])

    out = capsys.readouterr().out
    assert "what is true" in out
    assert "→ what to do" in out


def test_json_output_is_parseable_and_carries_the_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_checks(monkeypatch, _results(Status.OK, Status.FAIL))

    doctor.main(["acme", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert [entry["status"] for entry in payload] == ["ok", "fail"]
    assert payload[1]["remedy"] == "what to do"


def test_offline_is_passed_through_and_said_out_loud(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_checks(monkeypatch, _results(Status.OK))

    doctor.main(["acme", "--offline"])

    assert seen["offline"] is True
    assert "did not run" in capsys.readouterr().err


def test_config_path_is_passed_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """So a candidate clients.yml can be validated before it replaces the real one."""
    seen = _patch_checks(monkeypatch, _results(Status.OK))

    doctor.main(["acme", "--config", str(tmp_path / "candidate.yml")])

    assert seen["config_path"] == str(tmp_path / "candidate.yml")


def test_the_batch_mode_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.gates import Mode  # noqa: PLC0415

    seen = _patch_checks(monkeypatch, _results(Status.OK))

    doctor.main(["acme", "--mode", "links"])

    assert seen["mode"] is Mode.LINKS


def test_no_mode_checks_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_checks(monkeypatch, _results(Status.OK))

    doctor.main(["acme"])

    assert "mode" not in seen
