"""Write the generated gate module into every MCP package that gates a write tool.

Run after changing ``lib/gates.py``:

    python -m scripts.export_gates

``--check`` writes nothing and exits 1 when a file is stale — what CI and
``tests/lib/test_gates_export.py`` use, so a gate added in Python cannot quietly stop being shown
by a server that still ships last week's copy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from lib.gates_export import render_typescript

#: Every package whose tools gate on the contract. `qr-render` writes only local files, so it is
#: deliberately absent rather than carrying an unused copy.
TARGETS: Final = (
    Path("mcps/wordpress/src/gates.generated.ts"),
    Path("mcps/gs1-nl/src/gates.generated.ts"),
)


def main(argv: list[str] | None = None) -> int:
    """Write (or check) the generated module. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any target is missing or stale",
    )
    args = parser.parse_args(argv)

    rendered = render_typescript()
    stale: list[Path] = []
    for target in TARGETS:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered:
            continue
        if args.check:
            stale.append(target)
            continue
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target}")

    if stale:
        for target in stale:
            print(f"stale: {target}", file=sys.stderr)
        print("run `python -m scripts.export_gates` and commit the result", file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(TARGETS)} generated gate module(s) up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
