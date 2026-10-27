"""``scripts.handover``: pack on one machine, unpack on another, and the refusal between them.

Runs against ``clients.example.yml`` (``democlient``) copied into a scratch data folder — never
the operator's real config, which a test reading the default path would pick up.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lib.handover import state_file
from lib.records import State, StateEntry
from scripts import handover

_EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "clients.example.yml"
CLIENT = "democlient"
GTIN = "02912345000017"


def _ledger(root: Path, when: datetime) -> None:
    entry = StateEntry(
        wp_page_id=1,
        wp_url="https://example.test/p/",
        wp_featured_media_id=None,
        content_hash="c" * 64,
        gs1_link_set_hash="g" * 64,
        last_run=when,
    )
    path = root / state_file(CLIENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        State(client_id=CLIENT, entries={GTIN: {"nl": entry}}).model_dump_json(), encoding="utf-8"
    )


def _data_folder(root: Path, when: datetime) -> Path:
    root.mkdir(parents=True)
    shutil.copy(_EXAMPLE_CONFIG, root / "clients.yml")
    (root / ".env").write_text("DEMO=1\n", encoding="utf-8")
    _ledger(root, when)
    return root


T0 = datetime(2026, 10, 22, 5, 36, tzinfo=UTC)


def test_a_pack_made_here_unpacks_there(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mac = _data_folder(tmp_path / "mac", T0)
    out = tmp_path / "pack.zip"

    assert handover.main(["pack", "--out", str(out)], root=mac) == 0
    told = capsys.readouterr()
    assert told.out.strip() == str(out), "the last stdout line is the pack, for a script to use"
    assert ".env is not in this pack" in told.err
    assert "do not publish from this machine" in told.err

    windows = tmp_path / "windows"
    windows.mkdir()
    assert handover.main(["unpack", str(out)], root=windows) == 0
    assert (windows / state_file(CLIENT)).read_bytes() == (mac / state_file(CLIENT)).read_bytes()
    assert "Does the site match the ledger?" in capsys.readouterr().err


def test_unpacking_over_a_newer_ledger_exits_1_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pack.zip"
    assert handover.main(["pack", "--out", str(out)], root=_data_folder(tmp_path / "mac", T0)) == 0
    windows = _data_folder(tmp_path / "windows", T0 + timedelta(hours=1))
    capsys.readouterr()

    assert handover.main(["unpack", str(out)], root=windows) == 1
    assert "refused" in capsys.readouterr().err


def test_the_pack_is_named_after_the_client_and_lands_in_the_data_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _data_folder(tmp_path / "mac", T0)

    assert handover.main(["pack", "--with-credentials"], root=mac) == 0

    told = capsys.readouterr()
    made = Path(told.out.strip())
    assert made.parent == mac and made.name.startswith(f"handover-{CLIENT}-")
    assert ".env IS in this pack" in told.err


def test_a_folder_with_no_config_is_a_usage_error(tmp_path: Path) -> None:
    assert handover.main(["pack"], root=tmp_path) == 2
