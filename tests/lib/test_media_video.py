"""Unit tests for the video name→GTIN mapping and transcode step (Phase 9.5 media)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib.errors import VideoMapError
from lib.media_video import (
    VideoMap,
    VideoMapEntry,
    canon_gtin,
    check_video_map,
    clashing_gtins,
    files_by_language,
    fully_mapped_gtins,
    list_video_files,
    load_video_map,
    normalize_video_name,
    prepare_video,
    rank_candidates,
    summarize_video_map,
)
from lib.records import LocalisedText, ProductRecord


def _product(
    gtin: str,
    *,
    nl: str = "",
    fr: str = "",
    extras: dict[str, str] | None = None,
    extras_localised: dict[str, LocalisedText] | None = None,
) -> ProductRecord:
    values = {k: v for k, v in {"nl": nl, "fr": fr}.items() if v}
    return ProductRecord(
        gtin=gtin,
        brand="Democlient",
        product_name=LocalisedText(values=values or {"nl": gtin}),
        extras=extras or {},
        extras_localised=extras_localised or {},
    )


# --- normalize_video_name ----------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("BonnetShine_NL_SmallV2.mpg", "bonnet shine"),
        ("FlyCatch_NL_Small.mpg", "fly catch"),
        ("PipeSticks_NL.mpeg", "pipe sticks"),
        ("PaperKnife_NL_small.mpeg", "paper knife"),
        ("Silly Cat_FR.mpg", "silly cat"),
        ("CedarPillow FR.mpg", "cedar pillow"),
        ("KneePillow_FR_NEW.mpeg", "knee pillow"),
        ("4-in-1 Lamp.mpg", "4 in 1 lamp"),
        ("Hedge Snipper.mpg", "hedge snipper"),
    ],
)
def test_normalize_strips_lang_size_version_and_splits_camelcase(
    filename: str, expected: str
) -> None:
    assert normalize_video_name(filename) == expected


# --- list_video_files --------------------------------------------------------


def test_list_video_files_skips_system_dirs_and_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "A_NL.mpeg").write_bytes(b"x")
    (tmp_path / "B.mpg").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    svi = tmp_path / "System Volume Information"
    svi.mkdir()
    (svi / "junk.mpg").write_bytes(b"x")

    files = list_video_files(tmp_path)
    names = sorted(p.name for p in files)
    assert names == ["A_NL.mpeg", "B.mpg"]


def test_files_by_language_lists_each_folder_by_name_and_tolerates_an_absent_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "NL").mkdir()
    (tmp_path / "NL" / "b.mpg").write_bytes(b"x")
    (tmp_path / "NL" / "a.mp4").write_bytes(b"x")

    files = files_by_language({"nl": str(tmp_path / "NL"), "fr": str(tmp_path / "FR")})

    assert files == {"nl": ["a.mp4", "b.mpg"], "fr": []}


# --- rank_candidates (hints only) --------------------------------------------


def test_rank_candidates_reads_a_name_extra_the_record_carries_per_language() -> None:
    """Both name extras are `localised: true`, so a current parse puts them in `extras_localised`.

    A loop over flat `extras` alone found nothing there, which left every hint in the pilot scored
    on `product_name` and nothing else — and nothing failed, because the only test of this branch
    built a flat extra the parser no longer produces.
    """
    products = [
        _product(
            "02987653007434",
            nl="handige lamp",
            extras_localised={"logistics_name": LocalisedText(values={"nl": "Bulb man lamp wt"})},
        ),
        _product("02987653000001", nl="iets heel anders"),
    ]

    ranked = rank_candidates("bulb man", products, top_n=3)

    assert ranked[0].gtin == "02987653007434"
    assert ranked[0].field == "extras.logistics_name.nl"  # the language is part of the label
    assert ranked[0].score > ranked[1].score


def test_rank_candidates_reaches_every_language_of_a_name_extra() -> None:
    """The filenames are English, and the feed's English is usually in the *French* value.

    This product's `product_name` reads "huisdierspeelgoed" / "Jouets chiens" — neither is the
    filename. `marketing_name.fr` is "Democlient Pet Pal". Reading one language, whichever one it
    is, misses that.
    """
    products = [
        _product(
            "02987653007434",
            nl="huisdierspeelgoed",
            fr="Jouets chiens",
            extras_localised={
                "marketing_name": LocalisedText(
                    values={"nl": "Democlient Huisdierspeelgoed pluche", "fr": "Democlient Pet Pal"}
                )
            },
        )
    ]

    ranked = rank_candidates("pet pal", products, top_n=3)

    assert ranked[0].field == "extras.marketing_name.fr"


def test_rank_candidates_still_reads_a_name_extra_the_record_carries_flat() -> None:
    """A `products.json` written before `extras_localised` holds one flat string per extra.

    Whether a value is per-language is the record's fact, not the config's: `localised` is a
    per-field switch another client need not set, and the draft reads a file from disk rather than
    a fresh parse. Dropping this branch returns those hints to `product_name` alone.
    """
    products = [
        _product("02987653007434", nl="handige lamp", extras={"logistics_name": "Bulb man"}),
        _product("02987653000001", nl="iets heel anders"),
    ]

    ranked = rank_candidates("bulb man", products, top_n=3)

    assert ranked[0].gtin == "02987653007434"
    assert ranked[0].field == "extras.logistics_name"  # no language: the record carries none


def test_rank_candidates_never_offers_a_blank_value_as_a_certainty() -> None:
    """Two empty strings compare 1.00, and a video named for its language alone normalizes to one.

    So a language an extra carries blank would rank first at a perfect score with no name to show
    for it — in the operator's shell, a click-to-fill button reading "02987653007434 ·  (1.00)".
    """
    products = [
        _product(
            "02987653007434",
            nl="handige lamp",
            extras_localised={"logistics_name": LocalisedText(values={"nl": "", "fr": "Lampe"})},
        )
    ]

    ranked = rank_candidates(normalize_video_name("NL.mpg"), products, top_n=3)

    assert ranked[0].name
    assert ranked[0].score < 1.0


def test_rank_candidates_pure_miss_scores_low() -> None:
    products = [_product("02987653000001", nl="reinigingssticks voor je afvoer")]
    ranked = rank_candidates("fly catch", products, top_n=3)
    assert ranked[0].score < 0.5


# --- VideoMap.resolve --------------------------------------------------------


def _map(data: dict[str, list[dict[str, str]]]) -> VideoMap:
    return VideoMap.model_validate({"by_language": data})


def test_canon_gtin_pads_to_14_and_strips_nondigits() -> None:
    assert canon_gtin("2987653007717") == "02987653007717"
    assert canon_gtin("02987653007717") == "02987653007717"
    assert canon_gtin(" 2987653007717 ") == "02987653007717"


def test_resolve_matches_across_gtin_digit_forms() -> None:
    # mapping keyed 13-digit; pipeline queries 14-digit — must still match (the live bug).
    vmap = _map({"nl": [{"file": "PipeSticks_NL.mpeg", "gtin": "2987653001234"}]})
    assert vmap.resolve("02987653001234", "nl") == "PipeSticks_NL.mpeg"
    assert vmap.resolve("2987653001234", "nl") == "PipeSticks_NL.mpeg"


def test_fully_mapped_gtins_requires_all_languages() -> None:
    vmap = _map(
        {
            "nl": [
                {"file": "a_nl.mp4", "gtin": "2987653000001"},
                {"file": "b_nl.mp4", "gtin": "2987653000002"},  # nl-only
                {"file": "c_nl.mp4", "gtin": "skip"},
            ],
            "fr": [
                {"file": "a_fr.mp4", "gtin": "2987653000001"},
                {"file": "d_fr.mp4", "gtin": ""},  # blank
            ],
        }
    )
    both = fully_mapped_gtins(vmap, ["nl", "fr"])
    assert both == frozenset({"02987653000001"})  # canonical 14-digit; only the both-mapped one


def test_resolve_returns_confirmed_filename() -> None:
    vmap = _map({"nl": [{"file": "PipeSticks_NL.mpeg", "gtin": "02987653001234"}]})
    assert vmap.resolve("02987653001234", "nl") == "PipeSticks_NL.mpeg"


def test_resolve_blank_gtin_returns_none() -> None:
    vmap = _map({"nl": [{"file": "FlyCatch_NL.mpg", "gtin": ""}]})
    assert vmap.resolve("02987653001234", "nl") is None


def test_resolve_unknown_pair_returns_none() -> None:
    vmap = _map({"nl": [{"file": "A.mpg", "gtin": "02987653001234"}]})
    assert vmap.resolve("02987653009999", "fr") is None


def test_resolve_ambiguous_gtin_returns_none() -> None:
    vmap = _map(
        {
            "nl": [
                {"file": "A.mpg", "gtin": "02987653001234"},
                {"file": "B.mpg", "gtin": "02987653001234"},
            ]
        }
    )
    assert vmap.resolve("02987653001234", "nl") is None


def test_load_video_map_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "mapping.yml"
    path.write_text(
        "nl:\n"
        '  - {file: "PipeSticks_NL.mpeg", gtin: "02987653001234"}\n'
        "fr:\n"
        '  - {file: "PipeSticks_FR.mpeg", gtin: "02987653001234"}\n',
        encoding="utf-8",
    )
    vmap = load_video_map(path)
    assert vmap.resolve("02987653001234", "nl") == "PipeSticks_NL.mpeg"
    assert vmap.resolve("02987653001234", "fr") == "PipeSticks_FR.mpeg"


def test_a_missing_mapping_says_so_rather_than_raising_oserror(tmp_path: Path) -> None:
    """Every caller used to catch ``OSError`` for this. Now the loader owns the message."""
    with pytest.raises(VideoMapError) as caught:
        load_video_map(tmp_path / "nope.yml")
    assert "cannot read" in str(caught.value)
    assert "nope.yml" in str(caught.value)


def test_a_hand_edited_mapping_reports_where_the_syntax_error_is(tmp_path: Path) -> None:
    """The one failure that cannot happen unless a human edited the file used to be a traceback.

    ``yaml.YAMLError`` inherits from ``Exception`` alone, so it escaped every ``except (OSError,
    ValueError)`` in the codebase — and this file is one the design requires a human to edit and
    a client to sign off. A tab pasted by a text editor was enough to produce 25 lines of Python.
    """
    path = tmp_path / "mapping.yml"
    path.write_text(
        'nl:\n  - {file: "PipeSticks_NL.mpeg", gtin: ""}\n\tstray tab\n', encoding="utf-8"
    )

    with pytest.raises(VideoMapError) as caught:
        load_video_map(path)

    message = str(caught.value)
    assert "line 3" in message, f"the operator has to be told where to look: {message}"
    assert "column 1" in message
    assert "\n" not in message, "one line, so it survives a table cell or a log record"


def test_a_mapping_of_the_wrong_shape_says_what_was_expected(tmp_path: Path) -> None:
    path = tmp_path / "mapping.yml"
    path.write_text("nl: 42\n", encoding="utf-8")

    with pytest.raises(VideoMapError) as caught:
        load_video_map(path)

    message = str(caught.value)
    assert "not a video mapping" in message
    assert "by_language.nl" in message
    assert "\n" not in message


# --- summarize_video_map -----------------------------------------------------


def test_the_summary_counts_each_kind_of_gap_separately() -> None:
    vmap = _map(
        {
            "nl": [
                {"file": "unset.mpg", "gtin": ""},
                {"file": "gone.mpg", "gtin": "2987653000001"},
                {"file": "here.mpg", "gtin": "2987653000002"},
            ]
        }
    )

    summary = summarize_video_map(vmap, {"nl": ["here.mpg", "extra.mpg"]}, ["nl"])

    assert summary.files == 2
    assert summary.entries == 3
    assert summary.unconfirmed == 1  # unset.mpg
    assert summary.file_missing == 2  # unset.mpg and gone.mpg name files not on disk
    assert summary.missing_from_map == 1  # extra.mpg
    assert summary.ambiguous == 0
    assert summary.confirmed_gtins == 2
    assert summary.gaps == 4


def test_the_summary_distinguishes_an_empty_folder_from_an_unconfirmed_mapping() -> None:
    """The day-one operator machine: the mapping was handed over, the video library was not.

    Counting that as unconfirmed mappings sends someone to edit a file that is already correct —
    and produced the "284 of 0" line, whose numerator was a different quantity from its
    denominator.
    """
    vmap = _map({"nl": [{"file": "a.mpg", "gtin": "2987653000001"}]})

    absent = summarize_video_map(vmap, {"nl": []}, ["nl"])
    present = summarize_video_map(vmap, {"nl": ["a.mpg"]}, ["nl"])

    assert absent.no_files_found is True
    assert present.no_files_found is False
    assert present.gaps == 0


def test_an_empty_mapping_and_empty_folders_is_not_the_missing_files_case() -> None:
    """Nothing configured yet is not the same as a library that has not been copied."""
    assert summarize_video_map(_map({}), {}, ["nl"]).no_files_found is False


# --- check_video_map ---------------------------------------------------------


def test_check_reports_unconfirmed_entry() -> None:
    vmap = _map({"nl": [{"file": "A.mpg", "gtin": ""}]})
    issues = check_video_map(vmap, {"nl": ["A.mpg"]})
    kinds = {i.issue for i in issues}
    assert "video_unconfirmed" in kinds


def test_check_reports_file_on_disk_missing_from_map() -> None:
    vmap = _map({"nl": [{"file": "A.mpg", "gtin": "02987653001234"}]})
    issues = check_video_map(vmap, {"nl": ["A.mpg", "B.mpg"]})
    assert any(i.issue == "video_missing_from_map" and "B.mpg" in i.value for i in issues)


def test_check_reports_map_entry_without_file() -> None:
    vmap = _map({"nl": [{"file": "GONE.mpg", "gtin": "02987653001234"}]})
    issues = check_video_map(vmap, {"nl": []})
    assert any(i.issue == "video_file_missing" for i in issues)


def test_check_reports_ambiguous_gtin() -> None:
    vmap = _map(
        {
            "nl": [
                {"file": "A.mpg", "gtin": "02987653001234"},
                {"file": "B.mpg", "gtin": "02987653001234"},
            ]
        }
    )
    issues = check_video_map(vmap, {"nl": ["A.mpg", "B.mpg"]})
    assert any(i.issue == "video_ambiguous" for i in issues)


def test_check_skip_sentinel_is_not_a_gap() -> None:
    vmap = _map({"nl": [{"file": "A.mpg", "gtin": "skip"}]})
    issues = check_video_map(vmap, {"nl": ["A.mpg"]})
    assert issues == []


def test_check_all_confirmed_and_covered_is_clean() -> None:
    vmap = _map({"nl": [{"file": "A.mpg", "gtin": "02987653001234"}]})
    assert check_video_map(vmap, {"nl": ["A.mpg"]}) == []


# --- prepare_video -----------------------------------------------------------


def test_prepare_video_noop_returns_src(tmp_path: Path) -> None:
    src = tmp_path / "clip.mpeg"
    src.write_bytes(b"x")
    assert prepare_video(src, tmp_path / "out", transcode=False) == src


def test_prepare_video_transcode_invokes_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "PipeSticks_NL.mpeg"
    src.write_bytes(b"x")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"mp4")  # ffmpeg writes the output file (last arg)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = prepare_video(src, tmp_path / "out", transcode=True)
    assert out == tmp_path / "out" / "PipeSticks_NL.mp4"
    assert out is not None and out.exists()
    assert calls and "-c:v" in calls[0] and "libx264" in calls[0]
    assert "-map_metadata" in calls[0]


def test_prepare_video_transcode_short_circuits_when_dest_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "clip.mpeg"
    src.write_bytes(b"x")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "clip.mp4").write_bytes(b"already")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("ffmpeg must not run when dest exists")

    monkeypatch.setattr(subprocess, "run", boom)
    assert prepare_video(src, out_dir, transcode=True) == out_dir / "clip.mp4"


def test_prepare_video_ffmpeg_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "clip.mpeg"
    src.write_bytes(b"x")

    def fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fail)
    assert prepare_video(src, tmp_path / "out", transcode=True) is None


def test_a_failed_transcode_says_why_and_leaves_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written mp4 left behind would be reused as finished by the next run."""
    src = tmp_path / "clip.mpeg"
    src.write_bytes(b"x")

    def fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(cmd[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(
            1,
            cmd,
            stderr=b"ffmpeg version 7\nclip.mpeg: Invalid data found when processing input\n",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    assert prepare_video(src, tmp_path / "out", transcode=True) is None
    assert not (tmp_path / "out" / "clip.mp4").exists()
    assert "Invalid data found when processing input" in caplog.text


def test_no_ffmpeg_is_named_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    src = tmp_path / "clip.mpeg"
    src.write_bytes(b"x")

    def missing(cmd: list[str], **kwargs: object) -> None:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", missing)

    assert prepare_video(src, tmp_path / "out", transcode=True) is None
    assert "ffmpeg not found" in caplog.text


def test_an_ambiguous_mapping_is_found_across_barcode_widths() -> None:
    """``resolve`` canonicalises, so the duplicate check has to as well.

    Counting the raw cells, a GTIN written 13-digit on one row and 14-digit on another is two
    different GTINs and the mapping looks clean — while ``resolve`` finds two files and returns
    ``None``, so the product gets no video and nothing reports why. Found on the pilot the day an
    import wrote 14-digit values beside hand-typed 13-digit ones.
    """
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="Mega Snare.mp4", gtin="2987653007922"),
                VideoMapEntry(file="Mega Snare.mpg", gtin="02987653007922"),
            ]
        }
    )

    issues = check_video_map(vmap, {"nl": ["Mega Snare.mp4", "Mega Snare.mpg"]})

    assert [issue.issue for issue in issues] == ["video_ambiguous"]
    assert issues[0].gtin == "02987653007922"
    assert vmap.resolve("2987653007922", "nl") is None, "which is why it must be reported"


def test_one_video_listed_twice_under_one_barcode_is_one_video_not_a_clash() -> None:
    """A shared video is one row per barcode, so rows are not videos. Two spellings of one barcode
    on one file used to count as two files: held as a clash, and ``resolve`` attached nothing."""
    vmap = VideoMap(
        by_language={
            "nl": [
                VideoMapEntry(file="A.mp4", gtin="2987653007434"),
                VideoMapEntry(file="A.mp4", gtin="02987653007434"),
            ]
        }
    )

    assert vmap.resolve("02987653007434", "nl") == "A.mp4"
    assert clashing_gtins(vmap, ["nl"]) == frozenset()
    assert fully_mapped_gtins(vmap, ["nl"]) == frozenset({"02987653007434"})
    assert check_video_map(vmap, {"nl": ["A.mp4"]}) == []
