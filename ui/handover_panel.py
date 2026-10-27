"""Setup → *Hand over to another machine*: make a handover pack, or receive one.

The same thing as ``python -m scripts.handover pack`` / ``unpack`` (:mod:`lib.handover`), on a
screen, because the person receiving the work is not expected to open a terminal. Receiving the
setup used to be six typed commands — a data-folder variable, a dry run, the unpack, an ``icacls``
for the passwords, the doctor — and each was a place a non-technical operator stalls.

Receiving reads the pack first and shows what it would change, written nowhere; only **Receive
this pack** writes. The refusal is the library's: a pack is never applied over a ledger that
records a publish the pack does not know about. The files it replaces are kept first, and
``.env`` is restricted to its owner (:mod:`lib.owner_only`).

Making one writes it in a temporary folder and hands a copy to Downloads, unopened: opening a zip
unpacks it, and with ``--with-credentials`` that would spread the passwords into Downloads.
"""

from __future__ import annotations

import platform
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nicegui import events, ui

from lib import handover as ledger_handover
from ui import DATA_ROOT, theme
from ui import handover as delivery

#: How many of the files a pack would write are listed before "and N more".
_LISTED = 12


def render(cid: str) -> None:
    with theme.section(
        "Hand over to another machine",
        anchor="handover",
        explain=(
            "Two people can publish for this client, never at the same time: only the machine "
            "holding the ledger (state.json, the record of what is published) publishes. A "
            "handover pack moves the settings, the batch files and the ledger from one machine "
            "to the other in one zip. Video files are not in it; copy those folders across "
            "separately."
        ),
    ):
        _receive()
        _make(cid)


def render_receive_only() -> None:
    """Receiving alone, for a machine whose ``clients.yml`` will not load: there is no client to
    make a pack for, but a pack is exactly what would give it a working one."""
    with theme.section("Receive a handover pack", anchor="handover"):
        _receive()


def _receive() -> None:
    theme.subhead("Receive a handover pack")
    ui.label(
        "Choose the zip you were sent. It is read first and nothing is written until you confirm. "
        "It is refused if this machine has published something the pack does not know about."
    ).classes("note")
    review = ui.column().classes("w-full mt-2")

    async def receive(event: events.UploadEventArguments) -> str:
        folder = Path(tempfile.mkdtemp(prefix="gs1-handover-"))
        pack = folder / "pack.zip"
        pack.write_bytes(await event.file.read())
        try:
            preview = ledger_handover.unpack(pack, DATA_ROOT, now=datetime.now(UTC), dry_run=True)
        except (ledger_handover.HandoverError, OSError, ValueError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            review.clear()
            raise ledger_handover.HandoverError(str(exc)) from exc
        _review(review, pack, preview)
        return "Read. Check what it would change below, then receive it."

    theme.upload("Handover pack (.zip)", receive, accept=".zip", busy="Reading the pack…")


def _review(area: ui.column, pack: Path, preview: ledger_handover.UnpackResult) -> None:
    manifest = preview.manifest
    area.clear()
    with area:
        ui.label(
            f"From {manifest.machine}, packed {manifest.packed_at[:16].replace('T', ' ')} UTC, "
            f"for {manifest.client_id}."
        ).classes("note")
        ui.label(
            "Last publish in its ledger: "
            + (manifest.ledger_latest[:16].replace("T", " ") if manifest.ledger_latest else "none")
        ).classes("note")
        ui.label(
            "It includes the passwords (.env)."
            if manifest.with_credentials
            else "It does not include passwords; this machine keeps its own."
        ).classes("note")
        if not preview.written:
            theme.band("Everything in this pack is already here. Nothing to do.", "quiet")
            shutil.rmtree(pack.parent, ignore_errors=True)
            return
        listed = list(preview.written[:_LISTED])
        more = len(preview.written) - len(listed)
        ui.label(
            f"It would write {len(preview.written)} file(s); {preview.unchanged} are already the "
            "same here. What it replaces is kept first."
        ).classes("note mt-2")
        ui.label("\n".join(listed) + (f"\n… and {more} more" if more > 0 else "")).classes(
            "mono note whitespace-pre-line"
        )

        def confirm() -> None:
            try:
                done = ledger_handover.unpack(pack, DATA_ROOT, now=datetime.now(UTC))
            except (ledger_handover.HandoverError, OSError, ValueError) as exc:
                theme.notify_problem(f"Not received: {exc}")
                return
            finally:
                shutil.rmtree(pack.parent, ignore_errors=True)
            area.clear()
            with area:
                theme.band(
                    f"Received: {len(done.written)} file(s) written. Next: copy the video "
                    "folders into place, restart the app, and open Runs → Does the site match the "
                    "ledger? before publishing from this machine.",
                    "quiet",
                )
            theme.notify_ok("Handover pack received.")

        theme.action("Receive this pack", confirm)


def _make(cid: str) -> None:
    theme.subhead("Make a handover pack")
    ui.label(
        "For the other machine, when the work moves there. After making it, do not publish from "
        "this machine until a pack comes back."
    ).classes("note")
    with_passwords: Any = ui.checkbox(
        "Include the passwords (.env): only for a first handover, sent privately"
    )

    def make() -> None:
        folder = Path(tempfile.mkdtemp(prefix="gs1-handover-"))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = folder / f"handover-{cid}-{stamp}.zip"
        try:
            manifest = ledger_handover.pack(
                DATA_ROOT,
                cid,
                out,
                with_credentials=bool(with_passwords.value),
                now=datetime.now(UTC),
                machine=platform.node() or "this machine",
            )
            delivery.give(out, open_it=False)
        except (ledger_handover.HandoverError, OSError) as exc:
            theme.notify_problem(f"No pack made: {exc}")
            return
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        said.text = (
            f"Made: {len(manifest.files)} file(s), saved to your Downloads folder. "
            + (
                "It holds the passwords: send it privately, never by email, and delete it once "
                "it has been received. "
                if manifest.with_credentials
                else ""
            )
            + "Video files are not in it."
        )

    theme.action("Make a handover pack", make)
    said = ui.label("").classes("note mt-2")
