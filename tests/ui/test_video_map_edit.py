"""Editing ``mapping.yml`` from a screen without losing what is written in it.

This file is a record of client sign-off, and two things in it carry information nothing else
does: the confirmed GTINs, and the trailing ``# hint:`` comments recording which fuzzy match each
one came from and which were rejected. Both would be gone after one round-trip through
``yaml.safe_dump``, which is why :mod:`ui.video_map_edit` rewrites the text a row at a time.

So these tests are mostly about what *did not* change. The pattern — and several of the cases —
are lifted from :mod:`tests.ui.test_config_edit`, which asks the same question of ``clients.yml``.
No NiceGUI is involved: the module is deliberately free of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.errors import VideoMapError
from lib.media_video import load_video_map, state_of
from lib.video_signoff import CHANGE, SignoffPlan, addable, plan
from lib.xlsx import Grid
from ui import video_map_edit

_MAPPING = """\
# Working video mapping. gtin "" = UNSET (fill it), "skip" = no product for this video.
# CONFIRMED = client-confirmed.
nl:
  - {file: "4-in-1 Lamp.mpg", gtin: ""}   # hint: 02987653000893 'lamp' (0.53)
  - {file: "Glowbug.mpg", gtin: "2987653007434"}   # verify 'cliplamp' (0.93)
  - {file: "Trailer.mpg", gtin: "skip"}
fr:
  - {file: "Lampe.mpg", gtin: ""}   # hint: 02987653000893 'lampe' (0.61)
