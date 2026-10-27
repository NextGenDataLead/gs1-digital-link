"""Parse a client's product export into ``products.json`` (IMPLEMENTATION_SPEC §8.1).

Usage:
    python -m scripts.parse_export CLIENT_ID [--dry-run] [--output PATH]

Reads the client's export (GS1 Data Source / GDSN datapool, or a flat single sheet),
normalises every product into a :class:`~lib.records.ProductRecord`, and writes
``output/{client_id}/data/products.json``.

Exit codes:
    0  success
    1  parse errors (no output written)
    2  config errors (bad client id, unreadable/absent export)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from lib import gdsn
from lib.config import ExportConfig, get_client
from lib.data_dir import enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError, ExportParseError
from lib.records import ProductRecord, SourceIssue, _coerce_cell, parse_excel_row

_log = logging.getLogger("scripts.parse_export")

_EXIT_OK = 0
_EXIT_PARSE_ERROR = 1
_EXIT_CONFIG_ERROR = 2


class _ParseOutput:
    """Accumulated records plus non-fatal warnings, fatal errors, and source-data issues."""

    def __init__(
        self,
        records: list[ProductRecord],
        warnings: list[str],
        errors: list[str],
        issues: list[SourceIssue] | None = None,
    ) -> None:
        self.records = records
        self.warnings = warnings
        self.errors = errors
        self.issues = issues or []


def _run_gdsn(export: ExportConfig, languages: list[str], default_language: str) -> _ParseOutput:
    workbook = gdsn.read_workbook(export.path)
    result = gdsn.build_records(
        workbook,
        export.gdsn_map,
        export.market_priority,
        languages,
        default_language,
        export.gdsn_extras,
    )
    return _ParseOutput(result.records, result.warnings, result.errors, result.issues)


def _run_flat(export: ExportConfig, default_language: str) -> _ParseOutput:
    workbook = openpyxl.load_workbook(export.path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        row_iter = sheet.iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(row_iter, ())]
        header_set = {h for h in header if h}
        column_map = export.column_map
        required = {"gtin", "brand", f"product_name.{default_language}"}

        unmapped_required = [t for t in required if t not in column_map.values()]
        if unmapped_required:
            raise ConfigError(f"required target(s) not in column_map: {sorted(unmapped_required)}")

        warnings, errors = _validate_flat_header(export, header, header_set, required)
        records, row_warnings, row_errors = _read_flat_rows(
            row_iter, header, export, default_language
        )
        return _ParseOutput(records, [*warnings, *row_warnings], [*errors, *row_errors])
    finally:
        workbook.close()


def _validate_flat_header(
    export: ExportConfig, header: list[str], header_set: set[str], required: set[str]
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    for target in required:  # E17 (required): mapped but column absent from sheet
        columns = [c for c, t in export.column_map.items() if t == target]
        if not any(c in header_set for c in columns):
            errors.append(f"required target {target!r} column(s) {columns} absent from sheet")
    for column in header:  # E16: present but unmapped
        if column and column not in export.column_map and column not in export.extras_columns:
            warnings.append(f"unmapped column {column!r}")
    for column in [*export.column_map, *export.extras_columns]:  # E17 (optional)
        if column not in header_set and export.column_map.get(column) not in required:
            warnings.append(f"mapped column {column!r} absent from sheet")
    return warnings, errors


def _read_flat_rows(
    row_iter: object,
    header: list[str],
    export: ExportConfig,
    default_language: str,
) -> tuple[list[ProductRecord], list[str], list[str]]:
    seen: set[str] = set()
    records: list[ProductRecord] = []
    warnings: list[str] = []
    errors: list[str] = []
    for raw in row_iter:  # type: ignore[attr-defined]
        if all(_coerce_cell(cell) is None for cell in raw):
            continue  # E4: empty row skipped silently
        row = dict(zip(header, raw, strict=False))
        try:
            record = parse_excel_row(
                row, export.column_map, export.extras_columns, default_language
            )
        except ExportParseError as exc:
            errors.append(str(exc))
            continue
        if record.gtin in seen:  # E3 (flat): first occurrence wins
            warnings.append(f"duplicate GTIN {record.gtin} skipped")
            continue
        seen.add(record.gtin)
        records.append(record)
    return records, warnings, errors


def _write_output(path: Path, records: list[ProductRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.model_dump(mode="json") for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_issues(path: Path, issues: list[SourceIssue]) -> None:
    """Write the source-data issue report — **always**, even when empty.

    Written unconditionally so the file's meaning is unambiguous: an empty list means "this
    run found nothing", while a missing file means "no run has looked". A report that only
    appears when there is bad news is one you cannot trust the absence of.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [issue.model_dump(mode="json") for issue in issues]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="parse_export", description="Parse a client export.")
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in clients.yml (optional when only one client is defined)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate only; write no file")
    parser.add_argument("--output", help="Override output/{client_id}/data/products.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        client = get_client(args.client_id)
        export = client.export
        default_language = client.wordpress.default_language
        result = (
            _run_gdsn(export, client.wordpress.languages, default_language)
            if export.format == "gdsn"
            else _run_flat(export, default_language)
        )
    except (ConfigError, FileNotFoundError, InvalidFileException) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    for warning in result.warnings:
        _log.warning("%s", warning)
    if result.errors:
        for error in result.errors:
            _log.error("%s", error)
        print(f"{len(result.errors)} parse errors", file=sys.stderr)
        return _EXIT_PARSE_ERROR

    issues_path = Path(f"output/{client.client_id}/data/source_issues.json")
    if not args.dry_run:
        output = Path(args.output or f"output/{client.client_id}/data/products.json")
        _write_output(output, result.records)
        _write_issues(issues_path, result.issues)
    print(
        f"Parsed {len(result.records)} products ({len(result.warnings)} warnings)",
        file=sys.stderr,
    )
    if result.issues and not args.dry_run:
        # Named explicitly: these are defects in the *source datapool* that a person must
        # fix in MyGS1, and they outlive this run. A count in a scrolling log is not a
        # work queue.
        print(
            f"{len(result.issues)} source-data issue(s) need fixing at the source "
            f"(MyGS1) — see {issues_path}",
            file=sys.stderr,
        )
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
