"""Tests for how this repository installs itself on the operator's machine.

The operator does not run ``pip``. They double-click ``install.command`` (or ``install.bat``),
which fetches ``uv``, has it fetch a CPython, and builds ``.venv`` **from the committed
``uv.lock``** rather than from a fresh resolution. That makes the lockfile part of the product:
it is what says which versions that machine gets, and it is what an IT reviewer vets.

Two kinds of drift would break it quietly, so both are checked here:

* **``uv.lock`` against ``pyproject.toml``.** A dependency added without re-locking installs on
  the maintainer's machine (``pip install -e``) and *fails* on the operator's, where ``uv sync
  --locked`` refuses to resolve. CI also runs ``uv lock --check``, which is the authority; this
  test is the offline half, so the mismatch is visible without a network or a uv binary.
* **The pins inside the four entry points.** The uv version and the Python version are spelled
  out in the scripts and in ``.github/workflows/ci.yml``, deliberately — there is no
  ``.python-version`` file, because pyenv reads that too and would break ``python`` in this
  directory for anyone who has pyenv without 3.11. Spelling a constant four times only works if
  something notices when one copy moves.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_ROOT: Final = Path(__file__).resolve().parent.parent
_LOCK: Final = _ROOT / "uv.lock"
_PYPROJECT: Final = _ROOT / "pyproject.toml"
_CI: Final = _ROOT / ".github" / "workflows" / "ci.yml"

_INSTALL_SH: Final = _ROOT / "install.command"
_START_SH: Final = _ROOT / "start.command"
_INSTALL_BAT: Final = _ROOT / "install.bat"
_START_BAT: Final = _ROOT / "start.bat"
_DOCKERFILE: Final = _ROOT / "Dockerfile"

#: The distribution's own entry in the lockfile.
_PROJECT_NAME: Final = "gs1-digital-link-orchestrator"

#: `extra == 'ui'` inside a lockfile marker — which optional group a requirement belongs to.
_EXTRA_MARKER: Final = re.compile(r"extra\s*==\s*['\"]([^'\"]+)['\"]")


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(_PYPROJECT.read_text("utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads(_LOCK.read_text("utf-8"))


def _locked_project() -> dict[str, Any]:
    packages = _lock()["package"]
    return next(p for p in packages if p["name"] == _PROJECT_NAME)


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so ``PyYAML`` and ``pyyaml`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


#: (name, extras, specifier, owning optional-dependency group or None).
_Requirement = tuple[str, tuple[str, ...], SpecifierSet, str | None]


def _declared_requirements() -> set[_Requirement]:
    """What ``pyproject.toml`` asks for."""
    data = _pyproject()["project"]
    declared: set[_Requirement] = set()
    for spec in data.get("dependencies", []):
        req = Requirement(spec)
        declared.add((_canonical(req.name), tuple(sorted(req.extras)), req.specifier, None))
    for group, specs in data.get("optional-dependencies", {}).items():
        for spec in specs:
            req = Requirement(spec)
            declared.add((_canonical(req.name), tuple(sorted(req.extras)), req.specifier, group))
    return declared


def _locked_requirements() -> set[_Requirement]:
    """What ``uv.lock`` recorded this project asking for, when it was written."""
    locked: set[_Requirement] = set()
    for entry in _locked_project()["metadata"]["requires-dist"]:
        marker = entry.get("marker", "")
        found = _EXTRA_MARKER.search(marker)
        locked.add(
            (
                _canonical(entry["name"]),
                tuple(sorted(entry.get("extras", []))),
                SpecifierSet(entry.get("specifier", "")),
                found.group(1) if found else None,
            )
        )
    return locked


# --- The lockfile is committed -------------------------------------------------


def test_the_lockfile_is_present() -> None:
    """Without it, ``uv sync --locked`` on the operator's machine has nothing to install from."""
    assert _LOCK.is_file(), "uv.lock is missing — run `uv lock` and commit the result"


