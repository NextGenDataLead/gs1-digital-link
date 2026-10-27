"""Unit tests for lib/quality_report_video.py — §1 of the data-quality report.

Rendered through :func:`lib.quality_report.render_quality_report`, because what matters is the
section as it sits in the document: its number, its neighbours, and the Summary rows that count it.
"""

from __future__ import annotations

import re

from lib.media_video import SKIP, VideoMap, VideoMapEntry
from lib.quality_report import render_quality_report
from lib.quality_report_video import NO_SHEET, TITLE, VIDEO_SAMPLE, SignoffReview, VideoReport
from lib.records import LocalisedText, ProductRecord, SourceIssue
from lib.video_signoff import (
    AMBIGUOUS,
    BLANK,
    CHANGE,
    REJECTED,
    UNCHANGED,
    FileChange,
    SignoffPlan,
    SignoffRow,
)
from lib.video_status import VideoStatus, video_status

_LANGUAGES = ["nl", "fr"]
_BOTH = "02987653000011"  # a video in nl and fr — publishes
_NL_ONLY = "02987653000028"  # no confirmed video in fr
_NOTHING = "02987653000035"  # no confirmed video in nl, fr
_CLASH = "02987653000042"  # two videos in nl, none in fr — held
_CLASH_ONLY = "02987653000059"  # two videos in nl, one in fr — NOT held, attaches no nl video


def _product(gtin: str, name: str = "") -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values={"nl": name or f"prod-{gtin[-3:]}"}),
    )


_MAP = VideoMap(
    by_language={
        "nl": [
            VideoMapEntry(file="both.mpg", gtin=_BOTH),
            VideoMapEntry(file="nl-only.mpg", gtin=_NL_ONLY),
            VideoMapEntry(file="clash-a.mpg", gtin=_CLASH),
            VideoMapEntry(file="clash-b.mpg", gtin=_CLASH),
            VideoMapEntry(file="solo-a.mpg", gtin=_CLASH_ONLY),
            VideoMapEntry(file="solo-b.mpg", gtin=_CLASH_ONLY),
            VideoMapEntry(file="nobody.mpg", gtin=""),
        ],
        "fr": [
            VideoMapEntry(file="both-fr.mpg", gtin=_BOTH),
            VideoMapEntry(file="solo-fr.mpg", gtin=_CLASH_ONLY),
        ],
    }
)
_ALL = [_BOTH, _NL_ONLY, _NOTHING, _CLASH, _CLASH_ONLY]


def _status(
    files: dict[str, list[str]] | None = None, *, publish_without_video: bool = False
) -> VideoStatus:
    on_disk = files
    if on_disk is None:
        on_disk = {lang: [e.file for e in entries] for lang, entries in _MAP.by_language.items()}
    return video_status(
        _MAP,
        [_product(g) for g in _ALL],
        languages=_LANGUAGES,
        files_by_language=on_disk,
        publish_without_video=publish_without_video,
    )


def _render(video: VideoReport | None, **over: object) -> str:
    base: dict[str, object] = {
        "client_id": "democlient",
        "source_issues": [],
        "generated_issues": [],
        "video_map_issues": [],
        "category_issues": [],
        "products": {},
        "snapshot": "2026-10-06",
        "freshness": {"generated": "—", "source": "—", "video_map": "—", "category": "—"},
        "languages": _LANGUAGES,
        "video": video,
    }
    base.update(over)
    return render_quality_report(**base)  # type: ignore[arg-type]


def _section(md: str) -> str:
    start = md.index(f"## 1. {TITLE}")
    return md[start : md.index("\n## 2. ", start)]


def _part(md: str, letter: str) -> str:
    section = _section(md)
    start = section.index(f"### 1{letter}.")
    following = re.search(r"^### ", section[start + 1 :], re.MULTILINE)
    return section[start : start + 1 + following.start()] if following else section[start:]


def _rows(text: str) -> list[list[str]]:
    return [
        [c.strip() for c in line.split("|")[1:-1]]
        for line in text.splitlines()
        if line.startswith("| `") or re.match(r"^\| \d+ \|", line)
    ]


# --- the section always exists, so nothing after it is renumbered ------------------------------


def test_with_no_mapping_the_section_is_one_line_and_still_numbered() -> None:
    md = _render(None)
    section = _section(md)

    assert "No readable video mapping" in section
    assert "### 1" not in section
    assert "## 2. Blocks publish" in md


def test_with_no_mapping_the_backlog_falls_back_to_the_file() -> None:
    """The one case where `video_map_issues.json` is the only record there is."""
    issues = [
        SourceIssue(
            gtin="",
            field="video.nl",
            source="s",
            issue="video_unconfirmed",
            value="a.mpg",
            detail="d",
        )
    ]
    section = _section(_render(None, video_map_issues=issues))

    assert "**1**" in section
    assert "- `nl` — a.mpg" in section


