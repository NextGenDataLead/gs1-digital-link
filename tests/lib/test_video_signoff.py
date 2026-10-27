"""The client's sign-off sheet, read against the mapping — and every shape an EAN arrives in.

The sheet is the one external dependency this pilot has been waiting on, and it arrives as a
spreadsheet somebody typed in. So the barcode column is the risk: Excel will hand a 13-digit EAN
back as a number, as a whole-number float, with a leading zero only if it was stored as text, and as
``8.7132E+12`` if the column was ever too narrow. Those are not edge cases — they are what the file
looks like. The first test here is the one that matters: every one of those spellings has to land on
the same canonical GTIN-14, because that is what :func:`lib.media_video.fully_mapped_gtins` will
compare against forever.

The second thing pinned here is the operator's rule (2026-10-24): **the sheet leads, per barcode**.
A barcode the sheet names gets exactly the video(s) the sheet gives it — moved off any other video,
which becomes ``skip`` when it has none left — and a barcode the sheet does not name keeps its
video. One video may carry several barcodes. What may never come out is one barcode on two videos
in one language; such a sheet is refused whole, and ``edits`` — the only thing a caller can write —
is then empty.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from lib.media_video import SKIP, VideoMap, VideoMapEntry
from lib.video_signoff import (
    AMBIGUOUS,
    BLANK,
    CHANGE,
    REJECTED,
    UNCHANGED,
    Clash,
    column_options,
    columns,
    is_header,
    looks_like_an_export,
    mapping_context,
    plan,
    read_sheet,
)
from lib.xlsx import Grid, read_grid

#: The pilot's own languages, and a barcode that is really on its feed.
_LANGUAGES = ("nl", "fr")
_GTIN13 = "2987653008486"
_GTIN14 = "02987653008486"
_EXPORTED = frozenset({_GTIN14})


def _map(gtin: str = "", *, file: str = "Glowbug.mpg", language: str = "nl") -> VideoMap:
    """A mapping with one row, carrying whatever the test needs it to already say."""
    return VideoMap(by_language={language: [VideoMapEntry(file=file, gtin=gtin)], "fr": []})


def _sheet(
    *rows: tuple[str, str, str], header: tuple[str, str, str] = ("language", "file", "gtin")
) -> Grid:
    """A sign-off sheet as the reader would hand it over: header plus rows of text."""
    return Grid(list(header), [list(row) for row in rows])


def _only(grid: Grid, vmap: VideoMap, *, exported: frozenset[str] = _EXPORTED) -> object:
    """The single row a one-row sheet decides to, so a test can name one outcome."""
    decided = plan(grid, vmap, exported=exported, languages=_LANGUAGES)
    assert len(decided.rows) == 1, decided.rows
    return decided.rows[0]


def test_every_spelling_of_one_barcode_lands_on_the_same_gtin() -> None:
    """The whole reason this module normalises rather than trusting the cell.

    Each of these is the same product. A 13-digit EAN typed plain, the same stored as text with its
    leading zero, the whole-number float Excel produces from a numeric cell, and the two ways people
    write a barcode down by hand.
    """
    spellings = [_GTIN13, _GTIN14, "2987653008486.0", "298-7653008486", "2987 653008486"]

    for given in spellings:
        row = _only(_sheet(("nl", "Glowbug.mpg", given)), _map())
        assert row.outcome == CHANGE, (given, row)
        assert row.gtin == _GTIN14, given


def test_scientific_notation_is_refused_with_the_fix_in_the_message() -> None:
    """The commonest way a barcode arrives broken, and the least obvious to whoever sent it.

    Stripping the ``E`` and padding what is left would produce a real-looking GTIN-14 for a
    product nobody chose, so this refuses — and says what to do, because "expected digits" gets
    the identical file sent back.
    """
    row = _only(_sheet(("nl", "Glowbug.mpg", "8.7132E+12")), _map())

    assert row.outcome == REJECTED
    assert "scientific notation" in row.detail
    assert "text" in row.detail


def test_too_many_digits_is_refused_rather_than_padded() -> None:
    """``zfill`` does not truncate, so this would read as "not in the export" — the wrong fault."""
    row = _only(_sheet(("nl", "Glowbug.mpg", "298765300848612345")), _map())

    assert row.outcome == REJECTED
    assert "18 digits" in row.detail


def test_a_cell_that_is_not_a_barcode_at_all_is_refused() -> None:
    """``canon_gtin`` strips every non-digit, so "see tab 2" would become a plausible GTIN-14."""
    row = _only(_sheet(("nl", "Glowbug.mpg", "see tab 2")), _map())

    assert row.outcome == REJECTED
    assert "not a barcode" in row.detail


def test_skip_is_an_answer_and_fills_like_a_gtin() -> None:
    """ "No product for this video" is a decision the client is entitled to make, not a gap."""
    row = _only(_sheet(("nl", "Glowbug.mpg", "Skip")), _map())

    assert row.outcome == CHANGE
    assert row.gtin == SKIP


def test_a_blank_cell_is_not_signed_off_and_not_an_error() -> None:
    """Most of a first-round sheet is blank. Calling that a problem would bury the real ones."""
    assert _only(_sheet(("nl", "Glowbug.mpg", "")), _map()).outcome == BLANK


def test_a_barcode_the_sheet_does_not_name_keeps_its_video_so_the_video_is_shared() -> None:
    """Rule (b), the operator's choice: the barcode leads, not the row.

    The mapping gives Glowbug to 00291111003190; the sheet gives it to another barcode and never
    mentions 00291111003190. That barcode keeps its video, so Glowbug now carries both.
    """
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", _GTIN13)),
        _map("00291111003190"),
        exported=_EXPORTED,
        languages=_LANGUAGES,
    )

    assert decided.rows[0].outcome == CHANGE
    # Both barcodes, in sorted order.
    assert decided.edits == {("nl", "Glowbug.mpg"): tuple(sorted(("00291111003190", _GTIN14)))}


def test_the_same_answer_in_a_different_width_is_not_a_conflict() -> None:
    """The mapping is written with 13-digit GTINs and the sheet may carry 14, or the reverse.

    Compared as text this reads as a disagreement, and every already-signed-off row in the file
    would come back as a conflict — which would make the import useless on the second round.
    """
    row = _only(_sheet(("nl", "Glowbug.mpg", _GTIN14)), _map(_GTIN13))

    assert row.outcome == UNCHANGED


def test_a_gtin_the_export_does_not_carry_is_refused() -> None:
    """A transposed digit usually produces a barcode no product has. That is the catchable case."""
    row = _only(_sheet(("nl", "Glowbug.mpg", "2987653008468")), _map())

    assert row.outcome == REJECTED
    assert "not in the export" in row.detail


def test_with_no_export_read_the_export_check_is_skipped() -> None:
    """Otherwise an import attempted before ``parse_export`` rejects every row for the same wrong
    reason, and the operator is sent to MyGS1 over a file they have not parsed yet."""
    row = _only(_sheet(("nl", "Glowbug.mpg", _GTIN13)), _map(), exported=frozenset())

    assert row.outcome == CHANGE


def test_a_language_the_client_invented_is_refused() -> None:
    """Filling a block no run reads is worse than refusing: it looks like the work was done."""
    row = _only(_sheet(("de", "Glowbug.mpg", _GTIN13)), _map())

    assert row.outcome == REJECTED
    assert "not a configured language" in row.detail


def test_a_filename_the_mapping_does_not_have_is_refused_as_a_filename() -> None:
    """Checked before the barcode on purpose: a good GTIN against an unknown file is still nothing
    this import can do, and "not in the export" would send somebody to MyGS1 over a typo."""
    row = _only(_sheet(("nl", "Glowbugg.mpg", _GTIN13)), _map())

    assert row.outcome == REJECTED
    assert "no nl row for this file" in row.detail


def test_rejections_name_the_spreadsheet_row_the_reader_is_looking_at() -> None:
    """Line 1 is the header to whoever opens the file, so the first data row is line 2."""
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", ""), ("nl", "Glowbug.mpg", "nope")),
        _map(),
        exported=_EXPORTED,
        languages=_LANGUAGES,
    )

    assert [row.line for row in decided.rows] == [2, 3]


def test_our_own_report_is_read_back_without_mistaking_a_hint_for_an_answer() -> None:
    """The client gets ``report_video_candidates``'s sheet, which carries ``candidate_1_gtin``
    beside ``gtin``. Matched loosely, the first *suggestion* would be read as the decision."""
    grid = Grid(
        [
            "language",
            "file",
            "normalized",
            "state",
            "gtin",
            "candidate_1_gtin",
            "candidate_1_score",
        ],
        [["nl", "Glowbug.mpg", "glowbug", "unset", _GTIN13, "00291111003190", "0.93"]],
    )

    where = columns(grid)

    assert where is not None
    assert where["gtin"] == 4
    assert plan(grid, _map(), exported=_EXPORTED, languages=_LANGUAGES).rows[0].gtin == _GTIN14


def test_the_barcode_column_is_found_under_an_earlier_reports_name_for_it() -> None:
    """``current_gtin`` is what an earlier ``report_video_candidates`` called the column, and the
    operator still holds sheets in that shape — the one in ``test-uploads`` carries 99 answers.

    Safe by construction, because in both schemas that single column is *both* the mapping's current
    value and where the client writes theirs: an untouched row decides UNCHANGED, an edited one
    CHANGE.
    """
    grid = _sheet(("fr", "Glowbug.mpg", _GTIN13), header=("language", "file", "current_gtin"))

    assert columns(grid) == {"language": 0, "file": 1, "gtin": 2}
    assert is_header(["language", "file", "state", "current_gtin"])


def test_the_columns_are_found_under_the_names_a_client_would_use() -> None:
    """Nobody outside this project calls a barcode a GTIN."""
    grid = _sheet(("fr", "Glowbug.mpg", _GTIN13), header=("Language Code", "Filename", "EAN"))

    assert columns(grid) == {"language": 0, "file": 1, "gtin": 2}


def test_a_sheet_with_no_barcode_column_decides_nothing() -> None:
    """Rather than reading some other column as barcodes. The caller says so on screen."""
    grid = _sheet(("nl", "Glowbug.mpg", "x"), header=("language", "file", "comments"))

    assert columns(grid) is None
    assert plan(grid, _map(), exported=_EXPORTED, languages=_LANGUAGES).rows == ()


def test_the_header_row_needs_both_a_barcode_and_a_file_column() -> None:
    """A title row above the table often carries one word, and a data row can carry "file"."""
    assert is_header(["language", "file", "gtin"])
    assert not is_header(["Video sign-off — round 2"])
    assert not is_header(["gtin"])


def _workbook(*rows: tuple[str, str, object]) -> io.BytesIO:
    """A real .xlsx, shaped like the file a client actually sends back.

    Three things about it are not incidental, and each has broken a reader here before: the table
    is on the **second** sheet, it starts **below a title row**, and the barcode goes in as a
    **number**, which is what a spreadsheet does with 13 digits unless the column was set to text.
    """
    wb = Workbook()
    wb.active["A1"] = "a pivot nobody asked for"
    sheet = wb.create_sheet("Sign-off")
    sheet["A1"] = "Video sign-off — round 2"
    sheet.append([])
    sheet.append(["Language Code", "Filename", "EAN", "Notes"])
    for row in rows:
        sheet.append([*row, ""])
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


def test_a_real_workbook_is_read_from_a_stream_and_the_table_is_found() -> None:
    """The upload is never written to disk, so the reader is handed bytes rather than a path.

    This is the whole path at once: a stream, a second sheet, a title row above the table, the
    client's own column names, and a barcode that arrived as a number.
    """
    grid = read_grid(_workbook(("nl", "Glowbug.mpg", int(_GTIN13))), header_row=is_header)

    assert grid is not None
    assert grid.header == ["Language Code", "Filename", "EAN", "Notes"]

    decided = plan(grid, _map(), exported=_EXPORTED, languages=_LANGUAGES)

    assert [(row.outcome, row.gtin) for row in decided.rows] == [(CHANGE, _GTIN14)]


def test_a_numeric_cell_and_a_text_cell_are_the_same_barcode_through_a_real_file() -> None:
    """Checked through a workbook, not only through the coercion, because the cell *type* is where
    this goes wrong: whether a leading zero survives is decided in Excel, not here."""
    grid = read_grid(
        _workbook(("nl", "Glowbug.mpg", int(_GTIN13)), ("fr", "Glowbug.mpg", _GTIN14)),
        header_row=is_header,
    )

    assert grid is not None
    vmap = VideoMap(
        by_language={
            "nl": [VideoMapEntry(file="Glowbug.mpg", gtin="")],
            "fr": [VideoMapEntry(file="Glowbug.mpg", gtin="")],
        }
    )
    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert {row.gtin for row in decided.rows} == {_GTIN14}


def test_a_sheet_whose_columns_are_called_nothing_we_know_still_arrives_on_screen() -> None:
    """The reason the column picker can exist at all.

    The accepted spellings are a guess about somebody else's spreadsheet, and the guess was wrong
    about the only real sheet there is. A file we cannot recognise must still come back with its
    headings listed, so the operator can say which column is which — being told to go and rename
    things in Excel to match a list the tool never shows is not a fix.
    """
    wb = Workbook()
    sheet = wb.active
    sheet.append(["kolom A", "het filmpje", "streepjescode"])
    sheet.append(["nl", "Glowbug.mpg", _GTIN13])
    stream = io.BytesIO()
    wb.save(stream)

    grid = read_sheet(stream.getvalue())

    assert grid is not None
    assert columns(grid) is None, "nothing here is recognisable — that is the premise"
    assert column_options(grid) == {0: "kolom A", 1: "het filmpje", 2: "streepjescode"}


def test_the_operators_own_column_choice_overrides_every_guess() -> None:
    """What the picker sends back. Three positions, and the names are not consulted again."""
    grid = Grid(["kolom A", "het filmpje", "streepjescode"], [["nl", "Glowbug.mpg", _GTIN13]])

    decided = plan(
        grid,
        _map(),
        exported=_EXPORTED,
        languages=_LANGUAGES,
        where={"language": 0, "file": 1, "gtin": 2},
    )

    assert [(row.outcome, row.gtin) for row in decided.rows] == [(CHANGE, _GTIN14)]


def test_a_column_with_no_heading_is_still_offered_by_position() -> None:
    """It may be the one wanted, and a sheet exported from a report often has one."""
    grid = Grid(["language", "", "gtin"], [["nl", "Glowbug.mpg", _GTIN13]])

    assert column_options(grid)[1] == "(column 2, no heading)"


def test_the_fallback_header_never_swallows_a_title_row() -> None:
    """A one-cell title above the table is not a header, however loose the fallback is."""
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Video sign-off — round 2"])
    sheet.append([])
    sheet.append(["taal", "video", "code"])
    sheet.append(["nl", "Glowbug.mpg", _GTIN13])
    stream = io.BytesIO()
    wb.save(stream)

    grid = read_sheet(stream.getvalue())

    assert grid is not None
    assert grid.header == ["taal", "video", "code"]


def test_two_sheet_rows_claiming_one_barcode_are_both_held_back() -> None:
    """What the pilot's own sheet does: `Coil Light Summer.mpg` and `…Winter.mpg`, one GTIN.

    Two genuine videos of one product, which the mapping cannot express. Applying both attaches
    **neither** — ``resolve`` returns ``None`` for an ambiguous pair — to a product that still
    counts as mapped, so the page publishes with no video and nothing says why.
    """
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Coil Light Summer.mpg", gtin=""),
                VideoMapEntry(file="Coil Light Winter.mpg", gtin=""),
            ],
            "fr": [],
        }
    )
    grid = _sheet(
        ("nl", "Coil Light Summer.mpg", _GTIN13), ("nl", "Coil Light Winter.mpg", _GTIN13)
    )

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert [row.outcome for row in decided.rows] == [AMBIGUOUS, AMBIGUOUS]
    assert decided.edits == {}, "neither may be applied — one of them has to be chosen by a person"
    assert "Coil Light Winter.mpg" in decided.rows[0].detail, "each names its rival"


def test_naming_a_barcode_on_another_video_moves_it_and_the_old_video_becomes_skip() -> None:
    """The collision that used to be held back is now a move: the sheet leads for the barcode."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Mega Snare.mp4", gtin=_GTIN13),
                VideoMapEntry(file="Mega Snare.mpg", gtin=""),
            ],
            "fr": [],
        }
    )

    decided = plan(
        _sheet(("nl", "Mega Snare.mpg", _GTIN14)), vmap, exported=_EXPORTED, languages=_LANGUAGES
    )

    assert decided.rows[0].outcome == CHANGE
    assert decided.edits == {
        ("nl", "Mega Snare.mp4"): (SKIP,),
        ("nl", "Mega Snare.mpg"): (_GTIN14,),
    }
    moved = next(change for change in decided.changes if change.file == "Mega Snare.mp4")
    assert moved.lines == (), "no sheet row names it — another row took its barcode"
    assert decided.losing == (), "the product still has a video, just a different one"


