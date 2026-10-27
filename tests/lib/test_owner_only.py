"""``.env`` restricted to its owner — mode bits on macOS/Linux, an NTFS permission on Windows.

The Windows half cannot run here, so it is checked by what it would ask Windows to do: the exact
``icacls`` call the onboarding guide used to make an operator type by hand.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lib import owner_only


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits")
def test_on_posix_the_mode_is_owner_only(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("X=1\n", encoding="utf-8")

    assert owner_only.restrict(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_on_windows_inheritance_is_removed_and_only_this_user_is_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / ".env"
    secret.write_text("X=1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(owner_only.sys, "platform", "win32")
    monkeypatch.setattr(owner_only.subprocess, "run", run)
    monkeypatch.setenv("USERNAME", "jan")

    assert owner_only.restrict(secret)
    assert calls == [["icacls", str(secret), "/inheritance:r", "/grant:r", "jan:F"]]


def test_on_windows_a_refusal_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / ".env"
    secret.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(owner_only.sys, "platform", "win32")
    monkeypatch.setattr(
        owner_only.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 5)
    )
    monkeypatch.setenv("USERNAME", "jan")

    assert owner_only.restrict(secret) is False
