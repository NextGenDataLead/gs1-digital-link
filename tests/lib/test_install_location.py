"""Where an installed copy keeps its data — decided once, remembered, and never guessed.

The failure that matters is a second ledger: a new release that quietly starts an empty data
folder while the real ``state.json`` sits in the old one. Every refusal below exists to stop that.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from lib.install_location import (
    NEW_FOLDER_NAME,
    How,
    Placement,
    PlacementError,
    holds_data,
    place,
    pointer_file,
    recorded,
    settle,
)
from scripts import data_folder


@pytest.fixture
def code(tmp_path: Path) -> Path:
    """A freshly unzipped release: code and examples, no data."""
    folder = tmp_path / "gs1-digital-link-0.2.0"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (folder / "clients.example.yml").write_text("clients: example\n", encoding="utf-8")
    (folder / ".env.example").write_text("EXAMPLE=\n", encoding="utf-8")
    return folder


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.fixture
def pointer(tmp_path: Path) -> Path:
    return tmp_path / "settings" / "data-folder.txt"


def _with_ledger(folder: Path) -> Path:
    (folder / "output" / "democlient").mkdir(parents=True)
    (folder / "output" / "democlient" / "state.json").write_text("{}", encoding="utf-8")
    return folder


# --- where the pointer lives -----------------------------------------------------------------


def test_the_pointer_lives_where_each_os_keeps_app_settings(tmp_path: Path) -> None:
    home = tmp_path
    mac = pointer_file("darwin", home, {})
    windows = pointer_file("win32", home, {"APPDATA": str(tmp_path / "Roaming")})
    linux = pointer_file("linux", home, {})
    assert mac == home / "Library" / "Application Support" / "GS1 Digital Link" / "data-folder.txt"
    assert windows == tmp_path / "Roaming" / "GS1 Digital Link" / "data-folder.txt"
    assert linux == home / ".config" / "gs1-digital-link" / "data-folder.txt"


# --- what counts as data -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["clients.yml", ".env"])
def test_settings_or_secrets_mean_a_folder_is_in_use(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("x", encoding="utf-8")
    assert holds_data(tmp_path)


def test_a_ledger_alone_means_a_folder_is_in_use(tmp_path: Path) -> None:
    assert holds_data(_with_ledger(tmp_path))


def test_examples_are_not_data(code: Path) -> None:
    assert not holds_data(code)


# --- the decision ------------------------------------------------------------------------------


def test_a_first_install_gets_a_new_folder_in_home(code: Path, pointer: Path, home: Path) -> None:
    assert place(code, pointer, home) == Placement(home / NEW_FOLDER_NAME, "new")


def test_an_existing_installation_keeps_its_data_where_it_is(
    code: Path, pointer: Path, home: Path
) -> None:
    """The original machine: data in the code folder, from before data folders existed."""
    _with_ledger(code)
    assert place(code, pointer, home) == Placement(code, "here")


def test_a_later_release_finds_the_remembered_folder(
    code: Path, pointer: Path, home: Path, tmp_path: Path
) -> None:
    earlier = _with_ledger(tmp_path / "gs1-digital-link-0.1.0")
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{earlier}\n", encoding="utf-8")
    assert place(code, pointer, home) == Placement(earlier, "remembered")


def test_a_lost_pointer_still_finds_the_home_folder(code: Path, pointer: Path, home: Path) -> None:
    (home / NEW_FOLDER_NAME).mkdir()
    (home / NEW_FOLDER_NAME / "clients.yml").write_text("theirs", encoding="utf-8")
    assert place(code, pointer, home) == Placement(home / NEW_FOLDER_NAME, "found")


def test_a_remembered_folder_that_is_gone_is_refused(
    code: Path, pointer: Path, home: Path, tmp_path: Path
) -> None:
    """An empty replacement would publish as if nothing had been published before."""
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{tmp_path / 'moved-away'}\n", encoding="utf-8")
    with pytest.raises(PlacementError, match="not there"):
        place(code, pointer, home)


def test_two_folders_with_data_are_refused(
    code: Path, pointer: Path, home: Path, tmp_path: Path
) -> None:
    remembered = _with_ledger(tmp_path / "remembered")
    _with_ledger(code)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{remembered}\n", encoding="utf-8")
    with pytest.raises(PlacementError, match="Two folders"):
        place(code, pointer, home)


# --- making it so ------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits to check")
def test_a_new_folder_is_created_seeded_and_recorded(code: Path, pointer: Path, home: Path) -> None:
    placement = place(code, pointer, home)
    settle(placement, pointer, code)

    folder = home / NEW_FOLDER_NAME
    assert (folder / "clients.yml").read_text(encoding="utf-8") == "clients: example\n"
    assert (folder / ".env").read_text(encoding="utf-8") == "EXAMPLE=\n"
    assert stat.S_IMODE((folder / ".env").stat().st_mode) == 0o600
    assert (folder / "input").is_dir() and (folder / "output").is_dir()
    assert recorded(pointer) == folder


def test_an_existing_installation_is_recorded_and_not_touched(
    code: Path, pointer: Path, home: Path
) -> None:
    _with_ledger(code)
    before = sorted(p.relative_to(code) for p in code.rglob("*"))

    settle(place(code, pointer, home), pointer, code)

    assert sorted(p.relative_to(code) for p in code.rglob("*")) == before, "nothing seeded"
    assert recorded(pointer) == code


@pytest.mark.parametrize("how", ["remembered", "found", "here"])
def test_a_folder_that_holds_data_is_never_written_to(
    code: Path, pointer: Path, home: Path, how: How
) -> None:
    """Not even a missing ``.env`` filled in: found in the 2026-10-10 rehearsal, where an
    earlier version's folder without one was handed the example's."""
    folder = home / "in-use"
    folder.mkdir()
    (folder / "clients.yml").write_text("theirs\n", encoding="utf-8")

    settle(Placement(folder, how), pointer, code)

    assert sorted(p.name for p in folder.iterdir()) == ["clients.yml"]
    assert (folder / "clients.yml").read_text(encoding="utf-8") == "theirs\n"
    assert recorded(pointer) == folder


def test_settling_twice_changes_nothing_the_second_time(
    code: Path, pointer: Path, home: Path
) -> None:
    settle(place(code, pointer, home), pointer, code)
    assert settle(place(code, pointer, home), pointer, code) == []


# --- the command the double-click scripts run --------------------------------------------------


def test_install_prints_the_folder_last_for_the_start_script(
    code: Path, pointer: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert data_folder.main(["--install"], code_root=code, pointer=pointer, home=home) == 0
    out, err = capsys.readouterr()
    assert out.strip() == str(home / NEW_FOLDER_NAME)
    assert "Back it up" in err


def test_install_copies_a_clients_prompt_and_templates_into_the_data_folder(
    code: Path, pointer: Path, home: Path
) -> None:
    """The one write into a folder already holding data — and only what it has none of."""
    folder = home / "in-use"
    (folder / "output" / "acme").mkdir(parents=True)
    (folder / "clients.yml").write_text("clients:\n  acme: {}\n", encoding="utf-8")
    (folder / "output" / "acme" / "state.json").write_text("{}", encoding="utf-8")
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{folder}\n", encoding="utf-8")
    (code / "prompts" / "acme").mkdir(parents=True)
    (code / "prompts" / "acme" / "generation.v1.md").write_text("voice", encoding="utf-8")

    assert data_folder.main(["--install"], code_root=code, pointer=pointer, home=home) == 0

    copied = folder / "input" / "acme" / "prompts" / "generation.v1.md"
    assert copied.read_text(encoding="utf-8") == "voice"
    assert not (folder / "input" / "acme" / "templates").exists()


def test_install_warns_to_keep_an_earlier_versions_folder(
    code: Path, pointer: Path, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    earlier = _with_ledger(tmp_path / "gs1-digital-link-0.1.0")
    (earlier / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{earlier}\n", encoding="utf-8")

    assert data_folder.main(["--install"], code_root=code, pointer=pointer, home=home) == 0
    assert "KEEP IT" in capsys.readouterr().err


def test_install_refuses_and_changes_nothing(
    code: Path, pointer: Path, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{tmp_path / 'gone'}\n", encoding="utf-8")

    assert data_folder.main(["--install"], code_root=code, pointer=pointer, home=home) == 1
    out, _ = capsys.readouterr()
    assert out == "", "nothing for a start script to capture"
    assert not (home / NEW_FOLDER_NAME).exists()


def test_start_reads_the_recorded_folder(
    code: Path,
    pointer: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GS1_DATA_DIR", raising=False)
    data_folder.main(["--install"], code_root=code, pointer=pointer, home=home)
    capsys.readouterr()

    assert data_folder.main([], code_root=code, pointer=pointer, home=home) == 0
    assert capsys.readouterr().out.strip() == str(home / NEW_FOLDER_NAME)


def test_start_honours_a_folder_it_already_pins(
    pointer: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GS1_DATA_DIR", str(tmp_path))
    assert data_folder.main([], pointer=pointer) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path)


def test_start_before_install_asks_for_the_installer(
    code: Path,
    pointer: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GS1_DATA_DIR", raising=False)
    assert data_folder.main([], code_root=code, pointer=pointer, home=home) == 1
    assert not (home / NEW_FOLDER_NAME).exists(), "start never creates the folder"
    out, err = capsys.readouterr()
    assert out == ""
    assert "run the installer" in err


def test_start_without_a_record_uses_data_already_here(
    code: Path,
    pointer: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An installation that predates the installer step — or a development clone — still starts."""
    monkeypatch.delenv("GS1_DATA_DIR", raising=False)
    _with_ledger(code)
    assert data_folder.main([], code_root=code, pointer=pointer, home=home) == 0
    assert capsys.readouterr().out.strip() == str(code)
    assert recorded(pointer) is None, "start writes nothing"


def test_start_refuses_two_folders_with_data_too(
    code: Path,
    pointer: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GS1_DATA_DIR", raising=False)
    remembered = _with_ledger(tmp_path / "remembered")
    _with_ledger(code)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{remembered}\n", encoding="utf-8")
    assert data_folder.main([], code_root=code, pointer=pointer, home=tmp_path) == 1
    assert capsys.readouterr().out == ""