def test_the_operators_case_one_row_per_product_clears_the_second_video() -> None:
    """2026-10-24: the mapping had two Dutch videos for one product, and the sheet names one.

    `Coil Light Winter.mpg`'s cell is blank — that alone changes nothing — but the barcode it held
    is named on Summer, so Winter loses it, becomes ``skip``, and the clash is solved.
    """
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Coil Light Summer.mpg", gtin=_GTIN14),
                VideoMapEntry(file="Coil Light Winter.mpg", gtin=_GTIN14),
            ],
            "fr": [],
        }
    )
    grid = _sheet(("nl", "Coil Light Summer.mpg", _GTIN13), ("nl", "Coil Light Winter.mpg", ""))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert [row.outcome for row in decided.rows] == [UNCHANGED, BLANK]
    assert decided.edits == {("nl", "Coil Light Winter.mpg"): (SKIP,)}
    assert decided.resolved == frozenset({_GTIN14})
    assert mapping_context(decided, vmap, _LANGUAGES).clashing_products == 0


def test_a_blank_cell_alone_changes_nothing() -> None:
    """A barcode no row names keeps its video, so a blank cell is not a way to clear one."""
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", "")), _map(_GTIN13), exported=_EXPORTED, languages=_LANGUAGES
    )

    assert decided.changes == ()


