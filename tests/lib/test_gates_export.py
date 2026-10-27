"""The generated TypeScript gate modules must not drift from ``lib/gates.py``.

This is the same contract ``tests/lib/test_gates.py`` enforces between ``lib/gates.py`` and
``SKILL.md``, extended to the two MCP servers that gate their write tools on it. Without this test
the servers would ship whatever copy was current when someone last remembered to regenerate —
which is precisely how the WordPress client accumulated nine unported fixes (#75, #85).

A gate that quietly stops being shown raises nothing, so the check has to be mechanical.
"""

from __future__ import annotations

import json
import re

from lib.gates import GATES
from lib.gates_export import gate_payload, render_typescript
from scripts.export_gates import TARGETS, main


def test_every_generated_module_is_current() -> None:
    """Each committed module equals what the exporter produces right now."""
    expected = render_typescript()
    for target in TARGETS:
        assert target.exists(), f"{target} is missing; run `python -m scripts.export_gates`"
        assert target.read_text(encoding="utf-8") == expected, (
            f"{target} is stale; run `python -m scripts.export_gates` and commit the result"
        )


def test_check_mode_passes_on_the_committed_files() -> None:
    """``--check`` is what CI runs; it must agree with the files in the tree."""
    assert main(["--check"]) == 0


def test_payload_carries_every_gate() -> None:
    """No gate is silently dropped from the export."""
    assert [g["id"] for g in gate_payload()] == [g.id for g in GATES]


def test_the_two_gates_the_servers_use_are_present() -> None:
    """`intent` and `production` are the ids the MCP handlers name.

    Named explicitly because the servers look them up by string: renaming a gate in
    ``lib/gates.py`` without updating ``tools.ts`` would throw at call time, on the write path,
    which is the worst possible place to discover it.
    """
    ids = {g["id"] for g in gate_payload()}
    assert {"intent", "production"} <= ids


def test_chat_only_options_are_not_exported() -> None:
    """An MCP client renders a form, so an option only chat can honour has no meaning here."""
    exported = {
        (gate["id"], option["value"])
        for gate in gate_payload()
        for option in gate["options"]  # type: ignore[attr-defined]
    }
    chat_only = {(g.id, o.value) for g in GATES for o in g.options if o.chat_only}
    assert chat_only, "no chat-only options left — this test would silently stop asserting"
    assert not (exported & chat_only)


def test_generated_module_is_valid_enough_to_parse() -> None:
    """The embedded payload is real JSON, so a TypeScript consumer gets what Python meant."""
    rendered = render_typescript()
    match = re.search(r"export const GATES: readonly Gate\[\] = (\[.*?\]);\n", rendered, re.S)
    assert match is not None
    assert json.loads(match.group(1)) == gate_payload()


def test_the_banner_warns_against_editing() -> None:
    """A generated file that does not say so gets hand-edited, and the edit is lost."""
    rendered = render_typescript()
    assert "DO NOT EDIT" in rendered
    assert "scripts.export_gates" in rendered
