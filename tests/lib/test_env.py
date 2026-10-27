"""Unit tests for ``.env`` loading (OD-1: ``.env`` is the single source of truth).

Every test points ``lib.env.ENV_PATH`` at a temporary file. None of them may read the real
repository ``.env`` — that file holds production credentials and all four variables the
staging guards gate on.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from lib import env as env_module
from lib.env import load_env

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_returns_false_when_no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_module, "ENV_PATH", tmp_path / "absent.env")
    assert load_env() is False


def test_loads_values_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_OD1_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "ENV_PATH", env_file)
    monkeypatch.delenv("TEST_OD1_TOKEN", raising=False)

    assert load_env() is True
    assert os.environ["TEST_OD1_TOKEN"] == "from-file"


def test_does_not_override_an_existing_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``override=False`` keeps CI and deliberate one-off overrides working."""
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_OD1_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setattr(env_module, "ENV_PATH", env_file)
    monkeypatch.setenv("TEST_OD1_TOKEN", "from-environment")

    load_env()
    assert os.environ["TEST_OD1_TOKEN"] == "from-environment"


def test_env_path_resolves_to_the_repository_root() -> None:
    """With ``GS1_DATA_DIR`` unset (``conftest.py`` drops it), ``.env`` is the repository's."""
    assert env_module.ENV_PATH == _REPO_ROOT / ".env"


# --- the constraint that makes this safe -------------------------------------


def _reads_dotenv(path: Path) -> bool:
    """Whether ``path`` imports or calls anything that reads ``.env``.

    Walks the AST rather than scanning the text, which the sibling check below has always done.
    A text scan forbids *mentioning* the rule as well as breaking it, so a module that documents
    why it must not load ``.env`` — or one that tests for exactly this — could not be written.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _DOTENV_MODULES:
            return True
        if isinstance(node, ast.Import) and any(a.name in _DOTENV_MODULES for a in node.names):
            return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _DOTENV_CALLS:
                return True
    return False


_DOTENV_MODULES = {"lib.env", "dotenv"}
_DOTENV_CALLS = {"load_env", "load_dotenv", "dotenv_values"}


def test_no_test_module_or_conftest_loads_env() -> None:
    """``.env`` must never reach the test path — it would arm the live-writing staging tests."""
    offenders = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in [*(_REPO_ROOT / "tests").rglob("*.py"), *_REPO_ROOT.glob("conftest.py")]
        if path.name != "test_env.py" and _reads_dotenv(path)
    ]
    assert offenders == []


def test_the_ui_shell_never_loads_env_either() -> None:
    """It subprocesses the scripts, and each of those loads ``.env`` in its own ``__main__``.

    Loading it in the shell would put production credentials into a long-lived desktop process
    for no benefit, and would arm the four staging-guard variables inside it. Checked here rather
    than in ``tests/ui/`` so every rule about ``.env`` lives in the file that owns it.
    """
    package = _REPO_ROOT / "ui"
    if not package.is_dir():  # the [ui] extra is optional; the package may not be installed
        return
    offenders = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in package.rglob("*.py")
        if _reads_dotenv(path)
    ]
    assert offenders == []


#: Scripts that are not operational entry points: they take no credential and reach no network.
#: ``export_gates`` generates or checks files already in the tree; ``data_folder`` decides where the
#: data folder is, so it cannot start inside one.
#:
#: Exempt from the ``load_env`` and ``enter_data_dir`` rule because loading production credentials
#: into either is the thing this file exists to prevent, not an instance of it. Kept as an explicit
#: set — and held to :func:`test_codegen_scripts_stay_credential_free` below — so the exemption
#: cannot quietly become the place a real entry point hides.
_CODEGEN_SCRIPTS = {"export_gates.py", "data_folder.py"}

#: Importing any of these means a script can reach a credential or the network, so it is not
#: codegen and belongs back under the rule.
_OPERATIONAL_MODULES = {
    "dotenv",
    "httpx",
    "lib.config",
    "lib.env",
    "lib.gs1_dl_client",
    "lib.wp_client",
    "requests",
}


def _script_paths() -> list[Path]:
    paths = sorted(p for p in (_REPO_ROOT / "scripts").glob("*.py") if p.name != "__init__.py")
    assert paths, "expected script entry points to exist"
    return paths


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@pytest.mark.parametrize("entry_call", ["load_env", "enter_data_dir"])
def test_scripts_call_entry_setup_from_main_block_not_from_main(entry_call: str) -> None:
    """Tests call ``main()`` directly, so a call sited there would run under pytest.

    For ``load_env`` that loads production credentials into the pytest process; for
    ``enter_data_dir`` it moves the test run's working directory into a real installation's data.
    Asserts the call is a direct statement of the module-level ``if __name__ == "__main__":``
    block, and that no function body anywhere in the module calls it.
    """
    for path in _script_paths():
        if path.name in _CODEGEN_SCRIPTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        in_main_block = any(
            isinstance(node, ast.If)
            and any(
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == entry_call
                for stmt in node.body
            )
            for node in tree.body
        )
        assert in_main_block, f"{path.name}: {entry_call}() is not called in the __main__ block"

        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls_it = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == entry_call
                for node in ast.walk(func)
            )
            assert not calls_it, (
                f"{path.name}: {entry_call}() is called inside {func.name}() — tests call main() "
                "directly, so it would run inside the pytest process"
            )


def test_codegen_scripts_stay_credential_free() -> None:
    """The exemption is only sound while the exempt scripts remain what it describes.

    So it is checked in both directions: an exempt script must neither load ``.env`` nor import
    anything that could reach a credential or the network. The day one does, this fails and it goes
    back under the rule above — rather than keeping a quiet pass it no longer deserves.
    """
    present = {p.name for p in _script_paths()}
    assert present >= _CODEGEN_SCRIPTS, (
        f"exemption names a script that no longer exists: {_CODEGEN_SCRIPTS - present}"
    )

    for path in _script_paths():
        if path.name not in _CODEGEN_SCRIPTS:
            continue
        assert not _reads_dotenv(path), f"{path.name}: exempt from load_env, yet reads .env"
        offenders = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        offenders &= _OPERATIONAL_MODULES
        assert not offenders, f"{path.name}: no longer credential-free; imports {sorted(offenders)}"