def test_several_barcodes_in_one_cell_share_the_video() -> None:
    other = "02987653000893"
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", f"{_GTIN13}, 2987653000893")),
        _map(),
        exported=_EXPORTED | {other},
        languages=_LANGUAGES,
    )

    assert decided.rows[0].outcome == CHANGE
    assert decided.edits == {("nl", "Glowbug.mpg"): (other, _GTIN14)}


def test_one_video_on_two_sheet_rows_carries_both_barcodes() -> None:
    """The other way to write a shared video — the mapping's own shape, one row per barcode."""
    other = "02987653000893"
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", _GTIN13), ("nl", "Glowbug.mpg", other)),
        _map(),
        exported=_EXPORTED | {other},
        languages=_LANGUAGES,
    )

    assert decided.edits == {("nl", "Glowbug.mpg"): (other, _GTIN14)}


def test_one_bad_barcode_in_a_cell_rejects_the_whole_cell() -> None:
    """Applying the good half would silently drop the product the bad half was meant to name."""
    row = _only(_sheet(("nl", "Glowbug.mpg", f"{_GTIN13}, EAN 871")), _map())

    assert row.outcome == REJECTED
    assert "EAN 871" in row.detail


def test_skip_beside_a_barcode_is_refused() -> None:
    row = _only(_sheet(("nl", "Glowbug.mpg", f"skip, {_GTIN13}")), _map())

    assert row.outcome == REJECTED


