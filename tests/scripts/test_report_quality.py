"""Tests for scripts/report_quality.py — the CLI that loads the issue files and writes the report.

Drives ``main`` in a temp working directory: writes sample ``output/{client}/data/*_issues.json``
(and products.json), then asserts the consolidated markdown report is written and the exit code.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest
import yaml

from lib import video_signoff_archive
from lib.config import get_client
from lib.quality_report_video import SignoffReview
from lib.records import LocalisedText, ProductRecord
from scripts import report_quality


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_clients_yml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the script at a config this test owns, instead of the operator's.

    `lib.config.DEFAULT_CLIENTS_PATH` is `<repo root>/clients.yml` — absolute, so `chdir(tmp_path)`
    does not move it. That file is **gitignored**, so these tests were reading the real client
    config on a developer machine and getting a `ConfigError` in CI, where it does not exist. The
    difference was invisible until §1 started rendering from the E23 gaps, which need config: the
    same test then passed locally and failed on CI for a reason that had nothing to do with the
    change under test.
    """
    config = {
        "version": 1,
        "clients": {
            "democlient": {
                "display_name": "Democlient",
                "gs1": {
                    "account_number_test": "8712345000010",
                    "client_id_env_test": "TEST_GS1_ID",
                    "client_secret_env_test": "TEST_GS1_SECRET",
                },
                "export": {
                    "format": "gdsn",
                    "path": "./input/democlient/products.xlsx",
                    "gdsn_map": {
                        "product_name": {
                            "sheet": "TradeItemDescription",
                            "attribute": "3301",
                            "localised": True,
                            "required": True,
                        },
                        "description_short": {
                            "sheet": "TradeItemDescription",
                            "attribute": "1083",
                            "localised": True,
                            "required_group": "marketing_copy",
                        },
                    },
                },
                "wordpress": {
                    "site_url": "https://example.test",
                    "username": "bot",
                    "app_password_env": "TEST_WP_PASS",
                    "languages": ["nl"],
                },
                "process_list": {
                    "path": str(tmp_path / "input" / "democlient" / "process-list.xlsx"),
                    "gtin_column": "Barcode",
                },
            }
        },
    }
    path = tmp_path / "clients.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(
        report_quality, "get_client", lambda client_id: get_client(client_id, path=path)
    )


