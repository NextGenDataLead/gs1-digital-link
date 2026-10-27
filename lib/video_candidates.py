"""The video→GTIN candidate report: one row per (language, file), with what it might be.

``build_video_map --check`` answers "where are the gaps" and writes JSON. This answers the
question the *client* has to settle — "which product is this video of?" — as a grid they can work
in. A product is held out of every run until it has a confirmed video in each language
(``media.restrict_to_mapped_gtins``), so the mapping is the input that decides how much of a batch
can publish at all, and confirming a row is the client's call rather than the tool's.

Rows are the **union** of the mapping and the folders, so ``on disk, unmapped`` and ``mapped, file
missing`` are both visible; a mapping read against an absent video library would otherwise report
a clean file as a wall of missing ones. Scoring is not re-implemented here — every candidate comes
from :func:`lib.media_video.rank_candidates` over :func:`lib.media_video.normalize_video_name`, so
this report and the shell's suggestions cannot come to disagree about what matches.

Two things the columns exist to say, both of which cost a day to find out:

* **``product_name`` is the wrong name to show on its own.** Attribute 3301 is the short generic
  one — ``bezem``, ``siliconenbak``. What identifies a product to somebody looking at a video file
  is in the ``marketing_name`` / ``logistics_name`` extras: ``Democlient Afvoerreinigingsstick
  afbreekbaar geel``, ``Pipe Sticks 12pc``. All three are emitted, in every configured language.
* **The winning field is often a French one.** On the pilot feed 103 of 173 rows scored best
  against a ``.fr`` field, because the filenames are English and this feed's English sits in the
  French slots — ``logistics_name.fr`` is ``Pipe Sticks 12pc`` on one GTIN and ``Bâton`` on
  another. So the value that scored and the field it came from are columns of their own: without
  them a 0.93 beside an unrecognisable Dutch name reads as a bug rather than as the answer.

Pure and deterministic — no filesystem, no config, no clock. ``scripts/report_video_candidates.py``
does the I/O and chooses the file format.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from lib.media_video import (
    CONFIRMED,
    VideoCandidate,
    VideoMap,
    VideoMapEntry,
    canon_gtin,
    normalize_video_name,
    rank_candidates,
    state_of,
)
from lib.records import ProductRecord

#: A row whose file is on disk but which the mapping does not list. Upper-cased like
#: :data:`NOT_ON_DISK` because neither is a state the client can resolve by filling in a GTIN —
#: they are disagreements between the file and the folder, and a different job.
NOT_IN_MAPPING: Final = "NOT IN MAPPING"
#: A row the mapping lists whose file is not in the folder.
NOT_ON_DISK: Final = "NOT ON DISK"

#: The names shown for an already-mapped GTIN, in the order they identify a product: the feed's
#: own short name first, then the two that actually carry the marketing English.
NAME_FIELDS: Final = ("product_name", "marketing_name", "logistics_name", "functional_name")

#: Columns emitted per candidate — see the module docstring on why the last two are not optional.
_PER_CANDIDATE: Final = ("gtin", "score", "value", "field")

#: A spreadsheet cell. Scores stay numeric so the client can sort by them; everything else is text
#: (a GTIN with a leading zero is not a number, and Excel will eat one that is).
Cell = str | float


@dataclass(frozen=True)
class CandidateRow:
    """One video file in one language: what it maps to today, and what it might be.

    Attributes:
        language: The language folder / mapping block the file belongs to.
        file: The filename, verbatim.
        normalized: What :func:`lib.media_video.normalize_video_name` reduced it to — the token
            the scores below were actually computed against, so an odd score can be explained
            rather than doubted.
        state: ``confirmed`` / ``unset`` / ``skip``, or :data:`NOT_IN_MAPPING` /
            :data:`NOT_ON_DISK`.
        gtin: The GTIN(s) the mapping currently carries, verbatim and possibly blank — several,
            joined by ``", "``, for a video shared by several products.
        names: ``{"{field}.{lang}": value}`` for the mapped product, empty when nothing is mapped
            — or when the mapped GTIN is not in the feed at all, which is itself worth seeing.
        candidates: The ranked hints, best first. Shorter than ``top_n`` only when the feed holds
            fewer products than that.
    """

    language: str
    file: str
    normalized: str
    state: str
    gtin: str
    names: Mapping[str, str]
    candidates: tuple[VideoCandidate, ...]


def build_rows(
    vmap: VideoMap,
    files_by_language: Mapping[str, Sequence[str]],
    products: Sequence[ProductRecord],
    languages: Sequence[str],
    *,
    top_n: int,
) -> list[CandidateRow]:
    """Build the report: every mapping row, plus every file on disk the mapping does not list.

    Args:
        vmap: The client-confirmed mapping.
        files_by_language: Filenames found in each configured video folder.
        products: The parsed feed — the candidate pool, unrestricted by the process list, because
            the point of the report is to find which product a video is.
        languages: The client's configured languages, which fix the column and row order. Any
            language present only in the mapping or only on disk follows, sorted, rather than
            being dropped.
        top_n: How many ranked candidates to offer per row.

    Returns:
        The rows, ordered by language then filename.
    """
    feed = _Feed(list(products), {p.gtin14: p for p in products}, top_n)
    rows: list[CandidateRow] = []
    for language in _languages(vmap, files_by_language, languages):
        on_disk = set(files_by_language.get(language, ()))
        entries = vmap.by_language.get(language, [])
        for file, cell in _cells_by_file(entries).items():
            state = state_of(cell) if file in on_disk else NOT_ON_DISK
            rows.append(_row(feed, language, file, state, cell))
        listed = {entry.file for entry in entries}
        for name in sorted(on_disk - listed):
            rows.append(_row(feed, language, name, NOT_IN_MAPPING, ""))
    return rows


@dataclass(frozen=True)
class _Feed:
    """What every row is built against, carried once instead of threaded through each call."""

    products: list[ProductRecord]
    by_gtin: Mapping[str, ProductRecord]
    top_n: int


def _cells_by_file(entries: Sequence[VideoMapEntry]) -> dict[str, str]:
    """One barcode cell per video, in mapping order — several barcodes joined by ``", "``.

    A video shared by several products is one row per barcode in the mapping, and one row with a
    comma-separated cell in the sheet, which is the form :mod:`lib.video_signoff` reads back. The
    barcodes stay as written, so a sheet sent out and returned untouched changes nothing. ``skip``
    or blank shows only for a video that carries no barcode at all.
    """
    confirmed: dict[str, dict[str, str]] = {}
    first: dict[str, str] = {}
    for entry in entries:
        first.setdefault(entry.file, entry.gtin)
        written = confirmed.setdefault(entry.file, {})
        if state_of(entry.gtin) == CONFIRMED:
            written.setdefault(canon_gtin(entry.gtin), entry.gtin.strip())
    return {
        file: ", ".join(written.values()) if written else first[file]
        for file, written in confirmed.items()
    }


def _row(feed: _Feed, language: str, filename: str, state: str, gtin: str) -> CandidateRow:
    normalized = normalize_video_name(filename)
    mapped = (
        [feed.by_gtin.get(canon_gtin(one)) for one in gtin.split(",")]
        if state_of(gtin) == CONFIRMED
        else []
    )
    return CandidateRow(
        language=language,
        file=filename,
        normalized=normalized,
        state=state,
        gtin=gtin,
        names=_merged_names(mapped),
        candidates=tuple(rank_candidates(normalized, feed.products, top_n=feed.top_n)),
    )


def _merged_names(products: Sequence[ProductRecord | None]) -> dict[str, str]:
    """Every mapped product's names, one product's after another's with ``" / "`` between."""
    merged: dict[str, list[str]] = {}
    for product in products:
        for key, value in _names(product).items():
            merged.setdefault(key, []).append(value)
    return {key: " / ".join(values) for key, values in merged.items()}


