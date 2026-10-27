"""A report reaches the operator the way their screen can receive it.

Native window: copied to Downloads and opened in their own app (2026-10-09). Browser or
container: downloaded by the browser, because a copy would land on the serving side.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from ui import handover, runner, theme  # noqa: E402 — after the skip, which needs nicegui


def test_a_report_is_copied_to_downloads_without_overwriting_and_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(runner, "open_in_default_app", opened.append)
    report = tmp_path / "reports" / "issues.pdf"
    report.parent.mkdir()
    report.write_bytes(b"%PDF one")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    first = runner.hand_over(report, downloads=downloads)
    second = runner.hand_over(report, downloads=downloads)

    assert first == downloads / "issues.pdf"
    assert second == downloads / "issues-1.pdf", "an earlier download is never overwritten"
    assert first.read_bytes() == b"%PDF one"
    assert opened == [first, second]


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(theme, "notify_ok", said.append)
    monkeypatch.setattr(theme, "notify_problem", said.append)
    return said


def test_served_to_a_browser_the_browser_downloads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notes: list[str]
) -> None:
    """Inside the container, Downloads is the container's — the operator would never see it."""
    downloaded: list[tuple[bytes, str]] = []
    monkeypatch.setattr(
        handover.ui.download, "content", lambda data, name: downloaded.append((data, name))
    )
    monkeypatch.setattr(runner, "hand_over", lambda *a, **k: pytest.fail("copied server-side"))
    monkeypatch.setattr(handover, "_through_browser", True)
    report = tmp_path / "issues.pdf"
    report.write_bytes(b"%PDF")

    handover.give(report)
    report.unlink()  # the bytes were handed over; the original may go at once

    assert downloaded == [(b"%PDF", "issues.pdf")]
    assert notes == ["Downloading issues.pdf"]


def test_in_the_native_window_it_goes_to_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notes: list[str]
) -> None:
    copied: list[Path] = []
    monkeypatch.setattr(handover.ui.download, "content", lambda *a: pytest.fail("browser download"))
    monkeypatch.setattr(runner, "hand_over", lambda path, open_it=True: copied.append(path) or path)
    monkeypatch.setattr(handover, "_through_browser", False)
    report = tmp_path / "issues.pdf"
    report.write_bytes(b"%PDF")

    handover.give(report)

    assert copied == [report]
    assert notes == [f"Saved to {report} — opening it"]


def test_a_file_to_send_on_is_saved_but_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handover pack opened by the OS would be unpacked into Downloads, passwords and all."""
    opened: list[Path] = []
    monkeypatch.setattr(runner, "open_in_default_app", opened.append)
    pack = tmp_path / "handover.zip"
    pack.write_bytes(b"PK")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    copy = runner.hand_over(pack, downloads=downloads, open_it=False)

    assert copy.read_bytes() == b"PK"
    assert opened == []
