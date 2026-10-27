"""A client's own prompt and templates: the data folder first, the release as the fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib import client_files
from lib.client_files import adopt, configured_clients, locate, voice_prompt_candidates


def _file(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def code(tmp_path: Path) -> Path:
    folder = tmp_path / "release"
    _file(folder / "prompts" / "acme" / "generation.v1.md", "release voice")
    _file(folder / "templates" / "acme" / "product.nl.html", "release page")
    _file(folder / "templates" / "_default" / "product.nl.html", "default page")
    return folder


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


def test_the_voice_prompt_is_looked_for_in_the_data_folder_first(code: Path, data: Path) -> None:
    first, second = voice_prompt_candidates("acme", "v1", data_root=data, code_root=code)
    assert first == data / "input" / "acme" / "prompts" / "generation.v1.md"
    assert second == code / "prompts" / "acme" / "generation.v1.md"


def test_locate_prefers_the_data_folder(code: Path, data: Path) -> None:
    assert locate("acme", "prompts", data_root=data, code_root=code).where == "release"
    _file(data / "input" / "acme" / "prompts" / "generation.v1.md")
    found = locate("acme", "prompts", data_root=data, code_root=code)
    assert found.where == "data"
    assert found.path == data / "input" / "acme" / "prompts"


def test_an_empty_folder_is_not_a_source(code: Path, data: Path) -> None:
    (data / "input" / "acme" / "templates").mkdir(parents=True)
    assert locate("acme", "templates", data_root=data, code_root=code).where == "release"
    assert locate("nobody", "templates", data_root=data, code_root=code).where == "none"


def test_adopt_copies_a_clients_files_into_the_data_folder(code: Path, data: Path) -> None:
    notes = adopt(code, data, ["acme"])

    assert len(notes) == 2
    prompt = data / "input" / "acme" / "prompts" / "generation.v1.md"
    assert prompt.read_text(encoding="utf-8") == "release voice"
    assert (data / "input" / "acme" / "templates" / "product.nl.html").is_file()


def test_adopt_never_touches_a_folder_the_data_folder_already_has(code: Path, data: Path) -> None:
    """Even a partial one: it is the operator's, and may be newer than the release's."""
    mine = _file(data / "input" / "acme" / "prompts" / "generation.v2.md", "newer voice")

    notes = adopt(code, data, ["acme"])

    assert [n for n in notes if "prompts" in n] == []
    assert sorted(p.name for p in mine.parent.iterdir()) == ["generation.v2.md"]
    assert mine.read_text(encoding="utf-8") == "newer voice"
    assert (data / "input" / "acme" / "templates" / "product.nl.html").is_file()


def test_adopt_is_idempotent(code: Path, data: Path) -> None:
    adopt(code, data, ["acme"])
    assert adopt(code, data, ["acme"]) == []


def test_adopt_skips_a_development_clone(code: Path) -> None:
    assert adopt(code, code, ["acme"]) == []
    assert not (code / "input").exists()


def test_adopt_takes_only_the_configured_clients_and_never_the_defaults(
    code: Path, data: Path
) -> None:
    _file(code / "prompts" / "other" / "generation.v1.md")
    adopt(code, data, ["acme", "_default"])
    assert sorted(p.name for p in (data / "input").iterdir()) == ["acme"]


def test_configured_clients_reads_the_ids(tmp_path: Path) -> None:
    config = _file(tmp_path / "clients.yml", "clients:\n  zeta: {}\n  acme:\n    a: 1\n")
    assert configured_clients(config) == ["acme", "zeta"]


@pytest.mark.parametrize("text", ["", "clients: example\n", "[unclosed", "- a\n"])
def test_configured_clients_tolerates_a_config_setup_has_not_fixed_yet(
    tmp_path: Path, text: str
) -> None:
    assert configured_clients(_file(tmp_path / "clients.yml", text)) == []


def test_configured_clients_tolerates_a_missing_file(tmp_path: Path) -> None:
    assert configured_clients(tmp_path / "clients.yml") == []


def test_the_release_root_is_read_when_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not frozen into a default argument, so a test (or a caller) can point it elsewhere."""
    monkeypatch.setattr(client_files, "CODE_ROOT", tmp_path)
    assert client_files.release_dir("acme", "prompts") == tmp_path / "prompts" / "acme"


def test_this_repository_ships_a_prompt_for_the_demo_client() -> None:
    """The example config's generator block names v1; without this file nobody else can generate."""
    assert client_files.release_dir("democlient", "prompts").joinpath("generation.v1.md").is_file()
