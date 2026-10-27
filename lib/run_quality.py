"""The data-quality note a live run leaves in its own folder: what went live incomplete.

Today that is one thing — a page published with no video. Under ``media.publish_without_video`` a
product missing a video in some language publishes anyway, and the page looks finished: every
count on every screen counts pages, and a page without its video is still one page. So the run
says, in the folder that describes it, exactly which pages those are, for the client to supply a
video for.

**Read from what the run wrote, not from the mapping.** Each :class:`~lib.records.RunOutcome`
carries the ``video_file`` the page was given (``""`` for none), set at the moment of the write.
Re-deriving the list from ``mapping.yml`` afterwards would describe the mapping as it is *now*,
and a video confirmed between the run and the reading would vanish from a list of pages that
still do not have it.

Pure: outcomes in, Markdown out. :mod:`scripts.run_execute` writes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lib.records import RunOutcome


def without_video(outcomes: Iterable[RunOutcome]) -> list[RunOutcome]:
    """The rows this run published with no video, in run order.

    Only rows that wrote a page (``status == "ok"``) and track videos at all (``video_file`` is not
    ``None``) can be in it: a failed row published nothing, and a client that attaches no videos
    is not missing any.
    """
    return [o for o in outcomes if o.status == "ok" and o.video_file == ""]


def failed_video(outcomes: Iterable[RunOutcome]) -> list[RunOutcome]:
    """The rows whose confirmed video could not be prepared — absent from its folder, or refused."""
    return [o for o in outcomes if o.status == "ok" and o.video_failed]


def render(stamp: str, outcomes: Iterable[RunOutcome]) -> str:
    """The note for run ``stamp``, in Markdown."""
    rows = list(outcomes)
    bare = without_video(rows)
    written = sum(1 for o in rows if o.status == "ok" and o.video_file is not None)
    lines = [f"# Data quality — run {stamp}", "", "## Live without a video", ""]
    if not bare:
        lines.append(f"Every page this run wrote carries a video ({written} page(s)).")
    else:
        lines += _bare_lines(bare, written)
    failed = failed_video(rows)
    if failed:
        lines += [
            "",
            "## A confirmed video could not be prepared",
            "",
            "The mapping names a file for these, but it is missing from its folder or ffmpeg "
            "refused it, so the page kept whatever video it had. Fix the file; it is retried once "
            "the mapping names a different one, or with a re-publish.",
            "",
            "| Barcode | Language | File |",
            "|---|---|---|",
            *(f"| {o.gtin} | {o.language} | {o.video_failed} |" for o in failed),
        ]
    return "\n".join(lines) + "\n"


def _bare_lines(bare: list[RunOutcome], written: int) -> list[str]:
    lines: list[str] = []
    lines += [
        f"{len(bare)} of the {written} page(s) this run wrote went live with no video. Each needs "
        "a video from the client, confirmed in the mapping; the next run then adds it to the page.",
        "",
        "| Barcode | Language | Page |",
        "|---|---|---|",
    ]
    lines += [f"| {o.gtin} | {o.language} | {o.wp_url or ''} |" for o in bare]
    return lines