def test_the_four_parts_carry_the_sections_number_in_order() -> None:
    subheads = [
        line[:7]
        for line in _section(_render(VideoReport(_status()))).splitlines()
        if line[:4] == "### "
    ]

    assert subheads == ["### 1a.", "### 1b.", "### 1c.", "### 1d."]


# --- headline and 1a: the product side ----------------------------------------------------------


def test_the_headline_counts_what_publishes_and_what_is_held() -> None:
    """Five products: one attaches everywhere and four are held — including the one whose only
    problem is two videos in nl, because the page could not get an nl video."""
    section = _section(_render(VideoReport(_status())))

    assert "**1 of 5** in-scope products have a confirmed video in every language" in section
    assert "**4** are held" in section
    assert "also held for missing source data" not in section.split("###")[0]


def test_a_product_held_for_both_reasons_is_counted_once_and_marked() -> None:
    md = _render(
        VideoReport(_status()),
        mandatory_gaps={_NOTHING: []},  # E23 holds it too
    )
    held = _part(md, "a")

    assert "1 of those are also held for missing source data (E23)" in _section(md)
    assert f"`{_NOTHING}` ¹" in held
    assert f"`{_NL_ONLY}` |" in held  # no mark on the others


def test_held_lists_every_held_product_with_a_mark_per_language_and_what_it_waits_on() -> None:
    rows = {
        row[0].split()[0].strip("`"): row
        for row in _rows(_part(_render(VideoReport(_status())), "a"))
    }

    assert set(rows) == {_NL_ONLY, _NOTHING, _CLASH, _CLASH_ONLY}
    assert rows[_NL_ONLY][2:] == ["●", "○", "no confirmed video in fr"]
    assert rows[_NOTHING][2:] == ["○", "○", "no confirmed video in nl, fr"]
    assert rows[_CLASH][2:] == ["2", "○", "no confirmed video in fr; two videos in nl"]
    assert rows[_CLASH_ONLY][2:] == ["2", "●", "two videos in nl"]  # held for the clash alone


def test_held_is_unbounded_because_each_row_is_a_job() -> None:
    many = [f"029876530{n:05d}" for n in range(1, 41)]
    status = video_status(
        VideoMap(by_language={"nl": [], "fr": []}),
        [_product(g) for g in many],
        languages=_LANGUAGES,
        files_by_language={},
    )

    assert len(_rows(_part(_render(VideoReport(status)), "a"))) == 40


def test_held_says_it_is_the_product_side_and_what_joins_it_to_the_files() -> None:
    held = _part(_render(VideoReport(_status())), "a")

    assert "product side" in held and "file side" in held
    assert "sign-off sheet is what joins them" in held


# --- 1b: two videos -----------------------------------------------------------------------------


def test_two_videos_names_both_files_and_says_the_product_is_held() -> None:
    clash = _part(_render(VideoReport(_status())), "b")
    rows = _rows(clash)

    assert [(r[0].strip("`"), r[2], r[3]) for r in rows] == [
        (_CLASH, "nl", "clash-a.mpg, clash-b.mpg"),
        (_CLASH_ONLY, "nl", "solo-a.mpg, solo-b.mpg"),
    ]
    assert "**held**" in clash
    assert "mark the other `skip`" in clash


# --- 1c: the file side --------------------------------------------------------------------------


def test_the_backlog_is_the_unassigned_files_and_says_why_it_is_not_narrowed() -> None:
    backlog = _part(_render(VideoReport(_status())), "c")

    assert "**1** video files have no GTIN" in backlog
    assert "- `nl` — nobody.mpg" in backlog
    assert "names no product" in backlog
    assert "<details>" not in backlog


def test_the_backlog_samples_and_counts_the_rest() -> None:
    files = [f"clip{n:02d}.mpg" for n in range(25)]
    vmap = VideoMap(by_language={"nl": [VideoMapEntry(file=f, gtin="") for f in files]})
    status = video_status(vmap, [], languages=["nl"], files_by_language={"nl": files})
    backlog = _part(_render(VideoReport(status), languages=["nl"]), "c")

    assert len([line for line in backlog.splitlines() if line.startswith("- `")]) == VIDEO_SAMPLE
    assert f"…and {25 - VIDEO_SAMPLE} more" in backlog


def test_the_two_folder_gaps_the_report_never_showed_are_shown() -> None:
    on_disk = {"nl": ["both.mpg", "stray.mpg"], "fr": ["both-fr.mpg", "solo-fr.mpg"]}
    backlog = _part(_render(VideoReport(_status(on_disk))), "c")

    assert "**1** files are in a video folder with no row in the mapping" in backlog
    assert "Not in the mapping: `nl` stray.mpg" in backlog
    assert "Not in the folder:" in backlog and "`nl` clash-a.mpg" in backlog


# --- 1d: the sign-off sheet ---------------------------------------------------------------------


