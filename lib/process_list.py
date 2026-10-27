"""Process list: the operator's explicit list of which GTINs a run may touch.

Loads the operator-maintained control file — ``input/{client_id}/process/selection/
selections.xlsx``, at whatever path ``process_list.path`` names — and
returns the set of GTINs in it. **Every GTIN in the file is processed.** There is no
eligibility logic here and no interpretation of cell *values* — the file is a list, and
being on it is the whole meaning.

That is deliberate, and it replaced a version that read "already on website" and "already
in GS1" columns with presence-semantics (any non-blank cell meant ``True``). The old
behaviour was correct only for files that mark rows with ``X``: a client whose file said
``no`` got the opposite of what the word meant, silently, because ``"no"`` is non-blank.
It failed in both directions — a wrong "on website" emptied the plan and the run reported
success having published nothing, while a wrong "in GS1" marked a product eligible and
pointed the pipeline at a GTIN with no resolver record. Neither raised anything.

So the judgement moved to the person who has it. **The operator prepares the file by
deleting every row that should not be processed**, by whatever rule their business uses.
The tool no longer guesses what a column means, because it no longer reads one.

The file is read by :mod:`lib.xlsx` rather than ``openpyxl``, for reasons that are all
properties of the files real clients send — Strict Open XML, a table that does not start at
A1, a sheet that is not the first one. That reader lived here until the client's video
sign-off sheet needed the same treatment; its docstring carries the full account. What stays
here is what is specific to *this* file: the header is found by the configured GTIN column,
and GTINs are normalised to 14 digits so a 13-digit barcode joins to a 14-digit
:attr:`lib.records.ProductRecord.gtin14`.

There is one reader, not two. :func:`read_process_list` returns the whole table as a
:class:`ProcessListSheet` — the shape an editing surface needs — and
:func:`load_process_list` is that call plus ``listed_gtins()``. The shell used to carry its
own openpyxl reader, which meant a Strict-OOXML file with a title row loaded in a run and
failed on screen; the two could disagree about the same file, and did.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from lib import xlsx
from lib.errors import ProcessListError

if TYPE_CHECKING:
    from lib.config import ProcessListConfig

_log = logging.getLogger(__name__)

_GTIN14_WIDTH = 14


@dataclass(frozen=True)
class ProcessListSheet:
    """The control file as a grid: its header row, its data rows, and which column holds the GTIN.

    ``rows`` are cell texts, because this is a display and edit surface — the *meaning* of a
    GTIN (13 vs 14 digits, leading zeros) is settled by :meth:`gtin14_at` via the one
    normalisation in this module, and re-deciding it anywhere else would create a second
    opinion about the same value.

    Every column the file carries is present, in spreadsheet order, including columns with a
    blank header cell: a save rewrites the grid, so a column dropped here is a column dropped
    from the operator's file.
    """

    path: Path
    header: list[str]
    rows: list[list[str]]
    gtin_index: int

    def without(self, indices: set[int]) -> ProcessListSheet:
        """A copy with the given row positions removed. Immutable, like everything else here.

        Positions are **into this sheet**, so the result is renumbered. Applying it twice with
        positions taken from the original sheet removes the wrong rows the second time — see
        :meth:`keeping`, which is what an editing surface wants.
        """
        kept = [row for n, row in enumerate(self.rows) if n not in indices]
        return ProcessListSheet(self.path, self.header, kept, self.gtin_index)

    def keeping(self, indices: set[int]) -> ProcessListSheet:
        """A copy holding only these row positions, in their original order.

        The counterpart to :meth:`without`, and the one a grid should use. A grid identifies a row
        by a key fixed when it was built; this sheet renumbers on every edit. Feeding those fixed
        keys back into a renumbered sheet is silently wrong from the second edit onward, and the
        symptom is the worst kind: the screen shows one set of rows and the file receives another,
        with a success message either way. Deriving the sheet from the surviving keys against the
        *original* cannot drift, because nothing accumulates.
        """
        kept = [row for n, row in enumerate(self.rows) if n in indices]
        return ProcessListSheet(self.path, self.header, kept, self.gtin_index)

    def gtin14_at(self, index: int) -> str | None:
        """The GTIN-14 of the row at this position, or ``None`` when its barcode cell is blank."""
        row = self.rows[index]
        if not 0 <= self.gtin_index < len(row):
            return None
        return _coerce_gtin(row[self.gtin_index])

    def listed_gtins(self) -> frozenset[str]:
        """Every GTIN in the sheet, normalised to 14 digits. Duplicates collapse."""
        return frozenset(
            gtin for n in range(len(self.rows)) if (gtin := self.gtin14_at(n)) is not None
        )


def rows_in_export(
    sheet: ProcessListSheet, exported: Collection[str]
) -> tuple[list[int], list[int]]:
    """Split the sheet's row positions by whether the export carries that GTIN.

    Returns ``(matched, unmatched)``, both in sheet order. A row with a blank barcode is
    unmatched — it is on the list and the export has nothing for it, which is the same fact.

    ``exported`` must be GTIN-14s: ``{product.gtin14 for product in products}``, which is exactly
    the pair :func:`lib.preflight.in_scope` joins on. That is the whole reason this is a function
    rather than two lines on a screen. :func:`lib.preflight.check_scope` deliberately emits
    ``ProductRecord.gtin`` and *not* ``gtin14``, because a normalised variant there would silently
    fail to match for any client whose feed carries 13-digit codes; a third normalisation invented
    at a call site would report every good product as missing, and look like bad data rather than
    like a bug.

    Nothing else in the codebase computes this set. A barcode that is on the list and absent from
    the export is invisible today: it produces no error, no plan row and no count, and the
    operator's only evidence is a number that is one smaller than they expected.
    """
    matched: list[int] = []
    unmatched: list[int] = []
    for index in range(len(sheet.rows)):
        gtin = sheet.gtin14_at(index)
        (matched if gtin is not None and gtin in exported else unmatched).append(index)
    return matched, unmatched


def _is_filled(value: object) -> bool:
    """Return whether a spreadsheet cell holds a non-blank value."""
    return value is not None and str(value).strip() != ""


def _coerce_gtin(value: object) -> str | None:
    """Coerce a barcode cell to a GTIN-14 digit string, or ``None`` when blank.

    A 13-digit barcode is zero-padded to 14 digits so it joins to a
    :attr:`lib.records.ProductRecord.gtin14`; whole-number floats (``…905.0``) and ints
    are rendered without a decimal point first.
    """
    if not _is_filled(value):
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a GTIN
        return None
    if isinstance(value, float) and value.is_integer():
        digits = str(int(value))
    elif isinstance(value, int):
        digits = str(value)
    else:
        digits = str(value).strip()
    return digits.zfill(_GTIN14_WIDTH)


def read_process_list(config: ProcessListConfig) -> ProcessListSheet:
    """Read the control file as a grid: header, rows, and the position of the GTIN column.

    Args:
        config: The client's ``process_list`` configuration (path + GTIN column name).

    Returns:
        The whole table below the first row carrying the configured GTIN column, with every
        column the file holds.

    Raises:
        ProcessListError: If the file cannot be opened, or if no worksheet contains the
            configured GTIN column. Deliberately **not** raised for a sheet that yields no
            GTINs at all: an empty grid is displayable and fixable, and refusing it belongs
            to the two callers that would act on it — :func:`load_process_list`, which would
            plan nothing, and ``ui.process_list_edit.save_sheet``, which would write it.
    """
    path = Path(config.path)
    try:
        # Exact, not normalised: the column is named in clients.yml, where a mismatch is the
        # operator's to fix and a silent near-match would hide it.
        grid = xlsx.read_grid(path, header_row=lambda texts: config.gtin_column in texts)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ProcessListError(f"cannot read process list at {config.path}: {exc}") from exc

    if grid is None:
        raise ProcessListError(
            f"process list at {config.path} has no sheet with a {config.gtin_column!r} column"
        )
    return ProcessListSheet(path, grid.header, grid.rows, grid.header.index(config.gtin_column))


def load_process_list(config: ProcessListConfig) -> frozenset[str]:
    """Load the process list, returning every listed GTIN normalised to 14 digits.

    Args:
        config: The client's ``process_list`` configuration (path + GTIN column name).

    Returns:
        The GTIN-14s to process. Duplicate rows collapse; a row with a blank barcode is
        skipped.

    Raises:
        ProcessListError: If the file cannot be opened, if no worksheet contains the
            configured GTIN column, or if the file yields **no** GTINs at all. That last
            case is a structural check rather than an interpretation of values: a file
            that parses to an empty list would otherwise produce an empty plan and a run
            that reports success having published nothing.
    """
    gtins = read_process_list(config).listed_gtins()
    if not gtins:
        raise ProcessListError(
            f"process list at {config.path} has a {config.gtin_column!r} column "
            f"but no GTINs under it — nothing would be processed. Check that the "
            f"rows sit below the header and that the barcodes are not blank."
        )
    _log.info("Loaded %d GTIN(s) to process from %s", len(gtins), config.path)
    return gtins


def listed_urls(sheet: ProcessListSheet, column: str | None) -> dict[str, str]:
    """``{gtin14: address}`` from the *Link naar site* column of a sheet on screen, unvalidated.

    The Data screen's read, for a list that has just been uploaded and is not saved yet: every
    filled cell, the first one per GTIN, with nothing refused — the screen says what is wrong with
    each address itself (:mod:`lib.link_targets`). :func:`load_listed_targets` is the run's read,
    which refuses the whole file instead.
    """
    if column is None or column not in sheet.header:
        return {}
    index = sheet.header.index(column)
    found: dict[str, str] = {}
    for n, row in enumerate(sheet.rows):
        gtin = sheet.gtin14_at(n)
        url = row[index].strip() if index < len(row) else ""
        if gtin is not None and url:
            found.setdefault(gtin, url)
    return found


def load_listed_targets(config: ProcessListConfig, site_url: str) -> dict[str, str]:
    """Read the page address the operator listed per GTIN, keyed by GTIN-14.

    The one value this module interprets, and only when ``target_url_column`` names it — see
    :class:`~lib.config.ProcessListConfig` for why. A blank cell is simply absent from the
    result, so a list where nobody filled the column behaves exactly as it did before.

    Everything that can be wrong with a cell is refused here, for the whole file at once,
    rather than per GTIN at run time: the address ends up as the target of a GS1 record that
    can never be deleted, and a plan that quietly dropped a bad row would read as a product
    nobody asked about.

    Args:
        config: The client's ``process_list`` configuration.
        site_url: ``wordpress.site_url``. A listed page must be on the same host — the tool
            points GS1 at the client's own site, never at an address someone pasted from
            elsewhere.

    Returns:
        ``{gtin14: url}`` for every row whose barcode and address are both filled.

    Raises:
        ProcessListError: If the configured column is missing from the file, a cell is not an
            absolute ``http(s)`` address on the site's host, or one GTIN is listed with two
            different addresses.
    """
    column = config.target_url_column
    if column is None:
        return {}
    sheet = read_process_list(config)
    if column not in sheet.header:
        raise ProcessListError(
            f"process list at {config.path} has no {column!r} column, which "
            f"process_list.target_url_column names — rename the column or the setting"
        )
    index = sheet.header.index(column)
    site_host = (urlsplit(site_url).hostname or "").lower()
    targets: dict[str, str] = {}
    problems: list[str] = []
    for n, row in enumerate(sheet.rows):
        gtin = sheet.gtin14_at(n)
        url = row[index].strip() if index < len(row) else ""
        if gtin is None or not url:
            continue
        problem = _target_problem(url, site_host)
        if problem is None and targets.get(gtin, url) != url:
            problem = f"also listed as {targets[gtin]}"
        if problem is not None:
            problems.append(f"{gtin}: {url!r} — {problem}")
            continue
        targets[gtin] = url
    if problems:
        raise ProcessListError(
            f"process list at {config.path}: {len(problems)} unusable {column!r} value(s): "
            + "; ".join(problems)
        )
    return targets


def _target_problem(url: str, site_host: str) -> str | None:
    """Why ``url`` cannot be a resolver target on ``site_host``, or ``None`` when it can."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "not an absolute http(s) address"
    if parts.hostname.lower() != site_host:
        return f"not on the site's host ({site_host})"
    return None
