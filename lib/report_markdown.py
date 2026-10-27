"""The two markdown primitives every section of the data-quality report is built from.

Their own module so that a section can live in its own file: :mod:`lib.quality_report` was past
the project's size limit before the video section arrived, and that section importing these back
from the renderer that imports it would be a cycle.
"""

from __future__ import annotations


def cell(text: str) -> str:
    """Escape a value for a single markdown table cell (pipes would split the column)."""
    return text.replace("|", "\\|")


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A markdown table, or a single ``_None._`` line when there are no rows."""
    if not rows:
        return ["_None._"]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(cells) + " |" for cells in rows]
    return out
