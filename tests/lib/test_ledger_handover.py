"""Handing a data folder — and the ledger — from one machine to another (:mod:`lib.handover`).

The one outcome these tests exist to rule out: a pack replacing a ledger that knows about a
publish the pack's ledger does not. Everything else — what is carried, the backup, the paths a
pack may name — is in service of that or of not losing a file.
"""

from __future__ import annotations

import json
import stat
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lib.handover import (
    MANIFEST_NAME,
    HandoverError,
    ledger_latest,
    members,
    pack,
    state_file,
    unknown_to,
    unpack,
)
from lib.records import State, StateEntry

CLIENT = "acme"
GTIN = "02987653000527"
T0 = datetime(2026, 10, 22, 5, 36, tzinfo=UTC)
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def _entry(when: datetime, title: str = "Doek") -> StateEntry:
    return StateEntry(
        wp_page_id=1,
        wp_url="https://example.test/p/",
        wp_featured_media_id=None,
        content_hash="c" * 64,
        gs1_link_set_hash="g" * 64,
        last_run=when,
        title=title,
    )


def _state(**entries: dict[str, StateEntry]) -> State:
    return State(client_id=CLIENT, entries=dict(entries))


def _write_state(root: Path, state: State) -> None:
    path = root / state_file(CLIENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(), encoding="utf-8")


