"""What the operator ticked on the Data screen, and what was wrong with each — kept for the run.

Operator, 2026-10-09: the report after a run must be the report the Data screen showed — about the
**ticked** products, not only the ones that could run. Next saves only the runnable ones (the file
a run reads *is* the batch), so a ticked product that could not run never reaches the run, and
without this the run's report could not name it.

So Next also writes this snapshot beside the saved selection: the ticked products and their issues,
exactly as the screen judged them, plus the sha256 of the selection it was saved with. A run copies
it into its own directory **only when that hash still matches** the selection it is about to read:
a selection saved again, edited by hand or set aside since cannot borrow another batch's ticks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lib.issue_report import CATEGORIES, Issue
from lib.provenance import sha256_of

_VERSION: Final = 1
_SUFFIX: Final = ".ticked.json"
_BY_KEY: Final = {category.key: category for category in CATEGORIES}


@dataclass(frozen=True)
class Ticked:
    """The ticked products in list order, ``(barcode, name)``, and their issues."""

    products: tuple[tuple[str, str], ...]
    issues: tuple[Issue, ...]


def path_for(selection: Path) -> Path:
    """Where ``selection``'s snapshot lives: ``selections.xlsx`` → ``selections.ticked.json``."""
    return selection.with_name(selection.stem + _SUFFIX)


def write(selection: Path, products: list[tuple[str, str]], issues: list[Issue]) -> Path:
    """Snapshot the ticks for the selection just saved at ``selection``. Returns where it went."""
    path = path_for(selection)
    payload = {
        "v": _VERSION,
        "selection_sha256": sha256_of(selection),
        "products": [list(product) for product in products],
        "issues": [
            {"gtin": i.gtin, "name": i.name, "category": i.category.key, "reason": i.reason}
            for i in issues
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def read(path: Path, selection: Path | None = None) -> Ticked | None:
    """The snapshot at ``path``, or ``None`` when it is absent, unreadable or for another selection.

    With ``selection`` given, the snapshot must have been written for that file's current content.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("v") != _VERSION:
            return None
        if selection is not None and data.get("selection_sha256") != sha256_of(selection):
            return None
        products = tuple((str(gtin), str(name)) for gtin, name in data["products"])
        issues = tuple(
            Issue(str(i["gtin"]), str(i["name"]), _BY_KEY[i["category"]], str(i["reason"]))
            for i in data["issues"]
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return Ticked(products, issues)
