"""The shell may *offer* generation; it may never *do* it.

``ui/__init__.py`` used to claim the shell had no LLM at all, and that claim was enforced by
nothing — unlike the ``.env`` half of the same paragraph, which ``tests/lib/test_env.py`` walks
the AST for. The claim held only because nobody had written the import.

Now that the Content screen has a Generate button, the property that matters is narrower and has
to be real: reaching Anthropic stays the **subprocess's** job. ``ui/`` builds an argv and streams
the output; ``scripts/run_generate.py`` loads ``.env`` in its own ``__main__`` block, holds the
key, and makes the call. Nothing in this long-lived desktop process ever holds the credential or
opens the socket.

The two halves are separable and this is the one without a guard, so it gets one. An in-process
``AnthropicClient`` would work first time, look identical on screen, and quietly undo the reason
the subprocess seam exists.

AST rather than a text scan, for the reason ``tests/lib/test_env.py`` gives: a text scan forbids
*mentioning* the rule, so neither this file nor the docstring explaining it could be written.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent

#: Importing any of these from ``ui/`` puts the API call in this process.
_EGRESS_MODULES: Final = {"lib.llm", "anthropic", "httpx"}

#: Calling any of these does the same without an import the check above would see.
_EGRESS_NAMES: Final = {"AnthropicClient", "load_voice_template", "run_producer"}


def _reaches_anthropic(path: Path) -> bool:
    """Whether ``path`` imports or calls anything that would make the API call in-process."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _EGRESS_MODULES:
            return True
        if isinstance(node, ast.Import) and any(a.name in _EGRESS_MODULES for a in node.names):
            return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _EGRESS_NAMES:
                return True
    return False


def test_the_shell_never_calls_anthropic_in_process() -> None:
    """Generation is a subprocess. The key and the socket belong to the child, not to this app."""
    package = _REPO_ROOT / "ui"
    if not package.is_dir():  # the [ui] extra is optional; the package may not be installed
        return
    offenders = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in package.rglob("*.py")
        if _reaches_anthropic(path)
    ]
    assert offenders == []


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    """Return ``tree`` with every docstring removed, so prose may say what code may not do."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:]
    return tree


def _builds_the_generator_command(path: Path) -> bool:
    """Whether ``path`` writes the ``run_generate`` module name into code, docstrings aside."""
    tree = _strip_docstrings(ast.parse(path.read_text(encoding="utf-8")))
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "scripts.run_generate" in node.value
        for node in ast.walk(tree)
    )


def test_the_shell_reaches_the_generator_only_through_the_named_command() -> None:
    """``run_generate`` is spelled once, as an argv, in the one module that names commands.

    A second spelling somewhere in ``ui/pages/`` would be a second answer to "what does the shell
    run", which is exactly what ``runner``'s command block exists to prevent — and the flag it
    would most likely carry is ``--emit``, which answers a producer that is not present.

    Docstrings are stripped before looking, so a screen may still *explain* that it subprocesses
    the generator. Saying so is how the arrangement stays understood; doing so is the violation.
    """
    package = _REPO_ROOT / "ui"
    if not package.is_dir():
        return
    naming = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in package.rglob("*.py")
        if _builds_the_generator_command(path)
    )
    assert naming == ["ui/runner.py"]
