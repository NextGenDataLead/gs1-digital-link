"""Write the synthetic GS1 Data Source export the example client never had.

Usage:
    python -m scripts.make_demo_export [CLIENT_ID] [--config PATH] [--dry-run] [--force]

Builds a GDSN datapool workbook and a product scope list from :mod:`lib.demo_export` and writes
each to the path that client's config declares — ``export.path`` and ``process_list.path``. With a
``media`` block it also writes the videos from :mod:`lib.demo_videos`: a placeholder file per
mapping row into ``media.video_folders``, ``mapping.yml`` at ``media.video_map_path``, and the
client's sign-off sheet under ``test-uploads/``, where an operator rehearsing the Data screen picks
it up — so every video state the screen and the report distinguish can be rehearsed without a real
client. The
paths come from the config rather than from a flag because that is the only place the rest of the
tool will look: an export written anywhere else is invisible to it, which is the single most
common way a run quietly uses the wrong data.

**Why this exists.** The operator shell's Data screen needs both an export and a scope list before
it renders anything, and ``democlient`` had no export that ``parse_export`` could read. So every
rehearsal of that screen was driven against the live client — and uploading through the picker
replaces the control file in place, which corrupted a real operator's scope list three times.
This command is what makes a throwaway client a real option.

Defaults to ``clients.example.yml``, which is where ``democlient`` is defined; the operator's own
``clients.yml`` is gitignored and holds their real client, not this one.

**It will not overwrite an existing file without ``--force``.** The damage this command could do
is to write demo products over a client's real export, and the only reliable sign of that target
is that something is already there.

Exit codes:
    0  written (or, with --dry-run, validated)
    1  refused — a target file exists and --force was not given
    2  config errors (bad client id, unreadable config, no export/process_list path)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import openpyxl

from lib import demo_export, demo_videos
from lib.config import ClientConfig, get_client
from lib.data_dir import CODE_ROOT, enter_data_dir
from lib.env import load_env
from lib.errors import ConfigError
from lib.input_layout import client_root

_log = logging.getLogger("scripts.make_demo_export")

#: Where ``democlient`` is defined. The operator's ``clients.yml`` is gitignored and holds theirs.
#: It ships with the code, so it is found there even when the data folder is elsewhere; the paths
#: it names are relative, so the files still land in the data folder.
_DEFAULT_CONFIG = str(CODE_ROOT / "clients.example.yml")

_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_CONFIG_ERROR = 2


def _write_workbook(path: Path, grids: dict[str, list[list[object]]]) -> None:
    """Write one worksheet per module, in the order a real export lists them."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    try:
        for name, rows in grids.items():
            sheet = workbook.create_sheet(name)
            for row in rows:
                sheet.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(path)
    finally:
        workbook.close()


def _targets(client: ClientConfig) -> list[tuple[Path, str]]:
    """The files this command writes, as (path, what it is), straight from the config."""
    targets = [(Path(client.export.path), "GS1 Data Source export")]
    if client.process_list is not None:
        targets.append((Path(client.process_list.path), "product scope list"))
    return targets + _video_targets(client)


#: Where the demo sign-off sheet goes, under the client folder: the folder ``test-uploads/`` that
#: the README in it says is "kept on purpose so there is always something safe to practise with".
_SIGNOFF_SHEET = Path("test-uploads") / "video-signoff.xlsx"


def _video_targets(client: ClientConfig) -> list[tuple[Path, str]]:
    """The mapping, the sign-off sheet and every placeholder video — empty with no media block."""
    media = client.media
    if media is None or not media.video_map_path:
        return []
    targets = [(Path(media.video_map_path), "video mapping")]
    root = client_root(Path(client.export.path))
    if root is not None:
        targets.append((root / _SIGNOFF_SHEET, "video sign-off sheet"))
    for language, names in demo_videos.files().items():
        folder = media.video_folders.get(language)
        if folder is not None:
            targets += [(Path(folder) / name, f"{language} video") for name in names]
    return targets


def _write_videos(targets: list[tuple[Path, str]]) -> None:
    """Write each video target by what it is. Only reached once nothing refused."""
    for path, what in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        if what == "video mapping":
            path.write_text(demo_videos.mapping_text(), encoding="utf-8")
        elif what == "video sign-off sheet":
            _write_workbook(path, {"Sign-off": demo_videos.signoff_rows()})
        else:
            path.write_bytes(demo_videos.PLACEHOLDER)


def _refusals(targets: list[tuple[Path, str]], force: bool) -> list[str]:
    """Which targets already exist. Empty when ``force`` is set — the operator said so."""
    if force:
        return []
    return [f"{path} already exists ({what})" for path, what in targets if path.exists()]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="make_demo_export",
        description="Write a synthetic GDSN export and scope list for a demo client.",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        help="Key under clients: in the config (optional when only one client is defined)",
    )
    parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help="config file to read the output paths from (default: the shipped clients.example.yml)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; write no file")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files. Without it, an existing target refuses the whole run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        client = get_client(args.client_id, args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    targets = _targets(client)
    # Refusal is checked before anything is written, and names every clash rather than the first:
    # finding out about the second file only after re-running with --force is how a real export
    # gets overwritten by someone who thought they had already looked.
    refusals = _refusals(targets, args.force)
    if refusals and not args.dry_run:
        for refusal in refusals:
            print(f"refusing: {refusal}", file=sys.stderr)
        print("nothing written — pass --force to overwrite", file=sys.stderr)
        return _EXIT_REFUSED

    grids = demo_export.sheet_grids()
    scope = demo_export.process_list_rows()
    export_path, _ = targets[0]
    videos = _video_targets(client)
    if not args.dry_run:
        _write_workbook(export_path, grids)
        if client.process_list is not None:
            _write_workbook(Path(client.process_list.path), {"Scope": scope})
        _write_videos(videos)

    verb = "Would write" if args.dry_run else "Wrote"
    print(
        f"{verb} {len(grids)} sheets and {len(demo_export.CATALOGUE)} products to {export_path}",
        file=sys.stderr,
    )
    if client.process_list is not None:
        print(f"{verb} {len(scope) - 1} barcodes to {client.process_list.path}", file=sys.stderr)
    if videos:
        print(
            f"{verb} a video mapping, a sign-off sheet and "
            f"{sum(len(n) for n in demo_videos.files().values())} placeholder videos",
            file=sys.stderr,
        )
    for refusal in refusals:
        print(f"note: {refusal} — a real run would refuse", file=sys.stderr)
    return _EXIT_OK


if __name__ == "__main__":
    enter_data_dir()
    load_env()
    raise SystemExit(main())
