"""The client's settings kept in git beside the live copy (:mod:`lib.committed_config`).

The live ``clients.yml`` is always the one used; these tests pin that comparing never changes
it, that taking keeps the old one, and that a line-ending difference is not a difference.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.committed_config import committed_path, matches, save, take
from lib.preflight import Status, check_committed_config
from scripts import config_sync

_EXAMPLE = Path(__file__).resolve().parents[2] / "clients.example.yml"
CLIENT = "democlient"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    """A data folder holding the example config, and an empty program folder."""
    data, code = tmp_path / "data", tmp_path / "code"
    data.mkdir()
    code.mkdir()
    shutil.copy(_EXAMPLE, data / "clients.yml")
    return data, code


def test_the_committed_copy_is_named_for_the_client(tmp_path: Path) -> None:
    assert committed_path("otherclient", tmp_path) == tmp_path / "config" / "otherclient.yml"


def test_a_crlf_checkout_still_matches(tmp_path: Path) -> None:
    live, committed = tmp_path / "live.yml", tmp_path / "committed.yml"
    live.write_text("a: 1\nb: 2\n", encoding="utf-8")
    committed.write_bytes(b"a: 1\r\nb: 2\r\n")

    assert matches(live, committed)


def test_take_keeps_the_live_copy_before_replacing_it(tmp_path: Path) -> None:
    live, committed = tmp_path / "clients.yml", tmp_path / "committed.yml"
    live.write_text("mine\n", encoding="utf-8")
    committed.write_text("agreed\n", encoding="utf-8")

    kept = take(live, committed, tmp_path / "superseded", NOW)

    assert live.read_text(encoding="utf-8") == "agreed\n"
    assert kept is not None and kept.read_text(encoding="utf-8") == "mine\n"


# --- the preflight check -------------------------------------------------------------------------


def test_no_committed_copy_proves_nothing(folders: tuple[Path, Path]) -> None:
    data, code = folders

    assert check_committed_config(CLIENT, data / "clients.yml", code).status is Status.NA


def test_a_matching_copy_is_ok_and_a_different_one_warns_without_failing(
    folders: tuple[Path, Path],
) -> None:
    data, code = folders
    save(data / "clients.yml", committed_path(CLIENT, code))
    assert check_committed_config(CLIENT, data / "clients.yml", code).status is Status.OK

    with (data / "clients.yml").open("a", encoding="utf-8") as handle:
        handle.write("# edited on the Setup screen\n")
    result = check_committed_config(CLIENT, data / "clients.yml", code)

    assert result.status is Status.WARN
    assert "config_sync save" in (result.remedy or "")


# --- the script ----------------------------------------------------------------------------------


def test_save_then_compare_then_take_round_trips(folders: tuple[Path, Path]) -> None:
    data, code = folders

    assert config_sync.main(["check"], root=data, code_root=code) == 1, "nothing committed yet"
    assert config_sync.main(["save"], root=data, code_root=code) == 0
    assert config_sync.main(["check"], root=data, code_root=code) == 0

    original = (data / "clients.yml").read_text(encoding="utf-8")
    (data / "clients.yml").write_text(original + "# local edit\n", encoding="utf-8")
    assert config_sync.main(["check"], root=data, code_root=code) == 1

    assert config_sync.main(["take"], root=data, code_root=code) == 0
    assert (data / "clients.yml").read_text(encoding="utf-8") == original
    kept = list((data / "input" / CLIENT / "superseded").glob("clients.yml.before-take-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8").endswith("# local edit\n")


def test_a_committed_copy_that_does_not_load_is_never_taken(folders: tuple[Path, Path]) -> None:
    data, code = folders
    broken = committed_path(CLIENT, code)
    broken.parent.mkdir(parents=True)
    broken.write_text("clients: [not, a, mapping]\n", encoding="utf-8")
    before = (data / "clients.yml").read_bytes()

    assert config_sync.main(["take"], root=data, code_root=code) == 1
    assert (data / "clients.yml").read_bytes() == before
