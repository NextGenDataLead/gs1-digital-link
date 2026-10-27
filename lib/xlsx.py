"""Read a worksheet out of an .xlsx as a grid of text, without openpyxl.

This is :mod:`lib.process_list`'s reader, lifted out when a second caller needed it. The reasons it
is hand-written are all properties of the files real clients send, and every one of them was
discovered the hard way:

* **Strict Open XML.** The pilot's own export is saved in it, and ``openpyxl`` reads **zero sheets**
  from those — no error, an empty workbook. Hence the namespace-agnostic tag matching in
  :func:`_local`: Strict and Transitional differ in their namespace URIs and nothing else that
  matters here.
* **The table does not start at A1.** A report title, an export timestamp or a pivot summary sits
  above it, so the header row is *found* rather than assumed — see :func:`read_grid`.
* **The table is not on the first sheet.** It sits beside a pivot, so every sheet is tried in
  workbook order and the first one carrying a header wins.
* **Columns past Z.** ``["A", "Z", "AA"]`` sorted as strings gives ``A, AA, Z``, which silently
  reorders the grid and leaves a column index pointing at the wrong column. A 28-column operator
  file is ordinary, so :func:`_column_number` sorts by position.

Everything comes back as **text**, stripped. What a value *means* — a 13-digit barcode against a
14-digit one, a whole-number float — is the caller's business, settled once per domain
(:func:`lib.process_list._coerce_gtin`, :func:`lib.media_video.canon_gtin`) rather than guessed
here. A reader that returned numbers would have to decide, and would then be a third opinion.

No error type of its own: the ``zipfile``/``ElementTree`` exceptions come out as they are, so each
caller can say "cannot read process list at …" or "cannot read the sign-off sheet at …" in its own
vocabulary. ``None`` from :func:`read_grid` means no sheet carried the header — not a read failure,
and a different message.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class Grid:
    """One worksheet's header row and the rows beneath it, as stripped text.

    Attributes:
        header: The header row's cells, in spreadsheet order. May contain blank strings — a column
            with an empty header cell is still the operator's column.
        rows: The data rows, each padded to the full width, blank-only rows dropped.
    """

    header: list[str]
    rows: list[list[str]]

    def index_of(self, *names: str) -> int | None:
        """The position of the first header cell equal to any of ``names``, or ``None``.

        Exact, after the strip the reader already did. Callers that accept several spellings of a
        column normalise both sides themselves and use :attr:`header` — matching loosely in here
        would make "gtin" and "candidate_1_gtin" the same question.
        """
        for name in names:
            if name in self.header:
                return self.header.index(name)
        return None


def read_grid(source: Path | IO[bytes], *, header_row: Callable[[list[str]], bool]) -> Grid | None:
    """The first worksheet whose header ``header_row`` recognises, as a :class:`Grid`.

    Args:
        source: The workbook — a path, or an open binary stream. A stream because one caller reads
            an upload that is deliberately never stored: the client's sign-off sheet has no archive
            folder of its own, since where operator inputs are filed is an open question here and
            inventing a twelfth answer to it inside a reader would be the wrong place to decide.
        header_row: Given one row's cell texts, whether it is the header. A predicate rather than a
            column name because the two callers ask different questions: one knows the exact column
            configured for it, the other accepts any of several spellings of "EAN".

    Returns:
        The grid below the first recognised header row, or ``None`` when no sheet has one.

    Raises:
        OSError, zipfile.BadZipFile, xml.etree.ElementTree.ParseError: Deliberately unwrapped —
            see the module docstring.
    """
    with zipfile.ZipFile(source) as zf:
        shared = _read_shared_strings(zf)
        for sheet_path in _worksheet_paths(zf):
            rows = _read_sheet(zf, sheet_path, shared)
            found = _find_header(rows, header_row)
            if found is None:
                continue
            index, cells = found
            return _grid(rows, index, cells)
    return None


def _find_header(
    rows: list[dict[str, str]], header_row: Callable[[list[str]], bool]
) -> tuple[int, dict[str, str]] | None:
    """The first row ``header_row`` accepts, as ``(index, cells)``; ``None`` when there is none."""
    for index, cells in enumerate(rows):
        if header_row([text.strip() for text in cells.values()]):
            return index, cells
    return None


def _grid(rows: list[dict[str, str]], header_index: int, header_cells: dict[str, str]) -> Grid:
    """Assemble the rows below the header into a rectangle, in spreadsheet order.

    Columns are the **union** of the header row's letters and every data row's letters. The union is
    what keeps a column whose header cell is blank: it is still the operator's column, a save
    rewrites what this returns, and dropping it here would delete it from their file under a message
    saying the other columns were kept.
    """
    data = rows[header_index + 1 :]
    letters = sorted(
        {*header_cells, *(letter for cells in data for letter in cells)}, key=_column_number
    )
    header = [header_cells.get(letter, "").strip() for letter in letters]
    grid = [[cells.get(letter, "").strip() for letter in letters] for cells in data]
    return Grid(header, [row for row in grid if any(row)])


def _local(tag: str) -> str:
    """Return an XML tag's local name, dropping any ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Return the workbook's shared-string table (empty when absent)."""
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(t.text or "" for t in si.iter() if _local(t.tag) == "t")
        for si in root
        if _local(si.tag) == "si"
    ]


def _worksheet_paths(zf: zipfile.ZipFile) -> list[str]:
    """Return the archive paths of each worksheet, in workbook (sheet) order."""
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {rel.get("Id"): rel.get("Target") or "" for rel in rels}
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    paths: list[str] = []
    for el in workbook.iter():
        if _local(el.tag) != "sheet":
            continue
        rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
        target = rid_to_target.get(rid)
        if not target:
            continue
        normalised = target.lstrip("/")
        paths.append(normalised if normalised.startswith("xl/") else f"xl/{normalised}")
    return paths


def _col_letters(ref: str) -> str:
    """Return the column letters of a cell reference (``"C4"`` → ``"C"``)."""
    return "".join(ch for ch in ref if ch.isalpha())


def _column_number(letters: str) -> int:
    """Return a column letter's 1-based position (``"A"`` → 1, ``"Z"`` → 26, ``"AA"`` → 27).

    Sorting the letters as *strings* is the bug this exists to avoid — see the module docstring.
    """
    number = 0
    for char in letters:
        number = number * 26 + (ord(char.upper()) - ord("A") + 1)
    return number


def _read_sheet(zf: zipfile.ZipFile, path: str, shared: list[str]) -> list[dict[str, str]]:
    """Read a worksheet into a list of ``{column-letter: text}`` rows, in order."""
    root = ET.fromstring(zf.read(path))
    rows: list[dict[str, str]] = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells: dict[str, str] = {}
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            column = _col_letters(cell.get("r") or "")
            text = _cell_text(cell, shared)
            if column and text is not None:
                cells[column] = text
        rows.append(cells)
    return rows


def _cell_text(cell: ET.Element, shared: list[str]) -> str | None:
    """Return a cell's text, resolving shared and inline strings."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t")
    value = next((c for c in cell if _local(c.tag) == "v"), None)
    if value is None or value.text is None:
        return None
    if cell_type == "s":
        index = int(value.text)
        return shared[index] if 0 <= index < len(shared) else None
    return value.text
