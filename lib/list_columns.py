"""Which column of an uploaded product list is the barcode, and which the page link.

Every reader of the selection list finds its table by the configured heading
(``process_list.gtin_column``, ``Barcode`` for Democlient) and reads the link column by
``process_list.target_url_column``. A list whose barcode column is headed ``EAN`` or ``GTIN-13``, or
sits under a title row, used to be refused outright, and the operator was told to rename a column
in Excel to match a name the screen never showed them.

So an upload is read here first, **without** knowing its headings, and the operator says which
column is which, the way the video sign-off sheet works. What is installed is then normalised: the
chosen columns carry the configured headings, so every reader downstream (the plan, the preflight,
the result sheet, the run's copy of the list) reads it unchanged. The upload itself is kept exactly
as it was sent, in its dated copy.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from lib.config import ProcessListConfig
from lib.video_signoff import GTIN_COLUMNS
from lib.xlsx import Grid, read_grid

#: What a heading that already carries a configured name is renamed to, when it is not the column
#: chosen for that name. Two ``Barcode`` columns would leave the reader taking the first.
NOT_USED_SUFFIX: Final = " (not used)"


@dataclass(frozen=True)
class ListColumns:
    """The chosen columns, by position in the grid's header."""

    barcode: int
    link: int | None = None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


#: How a heading names the barcode, beyond the exact spellings the sign-off sheet accepts:
#: ``EAN code``, ``GTIN-13 (consumer unit)``, ``Barcode NL``.
_BARCODE_PREFIXES: Final = ("ean", "gtin", "barcode")

#: A barcode as people paste it: 8 to 14 digits once spaces and dots are gone.
_BARCODE: Final = re.compile(r"\d{8,14}")

#: How many of the chosen column's values a refusal quotes.
_QUOTED: Final = 3


def _names_a_barcode(text: str) -> bool:
    norm = _norm(text)
    return norm in GTIN_COLUMNS or norm.startswith(_BARCODE_PREFIXES)


def _filled(texts: Sequence[str]) -> int:
    return sum(1 for text in texts if text.strip())


def read_upload(data: bytes, config: ProcessListConfig) -> Grid | None:
    """The uploaded list's table, found by its configured heading, a barcode spelling, or shape.

    Four passes, most specific first: the configured heading exactly (an ordinary upload, read the
    same way a run reads it); any spelling of "barcode" the sign-off sheet also accepts; the first
    row with two filled cells; the first row with one, for a list that is a single column. The last
    two can take a title row for the header. That is survivable because the operator is shown the
    headings it found, and a title listed as the only column is unmistakable.
    """
    passes: tuple[Callable[[list[str]], bool], ...] = (
        lambda texts: config.gtin_column in texts,
        lambda texts: any(_names_a_barcode(text) for text in texts),
        lambda texts: _filled(texts) >= 2,  # noqa: PLR2004 — a barcode and one other column
        lambda texts: _filled(texts) >= 1,
    )
    for recognises in passes:
        grid = read_grid(io.BytesIO(data), header_row=recognises)
        if grid is not None:
            return grid
    return None


def guess(grid: Grid, config: ProcessListConfig) -> tuple[int | None, int | None]:
    """The best guess at ``(barcode, link)`` positions, either ``None`` where there is none.

    The configured heading wins; failing that, a barcode spelling. The link column is guessed only
    by its configured heading: a URL column has no conventional name to look for.
    """
    header = grid.header
    barcode = next((i for i, text in enumerate(header) if text == config.gtin_column), None)
    if barcode is None:
        barcode = next((i for i, text in enumerate(header) if _names_a_barcode(text)), None)
    link = None
    if config.target_url_column is not None:
        link = next((i for i, text in enumerate(header) if text == config.target_url_column), None)
    return barcode, link


def is_as_configured(grid: Grid, columns: ListColumns, config: ProcessListConfig) -> bool:
    """Whether installing with ``columns`` needs no renaming: the file already reads as chosen."""
    return normalised_header(grid, columns, config) == grid.header


def normalised_header(grid: Grid, columns: ListColumns, config: ProcessListConfig) -> list[str]:
    """The header with the chosen columns carrying the configured headings, and only them."""
    wanted = {columns.barcode: config.gtin_column}
    if columns.link is not None and config.target_url_column is not None:
        wanted[columns.link] = config.target_url_column
    taken = set(wanted.values())
    header: list[str] = []
    for index, text in enumerate(grid.header):
        if index in wanted:
            header.append(wanted[index])
        elif text in taken:
            header.append(text + NOT_USED_SUFFIX)
        else:
            header.append(text)
    return header


def barcode_problem(grid: Grid, columns: ListColumns) -> str | None:
    """Why the chosen barcode column cannot be the barcode column, or ``None`` when it can.

    A run's reader only asks that a cell is filled, so a column of product *names* would install
    and plan nothing. A column chosen by hand is checked for at least one value shaped like a
    barcode, and the refusal quotes what the column does hold.
    """
    values = [row[columns.barcode].strip() for row in grid.rows if row[columns.barcode].strip()]
    if any(_BARCODE.fullmatch(re.sub(r"[\s.\-]", "", value)) for value in values):
        return None
    heading = grid.header[columns.barcode].strip() or f"column {columns.barcode + 1}"
    shown = ", ".join(values[:_QUOTED]) or "nothing"
    return f"the column {heading!r} holds no barcodes (it starts: {shown})"