"""


def _rows(text: str) -> dict[tuple[str, str], str]:
    return {(r.language, r.file): r.gtin for r in video_map_edit.parse(text)}


# --- Reading ------------------------------------------------------------------


def test_every_row_is_found_with_its_language_and_its_comment() -> None:
    rows = video_map_edit.parse(_MAPPING)

    assert [(r.language, r.file) for r in rows] == [
        ("nl", "4-in-1 Lamp.mpg"),
        ("nl", "Glowbug.mpg"),
        ("nl", "Trailer.mpg"),
        ("fr", "Lampe.mpg"),
    ]
    assert rows[0].note.startswith("# hint:")
    assert rows[0].state == "unset"
    assert rows[1].state == "confirmed"
    assert rows[2].state == "skip"


def test_a_reformatted_row_is_refused_rather_than_guessed_at() -> None:
    """Every row this tool drafts is one line. A hand-reformatted one is not editable in place.

    Refusing says so and leaves the terminal working. Guessing where a multi-line row ends would
    corrupt a file that represents client sign-off.
    """
    text = "nl:\n  - file: A.mpg\n    gtin: ''\n"

    with pytest.raises(VideoMapError) as caught:
        video_map_edit.parse(text)

    assert "text editor" in str(caught.value)


# --- Writing one row ----------------------------------------------------------


def test_confirming_a_gtin_changes_that_row_and_nothing_else() -> None:
    edited = video_map_edit.set_gtin(_MAPPING, "nl", "4-in-1 Lamp.mpg", "02987653000893")

    before, after = _MAPPING.splitlines(), edited.splitlines()
    assert len(before) == len(after)
    assert [n for n, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [3]
    assert _rows(edited)[("nl", "Glowbug.mpg")] == "2987653007434"  # sign-off untouched


def test_the_hint_comment_survives_the_gtin_it_explains() -> None:
    """The comment is the evidence for the decision being recorded. Losing it loses the why."""
    edited = video_map_edit.set_gtin(_MAPPING, "nl", "4-in-1 Lamp.mpg", "02987653000893")

    row = next(r for r in video_map_edit.parse(edited) if r.file == "4-in-1 Lamp.mpg")
    assert row.note == "# hint: 02987653000893 'lamp' (0.53)"


def test_a_longer_value_pushes_its_comment_along_rather_than_overwriting_it() -> None:
    edited = video_map_edit.set_gtin(_MAPPING, "nl", "4-in-1 Lamp.mpg", "02987653000893")

    line = edited.splitlines()[3]
    assert line.index("#") > line.index("02987653000893")
    assert "  #" in line, "at least two spaces between the value and its comment"


def test_a_gtin_is_always_quoted(tmp_path: Path) -> None:
    """Unquoted, YAML reads it as an integer — which fails validation, and drops a leading zero."""
    edited = video_map_edit.set_gtin(_MAPPING, "nl", "4-in-1 Lamp.mpg", "02987653000893")

    assert 'gtin: "02987653000893"' in edited
    path = tmp_path / "mapping.yml"
    path.write_text(edited, encoding="utf-8")
    assert load_video_map(path).resolve("02987653000893", "nl") == "4-in-1 Lamp.mpg"


def test_skip_and_clear_are_both_writable() -> None:
    skipped = video_map_edit.set_gtin(_MAPPING, "fr", "Lampe.mpg", video_map_edit.SKIP)
    cleared = video_map_edit.set_gtin(_MAPPING, "nl", "Glowbug.mpg", "")

    assert _rows(skipped)[("fr", "Lampe.mpg")] == "skip"
    assert _rows(cleared)[("nl", "Glowbug.mpg")] == ""


def test_the_same_filename_in_two_languages_is_two_rows() -> None:
    """``PipeSticks.mpeg`` can exist in both folders; editing one must not touch the other."""
    text = 'nl:\n  - {file: "A.mpg", gtin: ""}\nfr:\n  - {file: "A.mpg", gtin: ""}\n'

    edited = video_map_edit.set_gtin(text, "fr", "A.mpg", "02987653000001")

    assert _rows(edited) == {("nl", "A.mpg"): "", ("fr", "A.mpg"): "02987653000001"}


def test_an_unknown_row_is_an_error_not_a_silent_no_op() -> None:
    with pytest.raises(VideoMapError):
        video_map_edit.set_gtin(_MAPPING, "nl", "NotThere.mpg", "02987653000001")


def test_a_batch_of_edits_is_applied_in_one_pass() -> None:
    edited = video_map_edit.apply_edits(
        _MAPPING,
        {("nl", "4-in-1 Lamp.mpg"): "02987653000893", ("fr", "Lampe.mpg"): "02987653000893"},
    )

    rows = _rows(edited)
    assert rows[("nl", "4-in-1 Lamp.mpg")] == "02987653000893"
    assert rows[("fr", "Lampe.mpg")] == "02987653000893"


# --- Adding rows for new files ------------------------------------------------


def test_files_on_disk_with_no_row_are_found_per_language() -> None:
    missing = video_map_edit.files_missing_from_map(
        _MAPPING, {"nl": ["4-in-1 Lamp.mpg", "New.mpg"], "fr": ["Lampe.mpg"]}
    )

    assert missing == {"nl": ["New.mpg"]}


def test_appending_adds_unset_rows_at_the_end_of_their_language() -> None:
    edited = video_map_edit.append_rows(_MAPPING, "nl", ["New.mpg"])

    rows = video_map_edit.parse(edited)
    added = next(r for r in rows if r.file == "New.mpg")
    assert added.language == "nl"
    assert added.state == "unset"
    assert [r.file for r in rows if r.language == "fr"] == ["Lampe.mpg"]


def test_appending_does_not_steal_the_next_language_header() -> None:
    """The new row must land before ``fr:``, not after it, or it changes language silently."""
    edited = video_map_edit.append_rows(_MAPPING, "nl", ["New.mpg"])

    lines = edited.splitlines()
    assert lines.index('  - {file: "New.mpg", gtin: ""}') < lines.index("fr:")


def test_appending_a_file_already_in_the_map_is_a_no_op() -> None:
    assert video_map_edit.append_rows(_MAPPING, "nl", ["Glowbug.mpg"]) == _MAPPING


def test_a_language_with_no_block_yet_gets_one() -> None:
    edited = video_map_edit.append_rows(_MAPPING, "de", ["Neu.mpg"])

    assert ("de", "Neu.mpg") in _rows(edited)


# --- Writing the file ---------------------------------------------------------


def test_what_the_screen_wrote_is_what_the_pipeline_then_loads(tmp_path: Path) -> None:
    """The round-trip that matters: the run must read back exactly what was confirmed here."""
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")

    video_map_edit.write_validated(
        path, video_map_edit.set_gtin(_MAPPING, "fr", "Lampe.mpg", "2987653007434")
    )

    vmap = load_video_map(path)
    assert vmap.resolve("2987653007434", "fr") == "Lampe.mpg"
    assert vmap.resolve("2987653007434", "nl") == "Glowbug.mpg"


def test_the_previous_version_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")

    backup = video_map_edit.write_validated(
        path, video_map_edit.set_gtin(_MAPPING, "fr", "Lampe.mpg", "2987653007434")
    )

    assert backup.read_text(encoding="utf-8") == _MAPPING


def test_a_candidate_that_will_not_load_is_refused_and_the_file_is_left_alone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")

    with pytest.raises(VideoMapError):
        video_map_edit.write_validated(path, "nl: 42\n")

    assert path.read_text(encoding="utf-8") == _MAPPING
    assert not list(tmp_path.glob("*.candidate"))


def test_a_candidate_that_lost_a_row_is_refused(tmp_path: Path) -> None:
    """Nothing on the screen deletes a row, so a row that has gone is a fault in the tool.

    A confirmed row is somebody's sign-off on which video shows which product. Writing a file
    that quietly has fewer of them than the one it replaces is the failure this refuses.
    """
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")
    without_glowbug = (
        "\n".join(line for line in _MAPPING.splitlines() if "Glowbug" not in line) + "\n"
    )

    with pytest.raises(VideoMapError) as caught:
        video_map_edit.write_validated(path, without_glowbug)

    assert "nl/Glowbug.mpg" in str(caught.value)
    assert path.read_text(encoding="utf-8") == _MAPPING


def test_an_imported_signoff_plan_applies_through_this_module(tmp_path: Path) -> None:
    """The contract between the two halves of the sign-off import, which nothing else checks.

    :mod:`lib.video_signoff` decides and :func:`ui.video_map_edit.apply_edits` writes, and the only
    thing passing between them is ``SignoffPlan.edits``. They are in different layers and neither
    imports the other, so a change to that key — ``(language, file)``, the same pair the per-row
    editor stages under — would break the import silently: ``apply_edits`` leaves a row it cannot
    find alone, so the file would be rewritten, validated, backed up, and unchanged.
    """
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")
    grid = Grid(
        ["language", "file", "gtin"],
        [
            ["nl", "4-in-1 Lamp.mpg", "2987653000893"],
            ["nl", "Glowbug.mpg", "0291111003190"],
        ],
    )

    decided = plan(
        grid,
        load_video_map(path),
        exported={"02987653000893", "00291111003190"},
        languages=("nl", "fr"),
    )
    video_map_edit.write_validated(
        path, video_map_edit.apply_edits(path.read_text("utf-8"), decided.edits)
    )
    after = {entry.file: entry.gtin for entry in load_video_map(path).by_language["nl"]}

    shared = [e.gtin for e in load_video_map(path).by_language["nl"] if e.file == "Glowbug.mpg"]

    assert len(decided.of(CHANGE)) == 2
    assert after["4-in-1 Lamp.mpg"] == "02987653000893", "the unset row took the sheet's barcode"
    assert shared == ["2987653007434", "00291111003190"], (
        "the barcode the sheet never names keeps its video, written as it was; the sheet's is added"
    )
    assert state_of(after["Trailer.mpg"]) == "skip", "a row the sheet never mentioned is untouched"


# --- One video, several barcodes ---------------------------------------------


def test_a_shared_video_is_written_one_row_per_barcode_below_its_first() -> None:
    edited = video_map_edit.set_assignment(
        _MAPPING, "nl", "Glowbug.mpg", ("02987653000893", "02987653007434")
    )
    rows = [(r.file, r.gtin) for r in video_map_edit.parse(edited) if r.language == "nl"]

    assert rows == [
        ("4-in-1 Lamp.mpg", ""),
        ("Glowbug.mpg", "2987653007434"),
        ("Glowbug.mpg", "02987653000893"),
        ("Trailer.mpg", "skip"),
    ], "the barcode it had keeps its row as written; the new one goes directly below"
    assert "# verify 'cliplamp' (0.93)" in edited, "the hint comment stays"


def test_an_assignment_it_already_has_is_no_diff_at_all() -> None:
    """13-digit in the file, 14 in the plan: the same barcode, so nothing is rewritten."""
    assert (
        video_map_edit.set_assignment(_MAPPING, "nl", "Glowbug.mpg", ("02987653007434",))
        == _MAPPING
    )


def test_a_shared_video_back_to_one_barcode_drops_the_spare_row() -> None:
    shared = video_map_edit.set_assignment(
        _MAPPING, "nl", "Glowbug.mpg", ("02987653000893", "02987653007434")
    )

    back = video_map_edit.set_assignment(shared, "nl", "Glowbug.mpg", ("02987653007434",))

    assert back == _MAPPING


def test_skipping_a_shared_video_leaves_one_skip_row_with_its_comment() -> None:
    shared = video_map_edit.set_assignment(
        _MAPPING, "nl", "Glowbug.mpg", ("02987653000893", "02987653007434")
    )

    skipped = video_map_edit.set_assignment(shared, "nl", "Glowbug.mpg", (video_map_edit.SKIP,))
    rows = [r for r in video_map_edit.parse(skipped) if r.file == "Glowbug.mpg"]

    assert [(r.gtin, r.note) for r in rows] == [("skip", "# verify 'cliplamp' (0.93)")]


def test_dropping_a_spare_row_is_not_losing_a_video(tmp_path: Path) -> None:
    """The backstop counts videos, so a shared video going back to one barcode may be written."""
    path = tmp_path / "mapping.yml"
    path.write_text(
        video_map_edit.set_assignment(
            _MAPPING, "nl", "Glowbug.mpg", ("02987653000893", "02987653007434")
        ),
        encoding="utf-8",
    )

    video_map_edit.write_validated(
        path,
        video_map_edit.set_assignment(
            path.read_text("utf-8"), "nl", "Glowbug.mpg", ("02987653007434",)
        ),
    )

    assert path.read_text("utf-8") == _MAPPING


def test_a_shared_video_resolves_for_every_product_it_carries(tmp_path: Path) -> None:
    """The publish side reads per barcode, so each product finds the one shared file."""
    path = tmp_path / "mapping.yml"
    path.write_text(
        video_map_edit.set_assignment(
            _MAPPING, "nl", "Glowbug.mpg", ("02987653000893", "02987653007434")
        ),
        encoding="utf-8",
    )
    vmap = load_video_map(path)

    assert vmap.resolve("02987653000893", "nl") == "Glowbug.mpg"
    assert vmap.resolve("2987653007434", "nl") == "Glowbug.mpg"


def test_a_dropped_rows_comment_stays_in_the_file() -> None:
    """The comment says why that barcode was chosen. Losing the row must not lose the evidence."""
    text = (
        "nl:\n"
        '  - {file: "A.mpg", gtin: "02987653000893"}  # email 3 May\n'
        '  - {file: "A.mpg", gtin: "02987653007434"}  # email 9 May: A is also Glowbug\n'
    )

    edited = video_map_edit.set_assignment(text, "nl", "A.mpg", ("02987653000893",))

    assert edited == (
        "nl:\n"
        '  - {file: "A.mpg", gtin: "02987653000893"}  # email 3 May\n'
        "  # email 9 May: A is also Glowbug\n"
    )
    assert [r.gtin for r in video_map_edit.parse(edited)] == ["02987653000893"]


def test_a_filename_with_a_quote_in_it_is_written_as_that_file() -> None:
    name = 'Q "x" \\ y.mp4'
    text = f'nl:\n  - {{file: {video_map_edit._quoted(name)}, gtin: "02987653000893"}}\n'

    edited = video_map_edit.set_assignment(text, "nl", name, ("02987653000893", "02987653007434"))

    assert [(r.file, r.gtin) for r in video_map_edit.parse(edited)] == [
        (name, "02987653000893"),
        (name, "02987653007434"),
    ]


def test_a_video_added_for_a_rejected_sheet_row_becomes_a_change(tmp_path: Path) -> None:
    """The Data screen's *Add new video files* button, without the screen: the rejected row's
    file gets a blank row, the sheet is read again, and its barcode is now a change to apply."""
    path = tmp_path / "mapping.yml"
    path.write_text(_MAPPING, encoding="utf-8")
    sheet = Grid(["language", "file", "gtin"], [["nl", "New.mp4", "2987653008486"]])

    def decided() -> SignoffPlan:
        return plan(sheet, load_video_map(path), exported=set(), languages=["nl", "fr"])

    before = decided()
    missing = video_map_edit.files_missing_from_map(_MAPPING, {"nl": ["New.mp4"], "fr": []})
    wanted = addable(before, missing)
    assert wanted == {"nl": ["New.mp4"]}

    text = video_map_edit.append_rows(_MAPPING, "nl", wanted["nl"])
    video_map_edit.write_validated(path, text)

    [row] = decided().rows
    assert row.outcome == CHANGE
    assert _rows(path.read_text(encoding="utf-8"))[("nl", "4-in-1 Lamp.mpg")] == ""


def test_appended_rows_take_the_indent_of_the_rows_above_them(tmp_path: Path) -> None:
    """A mapping written with its rows flush left (as the demo's is) stays valid YAML."""
    flush = 'nl:\n- {file: A.mp4, gtin: ""}\nfr:\n- {file: B.mp4, gtin: ""}\n'
    path = tmp_path / "mapping.yml"
    path.write_text(flush, encoding="utf-8")

    text = video_map_edit.append_rows(flush, "nl", ["New.mp4"])
    video_map_edit.write_validated(path, text)

    assert '- {file: "New.mp4", gtin: ""}' in text.splitlines()
    assert ("nl", "New.mp4") in _rows(text)
