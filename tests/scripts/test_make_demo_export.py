"""Tests for scripts/make_demo_export.py.

The behaviour worth pinning is the refusal. This command's whole risk is writing demo products
over a client's real export, and an existing file at the configured path is the only signal of
that it can have.

Every test runs with the cwd moved into ``tmp_path``: the configured paths are relative, so that
is what keeps the writes out of the working tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib import demo_export
from lib.config import load_clients
from lib.gdsn import build_records, read_workbook
from scripts import make_demo_export

_EXAMPLE_CONFIG = str(Path(__file__).resolve().parents[2] / "clients.example.yml")

#: Read from the example config rather than restated here. They were spelled out, and a layout
#: change moved the files while every assertion still pointed at where they used to be — six
#: failures that said "the demo export writes nothing" when it was writing perfectly well.
_DEMO = load_clients(_EXAMPLE_CONFIG)["democlient"]
_EXPORT = Path(_DEMO.export.path)
_SCOPE = Path(_DEMO.process_list.path) if _DEMO.process_list else Path()


@pytest.fixture(autouse=True)
def _in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _run(*argv: str) -> int:
    return make_demo_export.main(["--config", _EXAMPLE_CONFIG, *argv])


def test_it_writes_both_files_where_the_config_says() -> None:
    assert _run() == 0
    assert _EXPORT.is_file()
    assert _SCOPE.is_file()


def test_what_it_writes_parses_against_the_config_that_placed_it() -> None:
    """End to end in one assertion: the file this command writes is one parse_export can read."""
    assert _run() == 0
    client = load_clients(_EXAMPLE_CONFIG)["democlient"]
    export = client.export

    result = build_records(
        read_workbook(str(_EXPORT)),
        export.gdsn_map,
        export.market_priority,
        list(client.wordpress.languages),
        client.wordpress.default_language,
        export.gdsn_extras,
    )

    assert result.errors == []
    assert len(result.records) == len(demo_export.CATALOGUE)


def test_an_existing_file_refuses_the_whole_run(capsys: pytest.CaptureFixture[str]) -> None:
    _EXPORT.parent.mkdir(parents=True, exist_ok=True)
    _EXPORT.write_text("a real export, as far as this command can tell", encoding="utf-8")

    assert _run() == 1
    # Not a byte written, including the file that had no clash of its own: a partial write here
    # leaves a scope list describing an export that was never replaced.
    assert _EXPORT.read_text(encoding="utf-8").startswith("a real export")
    assert not _SCOPE.exists()
    assert "refusing" in capsys.readouterr().err


def test_the_refusal_names_every_clash_not_just_the_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Learning about the second file only after re-running with --force is how one gets lost."""
    _EXPORT.parent.mkdir(parents=True, exist_ok=True)
    _EXPORT.write_text("export", encoding="utf-8")
    # The two now live in sibling folders, not one directory — see clients.example.yml.
    _SCOPE.parent.mkdir(parents=True, exist_ok=True)
    _SCOPE.write_text("scope", encoding="utf-8")

    assert _run() == 1
    err = capsys.readouterr().err
    assert str(_EXPORT) in err
    assert str(_SCOPE) in err


def test_force_overwrites() -> None:
    _EXPORT.parent.mkdir(parents=True, exist_ok=True)
    _EXPORT.write_text("stale", encoding="utf-8")

    assert _run("--force") == 0
    assert _EXPORT.read_bytes()[:2] == b"PK"  # an xlsx, not the text that was there


def test_a_dry_run_writes_nothing_and_still_reports_what_would_refuse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _EXPORT.parent.mkdir(parents=True, exist_ok=True)
    _EXPORT.write_text("stale", encoding="utf-8")

    assert _run("--dry-run") == 0
    assert _EXPORT.read_text(encoding="utf-8") == "stale"
    assert not _SCOPE.exists()
    assert "a real run would refuse" in capsys.readouterr().err


def test_an_unknown_client_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("nosuchclient") == 2
    assert "config error" in capsys.readouterr().err
    assert not _EXPORT.exists()


_MEDIA = _DEMO.media


def test_it_writes_the_videos_the_mapping_and_the_sign_off_sheet() -> None:
    """What makes the Data screen's video half rehearsable on democlient at all."""
    assert _MEDIA is not None and _MEDIA.video_map_path
    assert _run() == 0

    assert Path(_MEDIA.video_map_path).read_text(encoding="utf-8").startswith("nl:\n- {file: ")
    assert (_EXPORT.parents[3] / "test-uploads" / "video-signoff.xlsx").is_file()
    for language, folder in _MEDIA.video_folders.items():
        assert sorted(p.name for p in Path(folder).iterdir()), language


def test_an_existing_mapping_refuses_the_whole_run(capsys: pytest.CaptureFixture[str]) -> None:
    """A mapping is client sign-off. Writing a demo one over a real one is the damage to refuse."""
    assert _MEDIA is not None and _MEDIA.video_map_path
    mapping = Path(_MEDIA.video_map_path)
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("nl:\n- {file: real.mp4, gtin: '02987653000011'}\n", encoding="utf-8")

    assert _run() == 1
    assert "real.mp4" in mapping.read_text(encoding="utf-8")
    assert not _EXPORT.exists()
    assert str(mapping) in capsys.readouterr().err
