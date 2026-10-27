"""The operator can select and copy text in the native window.

pywebview turns text selection off unless asked (2026-10-23): barcodes, URLs and error
messages could not be copied out of the shell.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from nicegui import app as nicegui_app  # noqa: E402 — after the skip, which needs nicegui

import ui.app as shell  # noqa: E402


def test_the_native_window_allows_text_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nicegui_app.native, "window_args", {})
    monkeypatch.setattr(shell.theme, "install", lambda: None)
    monkeypatch.setattr(shell.handover, "deliver_through_browser", lambda _browser: None)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(shell.ui, "run", lambda **kwargs: calls.append(kwargs))

    shell.main(native=True)

    assert calls and calls[0]["native"] is True
    assert nicegui_app.native.window_args["text_select"] is True