def test_two_rows_saying_skip_and_a_barcode_for_one_video_are_both_refused() -> None:
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", "skip"), ("nl", "Glowbug.mpg", _GTIN13)),
        _map(),
        exported=_EXPORTED,
        languages=_LANGUAGES,
    )

    assert [row.outcome for row in decided.rows] == [REJECTED, REJECTED]
    assert decided.edits == {}


def test_skip_takes_a_video_off_every_product_and_says_who_is_left_without_one() -> None:
    """The one way a sheet takes a barcode the sheet does not otherwise name off a video."""
    decided = plan(
        _sheet(("nl", "Glowbug.mpg", "skip")),
        _map(_GTIN13),
        exported=_EXPORTED,
        languages=_LANGUAGES,
    )

    assert decided.edits == {("nl", "Glowbug.mpg"): (SKIP,)}
    assert decided.losing == (("nl", _GTIN14),)


def test_a_refused_sheet_applies_nothing_not_even_its_good_rows() -> None:
    """Its rows move barcodes between videos; half of a move is a mapping nobody asked for."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="a.mpg", gtin=""),
                VideoMapEntry(file="b.mpg", gtin=""),
                VideoMapEntry(file="c.mpg", gtin=""),
            ],
            "fr": [],
        }
    )
    grid = _sheet(("nl", "a.mpg", _GTIN13), ("nl", "b.mpg", _GTIN13), ("nl", "c.mpg", "skip"))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert decided.refused
    assert decided.rows[2].outcome == CHANGE
    assert decided.edits == {}


def test_one_barcode_in_two_languages_is_the_normal_case_and_not_a_clash() -> None:
    """A product needs a video in *every* language, so this must stay the ordinary path."""
    vmap = VideoMap(
        by_language={
            "nl": [VideoMapEntry(file="Glowbug.mpg", gtin="")],
            "fr": [VideoMapEntry(file="Glowbug FR.mpg", gtin="")],
        }
    )
    grid = _sheet(("nl", "Glowbug.mpg", _GTIN13), ("fr", "Glowbug FR.mpg", _GTIN13))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert [row.outcome for row in decided.rows] == [CHANGE, CHANGE]
    assert len(decided.edits) == 2


def test_many_files_may_be_skip_without_clashing() -> None:
    """``skip`` is not a product, so it cannot collide with another ``skip``."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Trailer A.mpg", gtin=""),
                VideoMapEntry(file="Trailer B.mpg", gtin=""),
            ],
            "fr": [],
        }
    )
    grid = _sheet(("nl", "Trailer A.mpg", "skip"), ("nl", "Trailer B.mpg", "skip"))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert [row.outcome for row in decided.rows] == [CHANGE, CHANGE]
    assert len(decided.edits) == 2