def test_the_lockfile_is_not_gitignored() -> None:
    """It was ignored until Phase 4. Re-ignoring it would leave a stale copy in the checkout."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "uv.lock"],
            cwd=_ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git, e.g. a sdist
        pytest.skip("git is not available here")
    assert result.returncode != 0, (
        "uv.lock is ignored again — the operator's install would use whatever stale copy "
        "happens to be in their folder, or fail outright"
    )


# --- The lockfile agrees with pyproject.toml -----------------------------------


def test_the_lockfile_covers_every_declared_dependency() -> None:
    missing = _declared_requirements() - _locked_requirements()
    assert not missing, (
        f"pyproject.toml declares requirements the lockfile has not seen: {sorted(missing)}. "
        "Run `uv lock` and commit it, or the operator's `uv sync --locked` refuses to install."
    )


def test_the_lockfile_holds_nothing_pyproject_no_longer_asks_for() -> None:
    stale = _locked_requirements() - _declared_requirements()
    assert not stale, (
        f"the lockfile still pins requirements pyproject.toml has dropped: {sorted(stale)}. "
        "Run `uv lock` and commit it."
    )


def test_the_lockfile_offers_the_same_extras() -> None:
    """``ui`` in particular: the installers ask for it by name, and it is where NiceGUI lives."""
    declared = set(_pyproject()["project"].get("optional-dependencies", {}))
    assert set(_locked_project()["metadata"]["provides-extras"]) == declared
    assert "ui" in declared, "the installers run `uv sync --extra ui`"


def test_the_lockfile_targets_the_same_python_range() -> None:
    assert _lock()["requires-python"] == _pyproject()["project"]["requires-python"]


# --- The four entry points -----------------------------------------------------


@pytest.mark.parametrize(
    "script", [_INSTALL_SH, _START_SH, _INSTALL_BAT, _START_BAT], ids=lambda p: p.name
)
def test_the_entry_point_exists(script: Path) -> None:
    assert script.is_file(), f"{script.name} is what the operator double-clicks"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit to check")
@pytest.mark.parametrize("script", [_INSTALL_SH, _START_SH], ids=lambda p: p.name)
def test_the_macos_entry_point_is_executable(script: Path) -> None:
    """A ``.command`` without the bit does not open on a double-click; it opens in an editor."""
    assert script.stat().st_mode & 0o111, f"chmod +x {script.name}"


def _pinned(path: Path, pattern: str) -> str:
    """The single pinned value in ``path``, asserting every copy inside that file agrees.

    ``re.search`` would return the first match and ignore the rest, so a second CI job pinned to
    a different Python would drift silently — the exact failure this file exists to prevent.
    """
    found = re.findall(pattern, path.read_text("utf-8"))
    assert found, f"no pin matching {pattern!r} in {path.name}"
    assert len(set(found)) == 1, f"{path.name} pins more than one value: {sorted(set(found))}"
    return str(found[0])


def test_the_uv_version_is_pinned_to_one_value() -> None:
    """A uv older than the one that wrote the lockfile may not know its revision."""
    pins = {
        _INSTALL_SH.name: _pinned(_INSTALL_SH, r'UV_VERSION="([\d.]+)"'),
        _INSTALL_BAT.name: _pinned(_INSTALL_BAT, r'set "UV_VERSION=([\d.]+)"'),
        _CI.name: _pinned(_CI, r"astral\.sh/uv/([\d.]+)/install\.sh"),
        _DOCKERFILE.name: _pinned(_DOCKERFILE, r"FROM ghcr\.io/astral-sh/uv:([\d.]+)"),
    }
    assert len(set(pins.values())) == 1, f"the pinned uv version has drifted apart: {pins}"


def test_the_python_version_is_pinned_to_one_value() -> None:
    """The operator's interpreter is the one the suite runs on, which is the whole point."""
    pins = {
        _INSTALL_SH.name: _pinned(_INSTALL_SH, r'PYTHON_VERSION="([\d.]+)"'),
        _START_SH.name: _pinned(_START_SH, r'PYTHON_VERSION="([\d.]+)"'),
        _INSTALL_BAT.name: _pinned(_INSTALL_BAT, r'set "PYTHON_VERSION=([\d.]+)"'),
        _START_BAT.name: _pinned(_START_BAT, r'set "PYTHON_VERSION=([\d.]+)"'),
        _CI.name: _pinned(_CI, r'python-version: "([\d.]+)"'),
        _DOCKERFILE.name: _pinned(_DOCKERFILE, r"FROM python:([\d.]+)-slim"),
    }
    assert len(set(pins.values())) == 1, f"the pinned Python version has drifted apart: {pins}"

    pinned = pins[_INSTALL_SH.name]
    requires = SpecifierSet(_pyproject()["project"]["requires-python"])
    assert requires.contains(Version(pinned)), (
        f"the installers pin Python {pinned}, which requires-python ({requires}) excludes"
    )


def test_the_installers_build_from_the_lockfile() -> None:
    """``--locked`` is the refusal to resolve: a stale lock stops the install, loudly."""
    for script in (_INSTALL_SH, _INSTALL_BAT):
        text = script.read_text("utf-8")
        assert "sync --extra ui --locked" in text, (
            f"{script.name} must install the ui extra from the lockfile, without resolving"
        )


def test_the_start_scripts_launch_the_shell_without_touching_the_lockfile() -> None:
    """``--frozen``: starting the app is not the moment to resolve new versions."""
    for script in (_START_SH, _START_BAT):
        text = script.read_text("utf-8")
        assert "run --frozen --extra ui" in text, f"{script.name} must run from the locked env"
        assert "python -m ui" in text, f"{script.name} must start the operator shell"
