"""The data folder: where ``.env``, ``clients.yml``, ``input/`` and ``output/`` are read from.

Unset, it is the repository — every installation before ``GS1_DATA_DIR`` existed, unchanged.
Set, it must be an existing folder, and every entry point works inside it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from lib import data_dir
from lib.data_dir import CODE_ROOT, DATA_DIR_ENV, data_root, enter_data_dir

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def test_code_root_is_the_repository() -> None:
    assert CODE_ROOT == _REPO_ROOT


def test_unset_means_the_code_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert data_root() == CODE_ROOT


def test_blank_means_the_code_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, "   ")
    assert data_root() == CODE_ROOT


def test_set_names_the_data_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert data_root() == tmp_path.resolve()


def test_a_missing_folder_is_refused_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falling back to the code folder would start a second ledger there without saying so."""
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(NotADirectoryError, match="GS1_DATA_DIR"):
        data_root()


def test_enter_changes_nothing_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert enter_data_dir() == CODE_ROOT
    assert Path.cwd() == tmp_path.resolve()


def test_enter_moves_into_the_data_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setenv(DATA_DIR_ENV, str(target))
    monkeypatch.chdir(tmp_path)
    assert enter_data_dir() == target.resolve()
    assert Path.cwd() == target.resolve()


def test_a_relative_setting_is_made_absolute_for_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell starts every child inside the data folder, with its own environment."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(DATA_DIR_ENV, "data")
    enter_data_dir()
    assert os.environ[DATA_DIR_ENV] == str((tmp_path / "data").resolve())
    assert data_root() == (tmp_path / "data").resolve()


def test_entry_points_read_config_and_env_from_the_data_folder(tmp_path: Path) -> None:
    """A fresh process, as the shell and the container start one: both defaults move together."""
    probe = (
        "from lib.config import DEFAULT_CLIENTS_PATH; from lib.env import ENV_PATH; "
        "print(DEFAULT_CLIENTS_PATH); print(ENV_PATH)"
    )
    env = {**os.environ, DATA_DIR_ENV: str(tmp_path), "PYTHONPATH": str(_REPO_ROOT)}
    result = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    )
    clients, dotenv = result.stdout.split()
    assert Path(clients) == tmp_path.resolve() / "clients.yml"
    assert Path(dotenv) == tmp_path.resolve() / ".env"


#: ``<anything>_ROOT / "<data name>"`` — a data location built from a root other than the data one.
_DATA_NAMES = r"(\.env|clients\.yml|input|output)"
_CODE_ROOTED_DATA = re.compile(rf"\b(?:REPO_ROOT|CODE_ROOT|_ROOT)\s*/\s*[\"']{_DATA_NAMES}[\"']")
_FILE_ROOTED_DATA = re.compile(rf"Path\(__file__\)[^\n]*/\s*[\"']{_DATA_NAMES}[\"']")

#: ``Path("prompts")`` and the like — a shipped asset looked up in the working directory, which is
#: the data folder once ``GS1_DATA_DIR`` is set.
_CWD_ROOTED_ASSET = re.compile(r"Path\([\"'](prompts|templates|schema|reference)\b")


def test_no_data_location_is_built_from_the_code_folder() -> None:
    """Data paths go through the data folder or the working directory — never next to the code.

    One such path is enough to split an installation in two: the ledger written beside the code,
    and everything else beside the data, with an update free to replace the first.
    """
    sources = [
        path
        for package in ("lib", "scripts", "ui")
        for path in (_REPO_ROOT / package).rglob("*.py")
        if path != Path(data_dir.__file__).resolve()
    ]
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _CODE_ROOTED_DATA.search(line) or _FILE_ROOTED_DATA.search(line)
    ]
    assert not offenders, "data location built from the code folder:\n" + "\n".join(offenders)


def test_no_shipped_asset_is_looked_up_in_the_working_directory() -> None:
    """The reverse mistake: ``prompts/`` and friends ship with the code, not with the data."""
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for package in ("lib", "scripts", "ui")
        for path in (_REPO_ROOT / package).rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _CWD_ROOTED_ASSET.search(line)
    ]
    assert not offenders, "shipped asset resolved from the data folder:\n" + "\n".join(offenders)