def test_a_gs1_export_is_recognised_as_one_not_read_as_a_sheet() -> None:
    """The export's own opening headings, as lib.gdsn_layout writes them for the demo client."""
    export = Grid(
        header=["Gtin", "TargetMarketCountryCode", "InformationProviderOfTradeItem"], rows=[]
    )
    sheet = Grid(header=["language", "file", "gtin"], rows=[])

    assert looks_like_an_export(export)
    assert not looks_like_an_export(sheet)  # a "gtin" column alone is an ordinary sign-off sheet


# --- mapping_context: the figures beside a plan agree with the report's -------------------------


def test_a_sheet_repeating_a_clash_already_in_the_mapping_leaves_it_and_applies_the_rest() -> None:
    """A returned sheet repeats every barcode it was sent with, so an existing clash comes back on
    both videos. Refusing that refused every round-trip of a mapping with one clash in it — the
    fill on another row included (review, 2026-10-24). Left alone, it stays held and counted."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Mega Snare.mp4", gtin=_GTIN13),
                VideoMapEntry(file="Mega Snare.mpg", gtin=_GTIN14),
                VideoMapEntry(file="Glowbug.mpg", gtin=""),
            ],
            "fr": [],
        }
    )
    other = "02987653000893"
    decided = plan(
        _sheet(
            ("nl", "Mega Snare.mp4", _GTIN13),
            ("nl", "Mega Snare.mpg", _GTIN13),
            ("nl", "Glowbug.mpg", other),
        ),
        vmap,
        exported=_EXPORTED | {other},
        languages=_LANGUAGES,
    )

    context = mapping_context(decided, vmap, _LANGUAGES)

    assert not decided.refused
    assert [row.outcome for row in decided.rows] == [UNCHANGED, UNCHANGED, CHANGE]
    assert decided.edits == {("nl", "Glowbug.mpg"): (other,)}
    assert context.clashes == (Clash(_GTIN14, "nl", ("Mega Snare.mp4", "Mega Snare.mpg")),)
    assert context.new_clashes == frozenset()
    assert context.clashing_products == 1


def test_a_sheet_adding_a_third_video_to_an_existing_clash_is_refused() -> None:
    """Changing a clash is still making one: the sheet gives the product yet another video."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="a.mp4", gtin=_GTIN13),
                VideoMapEntry(file="a.mpg", gtin=_GTIN13),
                VideoMapEntry(file="a.mov", gtin=""),
            ],
            "fr": [],
        }
    )
    grid = _sheet(("nl", "a.mp4", _GTIN13), ("nl", "a.mpg", _GTIN13), ("nl", "a.mov", _GTIN13))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert decided.refused
    assert decided.resolved == frozenset(), "a refused sheet solves nothing"


