"""Unit tests for lib/demo_videos.py — the demo client's video mix does what its table says.

Checked through the real join and the real import planner rather than by restating the rows: the
point of the mix is the states the screen and the report end up showing, and those come from
:mod:`lib.video_status` and :mod:`lib.video_signoff`, not from this module.
"""

from __future__ import annotations

from pathlib import Path

from lib import demo_videos
from lib.demo_export import CATALOGUE
from lib.media_video import VideoMap, fully_mapped_gtins, load_video_map
from lib.records import LocalisedText, ProductRecord
from lib.video_signoff import columns, plan
from lib.video_status import VideoStatus, video_status
from lib.xlsx import Grid

_LANGUAGES = ["nl", "fr"]
_PRODUCTS = [
    ProductRecord(gtin=p.gtin, brand="Demoplast", product_name=LocalisedText(values=dict(p.name)))
    for p in CATALOGUE
]


def _mapping(tmp_path: Path, text: str | None = None) -> VideoMap:
    path = tmp_path / "mapping.yml"
    path.write_text(text if text is not None else demo_videos.mapping_text(), "utf-8")
    return load_video_map(path)


def _status(tmp_path: Path) -> VideoStatus:
    return video_status(
        _mapping(tmp_path),
        _PRODUCTS,
        languages=_LANGUAGES,
        files_by_language=demo_videos.files(),
    )


def _gtins(*indexes: int) -> set[str]:
    return {CATALOGUE[i].gtin for i in indexes}


def test_five_are_held_for_video_one_of_them_for_two_videos(tmp_path: Path) -> None:
    status = _status(tmp_path)

    assert {p.gtin for p in status.held} == _gtins(6, 7, 8, 9, 10)
    assert {p.gtin for p in status.clashing} == _gtins(8)
    # Product 6's French row is unset too: until the sheet fills it, it is a file with no barcode.
    assert [v.file for v in status.unassigned] == ["Passoire FR.mp4", demo_videos.MYSTERY]
    assert status.not_in_map == () and status.files_missing == ()


def test_seven_pass_the_gate_before_the_sheet_and_eight_after(tmp_path: Path) -> None:
    """Eight once the client's sheet is applied; the ninth waits for a Dutch video to be skipped."""
    vmap = _mapping(tmp_path)
    assert fully_mapped_gtins(vmap, _LANGUAGES) == _gtins(0, 1, 2, 3, 4, 5, 11)

    decided = plan(_grid(), vmap, exported=_gtins(*range(12)), languages=_LANGUAGES)
    lines = demo_videos.mapping_text().splitlines()
    for (_language, file), assignment in decided.edits.items():
        at = next(n for n, line in enumerate(lines) if line.startswith(f"- {{file: {file},"))
        lines[at : at + 1] = [f"- {{file: {file}, gtin: '{gtin}'}}" for gtin in assignment]
    after = fully_mapped_gtins(_mapping(tmp_path, "\n".join(lines) + "\n"), _LANGUAGES)

    assert after == _gtins(0, 1, 2, 3, 4, 5, 6, 11)


def _grid() -> Grid:
    header, *rows = demo_videos.signoff_rows()
    return Grid(
        header=[str(cell) for cell in header],
        rows=[["" if cell is None else str(cell) for cell in row] for row in rows],
    )


def test_the_sheet_has_one_row_per_outcome_and_the_real_sheets_column_name(
    tmp_path: Path,
) -> None:
    grid = _grid()
    assert "current_gtin" in grid.header  # what the column pickers exist because of
    assert columns(grid) is not None

    decided = plan(grid, _mapping(tmp_path), exported=_gtins(*range(12)), languages=_LANGUAGES)

    assert [row.outcome for row in decided.rows] == [
        "change",
        "unchanged",
        "change",
        "blank",
        "rejected",
    ]
    shared = decided.edits[("nl", CATALOGUE[0].name["nl"] + ".mp4")]
    assert shared == tuple(sorted(_gtins(0, 7))), "one video, two products"
    assert "scientific notation" in decided.rows[-1].detail


def test_the_mapping_is_in_the_one_row_per_line_shape_the_editor_accepts() -> None:
    """``yaml.safe_dump``'s block style is refused by the shell's editor — the trap this avoids."""
    rows = [line for line in demo_videos.mapping_text().splitlines() if line.startswith("- ")]

    assert rows and all(line.startswith("- {file: ") and line.endswith("}") for line in rows)


def test_every_mapping_row_has_a_placeholder_file() -> None:
    mapped = {(v.language, v.file) for v in demo_videos.videos()}
    on_disk = {(lang, name) for lang, names in demo_videos.files().items() for name in names}

    assert mapped == on_disk
