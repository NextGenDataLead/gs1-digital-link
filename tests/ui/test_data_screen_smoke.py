"""The Data screen builds, end to end, through the real app — four steps, and the report fold.

A smoke test, not a rendering test. It cannot say the screen looks right; it says that a GET of
``/data`` runs the whole of ``render()`` for a client with a batch and a video mapping and comes
back 200 with every step on it. That is the failure the fold could introduce — a section that
raises while the page is built takes the screen down, and only a terminal would say so.

Measured before this existed: a plain ASGI GET of a ``@ui.page`` *does* execute its body, given
``prepare_simulation()`` and the app's lifespan (both are needed — without either, every page
fails identically inside NiceGUI's own error handler). The report's deferred ``ui.timer`` does not
fire under a GET, so nothing is written and the report itself is not covered here.

The client is the synthetic ``democlient``, with every path pointed into ``tmp_path``: this must
never read or write a real client's files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

import httpx  # noqa: E402
import openpyxl  # noqa: E402
from nicegui import core  # noqa: E402
from nicegui.testing.user_simulation import prepare_simulation  # noqa: E402

from lib import demo_export  # noqa: E402
from lib.config import ClientConfig, load_clients  # noqa: E402
from ui import context  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent.parent

_MAPPING = "nl:\n- {file: a.mp4, gtin: ''}\nfr:\n- {file: a-fr.mp4, gtin: ''}\n"


def _workbook(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def _demo_client(tmp_path: Path) -> ClientConfig:
    """``democlient`` from the example config, every path moved into ``tmp_path``."""
    cfg = load_clients(_REPO / "clients.example.yml")["democlient"]
    assert cfg.process_list is not None and cfg.media is not None
    export = tmp_path / "process" / "uploads" / "GS1 export" / "export.xlsx"
    selection = tmp_path / "process" / "selection" / "selections.xlsx"
    mapping = tmp_path / "videos" / "mapping.yml"
    _workbook(export, demo_export.sheet_grids())
    _workbook(selection, {"Scope": demo_export.process_list_rows()})
    (tmp_path / "videos" / "NL").mkdir(parents=True)
    mapping.write_text(_MAPPING, "utf-8")
    return cfg.model_copy(
        update={
            "export": cfg.export.model_copy(update={"path": str(export)}),
            "process_list": cfg.process_list.model_copy(update={"path": str(selection)}),
            "media": cfg.media.model_copy(
                update={
                    "video_map_path": str(mapping),
                    "video_folders": {"nl": str(tmp_path / "videos" / "NL")},
                }
            ),
        }
    )


async def _get(route: str) -> httpx.Response:
    async with core.app.router.lifespan_context(core.app):
        transport = httpx.ASGITransport(core.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(route)


def test_the_data_screen_builds_with_five_steps_and_no_mapping_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ui.app  # noqa: F401, PLC0415 — importing registers the routes

    cfg = _demo_client(tmp_path)
    monkeypatch.setattr(context, "client_id", lambda: "democlient")
    monkeypatch.setattr(context, "client_config", lambda _cid: cfg)
    prepare_simulation()

    response = asyncio.run(_get("/data"))

    assert response.status_code == 200
    html = response.text
    # The numerals live in NiceGUI's serialised element tree, not in tags — count the class.
    assert html.count("step-num") == 5
    for title in (
        "Upload the product selection list",
        "Upload the GS1 export",
        "Upload the video sign-off sheet",
        "What this batch publishes",
        "Choose the products and save",
        "Issue report for the client",
    ):
        assert title.replace("'", "\\u0027") in html or title in html, title
    # The row-by-row editor was removed on the operator's word: the mapping is edited in its file.
    assert "The mapping, file by file" not in html


def test_step_4_offers_both_then_links_then_pages() -> None:
    """Operator, 2026-10-09: the run types in the order both, links, pages."""
    from lib.gates import Mode  # noqa: PLC0415
    from ui.pages import data  # noqa: PLC0415

    assert data._MODE_ORDER == (Mode.BOTH, Mode.LINKS, Mode.PAGES)
    assert set(data._MODE_ORDER) == set(Mode), "every run type is offered"