def _folder(root: Path, state: State | None = None) -> Path:
    """A data folder with one of everything a pack must carry, skip, or leave alone."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "clients.yml").write_text("clients: {}\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    files = {
        "input/acme/process/uploads/GS1 export/export.xlsx": b"export",
        "input/acme/process/selection/selections.xlsx": b"list",
        "input/acme/videos/mapping.yml": b"nl: []\n",
        "input/acme/prompts/generation.v1.md": b"voice",
        "input/acme/templates/product.nl.html": b"<p>{{product_name}}</p>",
        "input/acme/videos/NL/clip.mpg": b"video",
        "input/acme/videos/FR/Clip.MP4": b"video",
        "input/acme/.DS_Store": b"cruft",
        "input/acme/videos/NL/System Volume Information/WPSettings.dat": b"windows",
        "input/acme/videos/$RECYCLE.BIN/S-1-5/desktop.ini": b"windows",
        "input/acme/process/uploads/~$export.xlsx": b"lock",
        "input/acme/superseded/handover-20261001T000000Z/clients.yml": b"old backup",
        "output/acme/data/generation_results.json": b"{}",
        "output/acme/runs/20261022T053632Z/run.jsonl": b"{}\n",
        "output/acme/media/videos/clip.mp4": b"transcode",
        "output/acme/media/hero.jpg": b"image cache",
        "input/other/elsewhere.xlsx": b"another client",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _write_state(root, state if state is not None else _state(**{GTIN: {"nl": _entry(T0)}}))
    return root


def _pack(root: Path, out: Path, *, with_credentials: bool = False) -> Path:
    pack(root, CLIENT, out, with_credentials=with_credentials, now=NOW, machine="mac")
    return out


# --- what a pack carries -------------------------------------------------------------------------


def test_a_pack_carries_the_config_the_inputs_the_client_files_and_the_ledger_and_nothing_heavy(
    tmp_path: Path,
) -> None:
    root = _folder(tmp_path / "data")

    carried = members(root, CLIENT, with_credentials=False)

    assert carried == [
        "clients.yml",
        "input/acme/process/selection/selections.xlsx",
        "input/acme/process/uploads/GS1 export/export.xlsx",
        "input/acme/prompts/generation.v1.md",
        "input/acme/templates/product.nl.html",
        "input/acme/videos/mapping.yml",
        "output/acme/data/generation_results.json",
        "output/acme/runs/20261022T053632Z/run.jsonl",
        "output/acme/state.json",
    ]


def test_credentials_travel_only_when_asked_for(tmp_path: Path) -> None:
    root = _folder(tmp_path / "data")

    assert ".env" not in members(root, CLIENT, with_credentials=False)
    assert ".env" in members(root, CLIENT, with_credentials=True)


def test_the_manifest_names_the_ledger_it_carries(tmp_path: Path) -> None:
    root = _folder(tmp_path / "data")
    out = _pack(root, tmp_path / "pack.zip")

    with zipfile.ZipFile(out) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))
        assert set(archive.namelist()) == {MANIFEST_NAME, *manifest["files"]}
    assert manifest["client_id"] == CLIENT
    assert manifest["ledger_latest"] == T0.isoformat()
    assert len(manifest["state_sha256"]) == 64
    assert manifest["with_credentials"] is False


def test_a_folder_without_a_config_is_not_a_data_folder(tmp_path: Path) -> None:
    with pytest.raises(HandoverError, match="not a data folder"):
        pack(tmp_path, CLIENT, tmp_path / "x.zip", with_credentials=False, now=NOW, machine="m")
    assert not (tmp_path / "x.zip").exists()


# --- the ledger ----------------------------------------------------------------------------------


def test_the_latest_write_is_the_newest_entry_in_any_language() -> None:
    later = T0 + timedelta(days=1)
    state = _state(**{GTIN: {"nl": _entry(T0), "fr": _entry(later)}})

    assert ledger_latest(state) == later
    assert ledger_latest(_state()) is None


def test_a_write_the_pack_does_not_know_is_reported_by_key() -> None:
    here = _state(
        **{GTIN: {"nl": _entry(T0 + timedelta(hours=1))}, "02987653000011": {"fr": _entry(T0)}}
    )
    packed = _state(**{GTIN: {"nl": _entry(T0)}})

    assert unknown_to(packed, here) == ["02987653000011/fr", f"{GTIN}/nl"]
    assert unknown_to(here, here) == []


# --- unpack --------------------------------------------------------------------------------------


def test_a_pack_lands_on_a_fresh_install_and_the_seeded_config_is_kept_aside(
    tmp_path: Path,
) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip")
    windows = tmp_path / "windows"
    windows.mkdir()
    (windows / "clients.yml").write_text("clients: democlient\n", encoding="utf-8")

    result = unpack(out, windows, now=NOW)

    assert (windows / "clients.yml").read_text(encoding="utf-8") == "clients: {}\n"
    assert (windows / state_file(CLIENT)).is_file()
    assert (windows / "input/acme/videos/mapping.yml").read_bytes() == b"nl: []\n"
    assert not (windows / ".env").exists(), "credentials were not packed"
    assert result.backup is not None
    assert (result.backup / "clients.yml").read_text(encoding="utf-8") == "clients: democlient\n"


def test_unpacking_the_same_pack_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip")
    target = tmp_path / "windows"
    target.mkdir()
    unpack(out, target, now=NOW)

    again = unpack(out, target, now=NOW)

    assert again.written == ()
    assert again.backup is None


def test_a_newer_pack_replaces_an_older_ledger(tmp_path: Path) -> None:
    later = T0 + timedelta(days=1)
    out = _pack(
        _folder(tmp_path / "mac", _state(**{GTIN: {"nl": _entry(later, "Nieuw")}})),
        tmp_path / "pack.zip",
    )
    target = _folder(tmp_path / "windows")

    result = unpack(out, target, now=NOW)

    assert state_file(CLIENT) in result.written
    replaced = State.model_validate_json((target / state_file(CLIENT)).read_text(encoding="utf-8"))
    assert replaced.entries[GTIN]["nl"].title == "Nieuw"


def test_a_machine_that_published_after_the_pack_refuses_it_and_writes_nothing(
    tmp_path: Path,
) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip")
    newer = _state(**{GTIN: {"nl": _entry(T0 + timedelta(hours=2))}})
    target = _folder(tmp_path / "windows", newer)
    (target / "clients.yml").write_text("clients: mine\n", encoding="utf-8")
    before = (target / state_file(CLIENT)).read_bytes()

    with pytest.raises(HandoverError, match=rf"refused: .*1 page\(s\).*{GTIN}/nl"):
        unpack(out, target, now=NOW)

    assert (target / state_file(CLIENT)).read_bytes() == before
    assert (target / "clients.yml").read_text(encoding="utf-8") == "clients: mine\n"
    assert not list((target / "input/acme/superseded").glob("handover-20261023*"))


def test_a_pack_without_a_ledger_cannot_replace_one(tmp_path: Path) -> None:
    source = _folder(tmp_path / "mac")
    (source / state_file(CLIENT)).unlink()
    out = _pack(source, tmp_path / "pack.zip")
    target = _folder(tmp_path / "windows")

    with pytest.raises(HandoverError, match="refused"):
        unpack(out, target, now=NOW)


def test_a_dry_run_says_what_would_change_and_writes_nothing(tmp_path: Path) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip")
    target = tmp_path / "windows"
    target.mkdir()

    result = unpack(out, target, now=NOW, dry_run=True)

    assert "clients.yml" in result.written
    assert list(target.iterdir()) == []


def test_files_here_that_the_pack_does_not_carry_are_left_alone(tmp_path: Path) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip")
    target = _folder(tmp_path / "windows")

    unpack(out, target, now=NOW)

    assert (target / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (target / "input/acme/videos/NL/clip.mpg").is_file()
    assert (target / "output/acme/media/hero.jpg").is_file()


def test_credentials_in_a_pack_are_written_owner_only(tmp_path: Path) -> None:
    out = _pack(_folder(tmp_path / "mac"), tmp_path / "pack.zip", with_credentials=True)
    target = tmp_path / "windows"
    target.mkdir()

    unpack(out, target, now=NOW)

    assert (target / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    if sys.platform != "win32":  # Windows has no owner-only bits; the guide covers NTFS
        assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "name",
    ["../outside.txt", "/etc/passwd", "input/other/x.xlsx", "C:/x", "input\\acme\\x", "x.txt"],
)
def test_a_pack_naming_a_path_outside_a_data_folder_is_refused(tmp_path: Path, name: str) -> None:
    out = tmp_path / "evil.zip"
    manifest = {
        "client_id": CLIENT,
        "packed_at": NOW.isoformat(),
        "machine": "m",
        "with_credentials": False,
        "state_sha256": None,
        "ledger_latest": None,
        "files": [name],
    }
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest))
        archive.writestr(name, "x")
    target = tmp_path / "data"
    target.mkdir()

    with pytest.raises(HandoverError, match="outside a data folder"):
        unpack(out, target, now=NOW)
    assert list(target.iterdir()) == []


def test_a_zip_that_is_not_a_pack_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "other.zip"
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("clients.yml", "x")

    with pytest.raises(HandoverError, match="not a handover pack"):
        unpack(out, tmp_path, now=NOW)