def _review() -> SignoffReview:
    def row(line: int, outcome: str, given: str = "", detail: str = "") -> SignoffRow:
        return SignoffRow(line, "nl", f"file{line}.mpg", given, "", outcome, detail)

    return SignoffReview(
        sheet="signoff-20261006-101500.xlsx",
        given_name="Videos Democlient v3.xlsx",
        at="2026-10-06",
        plan=SignoffPlan(
            (
                row(2, CHANGE, "2987653000011"),
                row(3, CHANGE, "2987653000028"),
                row(4, UNCHANGED, "2987653000035"),
                row(5, AMBIGUOUS, "2987653000066", "would give one GTIN two files in nl"),
                row(6, REJECTED, "8.7132E+12", "scientific notation — not a barcode"),
                row(7, BLANK),
            ),
            changes=(
                FileChange("nl", "file2.mpg", (), ("02987653000011",), (2,)),
                FileChange("nl", "old.mpg", ("02987653000011",), (SKIP,)),
            ),
            clashing=frozenset({"02987653000066"}),
        ),
    )


def test_no_sheet_is_one_ordinary_line() -> None:
    sheet = _part(_render(VideoReport(_status())), "d")

    assert NO_SHEET in sheet
    assert "warn" not in sheet.lower() and "⚠" not in sheet


def test_an_absent_reason_is_said_as_given() -> None:
    reason = "The sheet on file was edited after its columns were chosen."
    sheet = _part(_render(VideoReport(_status(), signoff_absent=reason)), "d")

    assert reason in sheet


def test_a_sheet_is_named_dated_and_counted_in_one_sentence() -> None:
    sheet = _part(_render(VideoReport(_status(), _review())), "d")

    assert "**Videos Democlient v3.xlsx**" in sheet and "2026-10-06" in sheet
    assert "signoff-20261006-101500.xlsx" in sheet
    assert (
        "Of its 6 rows: **2** would change a video's barcodes, 1 already match, **1** would give a "
        "product a second video in one language (1b lists the 2 already in the selection), **1** "
        "are rejected, and 1 are blank." in sheet
    )
    assert "cannot be applied as it is" in sheet, "a sheet with a two-video row is refused"


def test_what_the_mapping_would_become_is_a_table_of_videos_now_and_after() -> None:
    """Including a video no sheet row names: it changes because another row took its barcode."""
    sheet = _part(_render(VideoReport(_status(), _review())), "d")
    changes = sheet.partition("What the mapping would become")[2].partition("Two videos")[0]

    rows = [
        [c.strip() for c in line.split("|")[1:-1]]
        for line in changes.splitlines()
        if line.startswith("| nl |")
    ]
    assert rows == [
        ["nl", "file2.mpg", "unset", "02987653000011", "2"],
        ["nl", "old.mpg", "02987653000011", "skip", "— another row took its barcode"],
    ]


def test_two_video_rows_and_rejections_are_tables_led_by_the_sheets_own_row_number() -> None:
    """The rejection is the table the operator forwards verbatim, so the sheet's text is quoted."""
    sheet = _part(_render(VideoReport(_status(), _review())), "d")
    person, _, fix = sheet.partition("Rejected — for the client")
    two_videos = person.partition("Two videos for one product")[2]

    assert [row[0] for row in _rows(two_videos)] == ["5"]
    rejected = _rows(fix)
    assert rejected == [
        ["6", "nl", "file6.mpg", "8.7132E+12", "scientific notation — not a barcode"]
    ]


# --- the Summary counts what §1 lists -----------------------------------------------------------


def _summary_row(md: str, finding: str) -> list[str]:
    line = next(line for line in md.splitlines() if f"| {finding} |" in line)
    return [c.strip() for c in line.split("|")[1:-1]]


def test_the_summary_counts_from_the_same_status_as_the_section() -> None:
    md = _render(VideoReport(_status()), mandatory_gaps={_NOTHING: []})

    assert _summary_row(md, "**No confirmed video (E24)**")[2] == "4 GTINs (1 also E23)"
    unassigned = _summary_row(md, "Videos not yet mapped to a GTIN")
    assert unassigned[2] == "1"
    assert unassigned[4] == "Unknown — these name no product"
    assert _summary_row(md, "Products with two videos in one language")[2] == "2"
    assert _summary_row(md, "Sign-off sheet rows that need a person")[2] == "—"


def test_the_summary_counts_the_sheet_rows_that_need_a_person() -> None:
    md = _render(VideoReport(_status(), _review()))

    assert _summary_row(md, "Sign-off sheet rows that need a person")[2] == "2"


def test_a_review_renders_deterministically() -> None:
    assert _render(VideoReport(_status(), _review())) == _render(VideoReport(_status(), _review()))


def test_publishing_without_video_lists_the_bare_products_and_holds_only_the_clashes() -> None:
    section = _section(_render(VideoReport(_status(publish_without_video=True))))

    assert "### 1a. Published without a video in some language" in section
    assert "**2** publish with no video in at least one language" in section
    assert "**2** are held because two videos are confirmed for one language" in section
    one_a = section.split("### 1a.")[1].split("### 1b.")[0]
    assert _NL_ONLY in one_a
    assert _NOTHING in one_a
    assert _CLASH not in one_a
    assert _CLASH_ONLY not in one_a
    assert "it is in 1a too" not in section