def test_a_barcode_on_two_videos_cannot_lose_a_video_it_never_showed() -> None:
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="a.mp4", gtin=_GTIN13),
                VideoMapEntry(file="a.mpg", gtin=_GTIN13),
            ],
            "fr": [],
        }
    )
    grid = _sheet(("nl", "a.mp4", "skip"), ("nl", "a.mpg", "skip"))

    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert decided.losing == ()


def test_thousands_separators_are_not_read_as_several_barcodes() -> None:
    """With no export to check against, ``8,713,195,008,509`` used to apply four padded GTINs."""
    row = _only(_sheet(("nl", "Glowbug.mpg", "8,713,195,008,486")), _map(), exported=frozenset())

    assert row.outcome == REJECTED
    assert "thousands separators" in row.detail


def test_a_clash_this_sheet_would_create_is_counted_apart_from_the_mapping() -> None:
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Coil Light Summer.mpg", gtin=""),
                VideoMapEntry(file="Coil Light Winter.mpg", gtin=""),
            ],
            "fr": [],
        }
    )
    grid = _sheet(
        ("nl", "Coil Light Summer.mpg", _GTIN13), ("nl", "Coil Light Winter.mpg", _GTIN13)
    )
    decided = plan(grid, vmap, exported=_EXPORTED, languages=_LANGUAGES)

    context = mapping_context(decided, vmap, _LANGUAGES)

    assert context.clashes == ()
    assert context.new_clashes == frozenset({_GTIN14})
    assert context.clashing_products == 1


