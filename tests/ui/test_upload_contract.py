"""The contract between the shell's upload handlers and NiceGUI's upload event.

This exists because of a real failure, found on the first from-scratch operator install. NiceGUI 2
handed the handler ``event.content``, a file-like object read synchronously. NiceGUI 3 replaced it
with ``event.file``, whose read methods are awaitable. ``pyproject.toml`` said ``nicegui>=2.0``, so
a fresh install resolved 3.x against code written for 2.x — and **both** uploads in the shell broke
at once: the export on the Data screen and the generated copy on the Content screen.

What made it serious was the shape of the failure, not the cause. The handler raised inside
NiceGUI, which logged it to the terminal; the browser showed the file at 100% with a checkmark and
no error; and nothing was written. An operator would have seen a successful upload and a parse that
insisted the file did not exist. Publishing nothing while reporting success is the outcome this
project refuses everywhere else — :mod:`ui.session` raises rather than build a command past an
unanswered gate, and an empty plan is refused rather than run — so it should not have been reachable
here either.

Two checks, deliberately split by what they need:

* :func:`test_every_upload_handler_is_async` and
  :func:`test_no_upload_handler_reads_the_removed_2x_attribute` are **AST** checks. They need no
  NiceGUI, so they run in the required CI job, which installs only ``.[dev]``.
* :func:`test_the_installed_nicegui_still_offers_the_api_the_handlers_use` checks the other side of
  the contract against the real package, and skips where it is absent. It runs in the
  ``Operator shell (ui extra)`` job — added in #59, because until then nothing in CI ever ran it,
  and this is the check that would have caught the break above.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import Final

import pytest

_UI: Final = Path(__file__).resolve().parent.parent.parent / "ui"

#: The annotation that marks a function as an upload handler.
_HANDLER_ANNOTATION: Final = "UploadEventArguments"

#: The NiceGUI 2 attribute that NiceGUI 3 removed.
_REMOVED_ATTRIBUTE: Final = "content"


def _handlers() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Every function in ``ui/`` that takes an upload event, with the parameter's name."""
    found = []
    for path in sorted(_UI.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for arg in node.args.args:
                annotation = arg.annotation
                name = (
                    annotation.attr
                    if isinstance(annotation, ast.Attribute)
                    else getattr(annotation, "id", None)
                )
                if name == _HANDLER_ANNOTATION:
                    found.append((path, node, arg.arg))
    return found


def test_the_upload_handlers_are_where_we_think() -> None:
    """A guard on the guard: these checks are worthless if they match nothing.

    Three now. The scope list used to be the one file that changed hands with no upload control —
    the operator had to find ``input/{client}/process-list.xlsx`` and replace it by hand, which is
    the same instruction the docs give for the two files that genuinely have no screen. It is the
    handler most worth guarding, because it is the only one that validates before it writes.
    """
    handlers = _handlers()
    assert len(handlers) >= 3, (
        "expected at least the export upload and the scope-list upload (ui/pages/data.py) and "
        f"the copy import (ui/pages/content.py); found {len(handlers)}"
    )


def test_every_upload_handler_is_async() -> None:
    """NiceGUI 3 reads an upload through awaitable methods, so a sync handler cannot read one."""
    for path, node, _ in _handlers():
        assert isinstance(node, ast.AsyncFunctionDef), (
            f"{path.name}:{node.lineno} {node.name}() takes an upload event but is not async — "
            "it cannot await event.file, and the failure is silent: the browser shows a completed "
            "upload and nothing is written"
        )


def test_no_upload_handler_reads_the_removed_2x_attribute() -> None:
    """``event.content`` is NiceGUI 2. Reaching for it raises where only the terminal sees it."""
    for path, node, param in _handlers():
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == _REMOVED_ATTRIBUTE
                and isinstance(inner.value, ast.Name)
                and inner.value.id == param
            ):
                pytest.fail(
                    f"{path.name}:{inner.lineno} reads {param}.{_REMOVED_ATTRIBUTE}, which NiceGUI "
                    "3 removed — use `await event.file.save(path)`"
                )


def test_the_installed_nicegui_still_offers_the_api_the_handlers_use() -> None:
    """The other half of the contract, against the real package.

    Skipped where NiceGUI is absent, which the required job still is — ``lib`` must stay
    installable without the ``ui`` extra. The second job installs it and runs this.
    """
    pytest.importorskip("nicegui", reason="the ui extra is not installed here")
    # Imported here, not at module scope, so the AST checks above still collect and run where
    # NiceGUI is absent — which is CI. A module-level import would skip the whole file there.
    from nicegui import events  # noqa: PLC0415
    from nicegui.elements.upload_files import FileUpload  # noqa: PLC0415

    fields = {f.name for f in dataclasses.fields(events.UploadEventArguments)}
    assert "file" in fields, f"UploadEventArguments no longer carries `file`: {sorted(fields)}"
    assert inspect.iscoroutinefunction(FileUpload.save), (
        "FileUpload.save is no longer awaitable — the handlers await it"
    )
    # The operator's own filename. The upload renames the file to export.xlsx, so this is the only
    # moment it exists — and without it "which export did that run use?" can only ever be answered
    # with a timestamp. A rename upstream would make `event.file.name` an AttributeError *inside*
    # the handler, where NiceGUI logs it and the browser still shows a completed upload.
    upload_fields = {f.name for f in dataclasses.fields(FileUpload)}
    assert "name" in upload_fields, (
        f"FileUpload no longer carries `name`, so an upload cannot record what it was called: "
        f"{sorted(upload_fields)}"
    )
