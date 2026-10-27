"""Render the gate contract as a TypeScript module, so the MCP servers do not restate it.

``lib/gates.py`` exists because two implementations of one safety contract drift silently. The MCP
servers need the same contract — they gate their write tools on it — and hand-copying the text into
TypeScript would make the fourth copy, in the language furthest from the tests. So the copy is
*generated*, and ``tests/lib/test_gates_export.py`` fails when it is stale.

Only the fields a gating consumer needs are emitted. Notably ``chat_only`` options are dropped: an
MCP client renders a form, exactly like the operator shell, so an option only a conversational
surface can honour has no meaning here — the same reason :attr:`GateOption.in_shell` exists.

Nothing here reads a file or executes anything; :func:`render_typescript` is a pure function of
:data:`lib.gates.GATES`.
"""

from __future__ import annotations

import json
from typing import Final

from lib.gates import GATES, Gate

#: Written into the generated file so a reader knows not to edit it, and knows what to run instead.
_BANNER: Final = """\
/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * The operator gates, rendered from `lib/gates.py` by `python -m scripts.export_gates`.
 * `tests/lib/test_gates_export.py` fails when this file is stale, so the safety contract has one
 * source rather than one per language.
 *
 * Consumers: `src/gate.ts` in this package, which puts a write tool behind the named gate.
 */
"""


def _option(value: str, label: str, consequence: str, outcome: str) -> dict[str, str]:
    return {"value": value, "label": label, "consequence": consequence, "outcome": outcome}


def _gate(gate: Gate) -> dict[str, object]:
    """One gate, reduced to what a form-rendering consumer needs."""
    return {
        "id": gate.id,
        "step": gate.step,
        "title": gate.title,
        "purpose": gate.purpose,
        "required": gate.required,
        "modes": sorted(str(mode) for mode in gate.modes),
        "needsProduction": gate.needs_production,
        "options": [
            _option(o.value, o.label, o.consequence, str(o.outcome))
            for o in gate.options
            if o.in_shell
        ],
    }


def gate_payload() -> list[dict[str, object]]:
    """The whole contract, as plain data."""
    return [_gate(gate) for gate in GATES]


def render_typescript() -> str:
    """The generated module's exact contents, including the trailing newline."""
    body = json.dumps(gate_payload(), indent=2, ensure_ascii=False)
    # `as const` so a consumer gets literal types for ids and outcomes rather than `string`.
    return (
        f"{_BANNER}\n"
        "export interface GateOption {\n"
        "  value: string;\n"
        "  label: string;\n"
        "  consequence: string;\n"
        '  outcome: "advances" | "stops" | "redisplays";\n'
        "}\n\n"
        "export interface Gate {\n"
        "  id: string;\n"
        "  step: string;\n"
        "  title: string;\n"
        "  purpose: string;\n"
        "  required: boolean;\n"
        "  modes: string[];\n"
        "  needsProduction: boolean;\n"
        "  options: GateOption[];\n"
        "}\n\n"
        f"export const GATES: readonly Gate[] = {body};\n\n"
        "/** The gate with this id, or throw — an unknown id is a bug, not input. */\n"
        "export function gateById(id: string): Gate {\n"
        "  const gate = GATES.find((g) => g.id === id);\n"
        "  if (gate === undefined) {\n"
        "    throw new Error(`no such gate: ${id}`);\n"
        "  }\n"
        "  return gate;\n"
        "}\n"
    )