def test_unset_is_the_mappings_rows_with_no_gtin_whatever_the_sheet_says() -> None:
    """The doctor's figure — 16 left blank plus 2 rejected made 18 on the pilot."""
    vmap = VideoMap(
        by_language={
            "nl": [VideoMapEntry(file="a.mpg", gtin=""), VideoMapEntry(file="b.mpg", gtin="skip")],
            "fr": [VideoMapEntry(file="c.mpg", gtin="")],
        }
    )
    decided = plan(_sheet(("nl", "a.mpg", "")), vmap, exported=_EXPORTED, languages=_LANGUAGES)

    assert mapping_context(decided, vmap, _LANGUAGES).unset == 2


def test_a_sheet_naming_a_clashing_barcode_on_one_video_solves_the_clash() -> None:
    """The sheet leads: a.mov gets the barcode, and the two videos that held it become skip."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="a.mp4", gtin=_GTIN13),
                VideoMapEntry(file="a.mpg", gtin=_GTIN13),
                VideoMapEntry(file="a.mov", gtin=""),
            ],
            "fr": [],
        }
    )
    decided = plan(_sheet(("nl", "a.mov", _GTIN13)), vmap, exported=_EXPORTED, languages=_LANGUAGES)

    context = mapping_context(decided, vmap, _LANGUAGES)

    assert decided.rows[0].outcome == CHANGE
    assert decided.edits == {
        ("nl", "a.mp4"): (SKIP,),
        ("nl", "a.mpg"): (SKIP,),
        ("nl", "a.mov"): (_GTIN14,),
    }
    assert context.new_clashes == frozenset()
    assert context.clashing_products == 0