def _names(product: ProductRecord | None) -> dict[str, str]:
    """Every configured name of the mapped product, keyed ``{field}.{lang}``.

    Read through :attr:`~lib.records.ProductRecord.product_name` and
    :meth:`~lib.records.ProductRecord.extra`, which is the same path the page templates take —
    including its fallback from ``extras_localised`` to a flat ``extras`` value, so a
    ``products.json`` written before that field existed still shows its names.
    """
    if product is None:
        return {}
    names = {f"product_name.{lang}": value for lang, value in product.product_name.values.items()}
    for field in NAME_FIELDS[1:]:
        for lang in _extra_languages(product, field):
            names[f"{field}.{lang}"] = product.extra(field, lang) or ""
    return names


def _extra_languages(product: ProductRecord, field: str) -> list[str]:
    """The languages this record carries ``field`` in — every one, or a single flat value's."""
    localised = product.extras_localised.get(field)
    if localised is not None:
        return list(localised.values)
    return [""] if field in product.extras else []


def header(languages: Sequence[str], top_n: int) -> list[str]:
    """The column names, in the order :func:`cells` emits their values."""
    columns = ["language", "file", "normalized", "state", "gtin"]
    columns += [f"mapped_{field}.{lang}" for field in NAME_FIELDS for lang in languages]
    for n in range(1, top_n + 1):
        columns += [f"candidate_{n}_{part}" for part in _PER_CANDIDATE]
    return columns


def cells(row: CandidateRow, languages: Sequence[str], top_n: int) -> list[Cell]:
    """One row's values, padded to the full width so short candidate lists do not shift columns."""
    values: list[Cell] = [row.language, row.file, row.normalized, row.state, row.gtin]
    for field in NAME_FIELDS:
        values += [_name(row, field, lang) for lang in languages]
    for n in range(top_n):
        if n < len(row.candidates):
            hit = row.candidates[n]
            values += [hit.gtin, round(hit.score, 2), hit.name, hit.field]
        else:
            values += ["", "", "", ""]
    return values


def _name(row: CandidateRow, field: str, language: str) -> str:
    """A mapped name in one language, falling back to a value the record carries flat.

    The flat form is keyed on the empty language because that is what the record knows: whether a
    name is per-language is a fact about the feed row, not about the config, and a record written
    before ``extras_localised`` existed holds one string for all of them.
    """
    return row.names.get(f"{field}.{language}") or row.names.get(f"{field}.", "")


def _languages(
    vmap: VideoMap, files_by_language: Mapping[str, Sequence[str]], configured: Sequence[str]
) -> list[str]:
    """Configured languages in their configured order, then any other language that turned up."""
    extra = (set(vmap.by_language) | set(files_by_language)) - set(configured)
    return [*configured, *sorted(extra)]