def _write_process_list(tmp_path: Path, gtins: list[str]) -> None:
    """A process list at the path `clients.yml` names, so `in_scope` really narrows."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.cell(row=1, column=1, value="Barcode")
    for row, gtin in enumerate(gtins, start=2):
        sheet.cell(row=row, column=1, value=gtin)
    path = tmp_path / "input" / "democlient" / "process-list.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _seed(tmp_path: Path) -> Path:
    data = tmp_path / "output" / "democlient" / "data"
    _write(
        data / "generated_issues.json",
        [
            {
                "gtin": "02987653003276",
                "field": "description_short.nl",
                "source": "attr 1083",
                "issue": "missing_generation_input",
                "value": "",
                "detail": "d",
            },
            {
                "gtin": "02987653007915",
                "field": "generated_description.nl",
                "source": "inferred",
                "issue": "generation_inference",
                "value": "magnet claim",
                "detail": "d",
            },
        ],
    )
    _write(data / "source_issues.json", [])
    _write(data / "video_map_issues.json", [])
    _write(data / "category_issues.json", [])
    # Both GTINs the seeded findings name: a finding about a product the file does not carry is
    # not in scope, because scope is decided over the parsed products.
    _write(
        data / "products.json",
        [
            ProductRecord(
                gtin=gtin,
                brand="Democlient",
                product_name=LocalisedText(values={"nl": name}),
            ).model_dump(mode="json")
            for gtin, name in (("02987653007915", "LED-lamp"), ("02987653003276", "plasma"))
        ],
    )
    return data


def test_writes_report_and_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    _write_process_list(tmp_path, ["02987653007915", "02987653003276"])
    _write_clients_yml(tmp_path, monkeypatch)

    code = report_quality.main(["democlient"])

    assert code == 0
    report = tmp_path / "output" / "democlient" / "data-quality-report.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Data quality report" in text
    assert "02987653003276" in text  # held GTIN
    assert "magnet claim" in text  # inference surfaced


def test_findings_for_out_of_scope_gtins_never_reach_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report describes one run, so every section describes the same set of GTINs.

    §3 was the exception: its rows came straight from `source_issues.json`, which the parser
    writes over the whole workbook, while §0's matrix and §1's rows were computed over the
    process list. Four of eleven GTINs in §3 were outside the run — findable nowhere else in the
    document, which is how it was noticed. `02987653000794` is in the export and not on the
    process list.
    """
    monkeypatch.chdir(tmp_path)
    data = _seed(tmp_path)
    in_scope_gtin, out_of_scope_gtin = "02987653007915", "02987653000794"
    # The real process list lives at a path relative to the repo root, so under `chdir(tmp_path)`
    # it is absent and `in_scope` narrows nothing — a scope test would pass on no scope at all.
    _write_process_list(tmp_path, [in_scope_gtin, "02987653003276"])
    _write_clients_yml(tmp_path, monkeypatch)
    _write(
        data / "source_issues.json",
        [
            {
                "gtin": gtin,
                "field": "net_content",
                "source": "TradeItemMeasurements attr 3510",
                "issue": "value_inconsistent_across_markets",
                "value": "x",
                "detail": "d",
                "market_values": [["528", "x"], ["056", "y"]],
            }
            for gtin in (in_scope_gtin, out_of_scope_gtin)
        ],
    )
    # Every issue file, not only the one where the leak was noticed. `generated_issues` happens
    # to be scoped already — generation only runs for in-scope units — so a narrowing applied to
    # `source` alone passes today and leaks the moment that stops being true.
    _write(
        data / "generated_issues.json",
        [
            {
                "gtin": out_of_scope_gtin,
                "field": "generated_description.nl",
                "source": "inferred",
                "issue": "generation_inference",
                "value": "a claim about a product this run will not touch",
                "detail": "d",
            }
        ],
    )
    # A finding with no GTIN is about the *input*, not a product, so scope cannot apply to it.
    _write(
        data / "video_map_issues.json",
        [
            {
                "gtin": "",
                "field": "video",
                "source": "mapping.yml",
                "issue": "video_unconfirmed",
                "value": "Damp Mat v2.mp4",
                "detail": "d",
            }
        ],
    )
    products = json.loads((data / "products.json").read_text())
    products.append(
        ProductRecord(
            gtin=out_of_scope_gtin,
            brand="Democlient",
            product_name=LocalisedText(values={"nl": "Power jet"}),
        ).model_dump(mode="json")
    )
    _write(data / "products.json", products)

    assert report_quality.main(["democlient"]) == 0

    text = (tmp_path / "output" / "democlient" / "data-quality-report.md").read_text(
        encoding="utf-8"
    )
    assert in_scope_gtin in text
    assert out_of_scope_gtin not in text
    assert "a claim about a product this run will not touch" not in text
    assert "Damp Mat v2.mp4" in text  # kept: it names a video file, not a GTIN


def test_missing_data_dir_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no output/ at all

    assert report_quality.main(["democlient"]) == 2


def test_absent_issue_files_treated_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # data dir exists but only products.json present; issue files absent
    data = tmp_path / "output" / "democlient" / "data"
    data.mkdir(parents=True)
    (data / "products.json").write_text("[]", encoding="utf-8")

    assert report_quality.main(["democlient"]) == 0
    assert (tmp_path / "output" / "democlient" / "data-quality-report.md").is_file()


def test_generated_at_is_local_time_with_a_named_zone() -> None:
    """Local, so it matches the file browser beside the file; named, so it travels unambiguously."""
    stamp = report_quality._generated_at()

    # "2026-08-13 22:02 CEST" — date, time, then a zone abbreviation (Windows: "UTC+02:00").
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+", stamp), stamp
    assert stamp.startswith(datetime.now().astimezone().strftime("%Y-%m-%d"))


# --- the video join: the matrix and the hold read lib.video_status --------------------------------

_HAS_ONE = "02987653000011"
_HAS_TWO = "02987653000028"
_HAS_NONE = "02987653000035"


def _with_videos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, object] | str
) -> dict[str, ProductRecord]:
    """The test config plus a `media` block, a mapping, its folder, and three in-scope products."""
    _write_clients_yml(tmp_path, monkeypatch)
    config_path = tmp_path / "clients.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    videos = tmp_path / "videos"
    (videos / "NL").mkdir(parents=True)
    for name in ("one.mpg", "two-a.mpg", "two-b.mpg"):
        (videos / "NL" / name).write_bytes(b"x")
    map_path = videos / "mapping.yml"
    map_path.write_text(
        mapping if isinstance(mapping, str) else yaml.safe_dump(mapping), encoding="utf-8"
    )
    config["clients"]["democlient"]["media"] = {
        "video_folders": {"nl": str(videos / "NL")},
        "video_map_path": str(map_path),
        "restrict_to_mapped_gtins": True,
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    gtins = [_HAS_ONE, _HAS_TWO, _HAS_NONE]
    _write_process_list(tmp_path, gtins)
    # Complete copy, so E23 holds none of them and only the video rule is under test.
    records = [
        ProductRecord(
            gtin=g,
            brand="Democlient",
            product_name=LocalisedText(values={"nl": "x"}),
            description_short=LocalisedText(values={"nl": "y"}),
        )
        for g in gtins
    ]
    return {p.gtin14: p for p in records}


_MAPPING = {
    "nl": [
        {"file": "one.mpg", "gtin": _HAS_ONE},
        {"file": "two-a.mpg", "gtin": _HAS_TWO},
        {"file": "two-b.mpg", "gtin": _HAS_TWO[1:]},  # the same barcode, 13-digit
    ]
}


def test_the_matrix_marks_a_video_only_where_the_page_gets_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files confirmed to one product is ○: ``resolve`` attaches neither.

    The matrix used to count any non-blank, non-`skip` cell, so it showed ● for a product whose
    page gets no video — and a whitespace-only cell became the all-zero GTIN.
    """
    products = _with_videos(tmp_path, monkeypatch, _MAPPING)

    matrix = report_quality._matrix_input("democlient", products)

    assert matrix is not None
    assert matrix.video_confirmed == {"nl": {_HAS_ONE}}


def test_two_videos_in_one_language_is_a_hold_like_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GTIN confirmed to two files gets neither video, so the gate holds it and so does this."""
    products = _with_videos(tmp_path, monkeypatch, _MAPPING)

    _, held = report_quality._publish_blocks("democlient", products)

    assert held == [_HAS_TWO, _HAS_NONE]


def test_an_unreadable_mapping_marks_nothing_and_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The video-map section reports a broken file; the matrix and the hold do not fail over it."""
    products = _with_videos(tmp_path, monkeypatch, "nl: [unclosed")

    matrix = report_quality._matrix_input("democlient", products)
    _, held = report_quality._publish_blocks("democlient", products)

    assert matrix is not None
    assert matrix.video_confirmed == {"nl": set()}
    assert held == []


# --- the video backlog is recomputed from the mapping, not read from a file nobody refreshes ------


def _stale_backlog(data: Path, count: int) -> None:
    """`video_map_issues.json` as `build_video_map --check` left it, weeks ago."""
    _write(
        data / "video_map_issues.json",
        [
            {
                "gtin": "",
                "field": "video.nl",
                "source": "operator video folder",
                "issue": "video_unconfirmed",
                "value": f"stale-{n}.mpg",
                "detail": "no GTIN filled in yet",
            }
            for n in range(count)
        ],
    )


def _report(tmp_path: Path) -> str:
    assert report_quality.main(["democlient"]) == 0
    return (tmp_path / "output" / "democlient" / "data-quality-report.md").read_text(
        encoding="utf-8"
    )


def test_the_video_backlog_counts_the_mapping_as_it_is_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pilot's file said 118 unassigned while the mapping had 18; the report believed the file.

    Only `build_video_map --check` rewrites that file and the Data screen never runs it, so the
    backlog was the one part of the report that did not move when the mapping did.
    """
    monkeypatch.chdir(tmp_path)
    _stale_backlog(_seed(tmp_path), 118)
    _with_videos(
        tmp_path,
        monkeypatch,
        {
            "nl": [
                {"file": "one.mpg", "gtin": ""},
                {"file": "two-a.mpg", "gtin": "   "},
                {"file": "two-b.mpg", "gtin": _HAS_TWO},
            ]
        },
    )

    text = _report(tmp_path)

    assert "**2**" in text
    assert "one.mpg" in text and "two-a.mpg" in text
    assert "stale-" not in text


def test_a_client_with_no_mapping_still_reads_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `media` block means nothing to recompute from, so the file is the only record there is."""
    monkeypatch.chdir(tmp_path)
    _stale_backlog(_seed(tmp_path), 3)
    _write_clients_yml(tmp_path, monkeypatch)

    text = _report(tmp_path)

    assert "**3**" in text
    assert "stale-0.mpg" in text


# --- §1d: the archived sign-off sheet, re-planned on every render --------------------------------

_SIGNOFF_MAPPING = {
    "nl": [
        {"file": "one.mpg", "gtin": ""},
        {"file": "two-a.mpg", "gtin": ""},
        {"file": "two-b.mpg", "gtin": _HAS_TWO},
    ]
}


def _sheet(path: Path, header: list[str], rows: list[list[str]]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path.read_bytes()


def _signed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: list[str] | None = None
) -> tuple[dict[str, ProductRecord], Path]:
    """A client with a mapping, and the sheet the client sent back archived with its columns."""
    products = _with_videos(tmp_path, monkeypatch, _SIGNOFF_MAPPING)
    video_map = tmp_path / "videos" / "mapping.yml"
    chosen = ["language", "file", "current_gtin"]
    data = _sheet(
        tmp_path / "upload.xlsx",
        header or chosen,
        [["nl", "one.mpg", _HAS_ONE[1:]], ["nl", "two-a.mpg", "8.7132E+12"]],
    )
    sheet = video_signoff_archive.archive(video_map, data, stamp="20261006-101500")
    video_signoff_archive.record(
        video_map,
        sheet=sheet,
        given_name="Videos Democlient v3.xlsx",
        at="2026-10-06T10:15:00",
        rows=2,
        columns={"language": 0, "file": 1, "gtin": 2},
        headers=dict(zip(("language", "file", "gtin"), chosen, strict=True)),
    )
    return products, video_map


def _review(products: dict[str, ProductRecord]) -> SignoffReview | str:
    return report_quality._signoff_review(report_quality.get_client("democlient"), products)


def _plan_of(products: dict[str, ProductRecord]) -> SignoffReview:
    review = _review(products)
    assert isinstance(review, SignoffReview), review
    return review


def test_with_no_sheet_the_review_says_so_in_one_ordinary_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    products = _with_videos(tmp_path, monkeypatch, _SIGNOFF_MAPPING)

    assert _review(products) == "No sign-off sheet from the client yet — nothing to re-plan."


def test_an_archived_sheet_is_replanned_against_the_mapping_as_it_is_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    products, _ = _signed_off(tmp_path, monkeypatch)

    review = _plan_of(products)

    assert review.given_name == "Videos Democlient v3.xlsx"
    assert review.at == "2026-10-06"
    plan = review.plan
    assert [(row.line, row.outcome) for row in plan.rows] == [(2, "change"), (3, "rejected")]
    assert plan.of("rejected")[0].given == "8.7132E+12"  # quoted as the sheet carried it


def test_a_fill_applied_since_reads_as_already_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-planned, not remembered: the counts correct themselves once the mapping moves."""
    products, video_map = _signed_off(tmp_path, monkeypatch)
    filled = {
        "nl": [
            {"file": "one.mpg", "gtin": _HAS_ONE},
            {"file": "two-a.mpg", "gtin": ""},
            {"file": "two-b.mpg", "gtin": _HAS_TWO},
        ]
    }
    video_map.write_text(yaml.safe_dump(filled), encoding="utf-8")

    plan = _plan_of(products).plan

    assert [row.outcome for row in plan.rows] == ["unchanged", "rejected"]


def test_a_sheet_edited_since_its_columns_were_chosen_is_refused_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning against moved columns would read a filename as a barcode."""
    products, _ = _signed_off(tmp_path, monkeypatch, header=["language", "file", "barcode"])

    review = _review(products)

    assert isinstance(review, str)
    assert 'gtin column was headed "current_gtin" and is now "barcode"' in review


def test_a_newer_sheet_with_no_choice_is_not_answered_with_the_older_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    products, video_map = _signed_off(tmp_path, monkeypatch)
    older = next((video_map.parent / "signoff").glob("signoff-*.xlsx"))
    newer = video_signoff_archive.archive(video_map, b"later", stamp="20261007-090000")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    review = _review(products)

    assert isinstance(review, str)
    assert "has not had its columns chosen yet" in review and newer.name in review
