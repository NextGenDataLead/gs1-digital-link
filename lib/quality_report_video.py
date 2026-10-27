"""§1 of the data-quality report: which selected products are waiting on a video, and on what.

**Why it is early.** A product held for want of a video is not published at all — in any language,
with a run that reports success — and on the pilot that is 48 of 110. It was spread across three
places that never met: §0 showed a ○ per product, the Summary counted the hold, and a "backlog"
section near the end listed filenames. "118 video files have no GTIN" and "40 GTINs have no
confirmed video" sat in one document and left the reader to notice the numbers do not meet in the
middle — because they cannot. A file with no barcode names no product.

**So the section has four parts, and says which side each is on.**

* **1a** is the product side: one row per held product and what it is waiting on. Unbounded,
  because each row is a job the client can do.
* **1b** is the products with two videos confirmed in one language — the tool attaches neither.
* **1c** is the file side: videos with no barcode yet. Sampled, because a filename that names no
  product is a status line, and ``mapping.yml`` is where the whole list lives. That asymmetry with
  1a is deliberate and the section says so.
* **1d** is the client's sign-off sheet, re-planned against the mapping on every render, so what
  it still says is always current. It is the one thing that joins the two sides.

Pure: no config, no I/O, no clock. :mod:`scripts.report_quality` gathers the inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, NamedTuple

from lib.mandatory import MandatoryGap
from lib.media_video import VideoCandidate
from lib.records import SourceIssue
from lib.report_markdown import cell, table
from lib.video_signoff import (
    AMBIGUOUS,
    BLANK,
    CHANGE,
    REJECTED,
    UNCHANGED,
    SignoffPlan,
    SignoffRow,
    describe,
)
from lib.video_status import (
    CLASHING,
    HAS_VIDEO,
    UnassignedVideo,
    VideoStatus,
    waiting_on,
)

#: The heading's words. Not "Blocks publish — no confirmed video": two of the four parts block
#: nothing, and a heading that overstates is the one a reader stops believing.
TITLE: Final = "Video — which products are waiting, and on what"

#: How many filenames 1c names before falling back to a count. ``mapping.yml`` is both the
#: authoritative list and where the work is done, so the report shows enough to recognise what is
#: outstanding rather than reproducing the file. (Sized when the report said 118 — a figure from a
#: ``video_map_issues.json`` seven weeks stale; the mapping then had 18.)
VIDEO_SAMPLE: Final = 10

_MARK: Final = {HAS_VIDEO: "●", CLASHING: "2"}
_NONE: Final = "○"

#: What the report says when no sheet has been re-planned. The ordinary state before the client
#: replies, so it is a sentence, never a warning.
NO_SHEET: Final = "No sign-off sheet from the client yet — nothing to re-plan."


class SignoffReview(NamedTuple):
    """The client's newest sign-off sheet, re-planned against the mapping as it stands now.

    Attributes:
        sheet: The archived file's name — where the operator finds it.
        given_name: What the file was called when it arrived, which is what the client calls it.
        at: When it arrived, ``YYYY-MM-DD``.
        plan: Every row decided. Never a ``Path`` anywhere in here, so the render stays a pure
            function of its inputs.
    """

    sheet: str
    given_name: str
    at: str
    plan: SignoffPlan


class VideoReport(NamedTuple):
    """Everything §1 renders from.

    Attributes:
        status: The in-scope products joined to the mapping, and the file-side gaps.
        signoff: The newest sheet, re-planned — or ``None``, with :attr:`signoff_absent` saying why.
        signoff_absent: Why there is no review. One line, never a warning.
    """

    status: VideoStatus
    signoff: SignoffReview | None = None
    signoff_absent: str = NO_SHEET
    #: ``(language, file) -> best match`` for the unassigned files, in the complete report only.
    #: ``None`` keeps §1c's short sample.
    suggestions: Mapping[tuple[str, str], VideoCandidate] | None = None


def video_lines(
    video: VideoReport | None,
    video_map_issues: Sequence[SourceIssue],
    *,
    client_id: str,
    languages: Sequence[str],
    also_held: frozenset[str],
) -> list[str]:
    """§1, always rendered — one line when there is nothing to join, so the numbering never moves.

    Args:
        video: The join and the sheet, or ``None`` when the client has no readable mapping.
        video_map_issues: The gaps as read from ``video_map_issues.json`` — used only when
            ``video`` is ``None``, which is the one case where that file is the only record.
        client_id: Names the mapping's path.
        languages: The configured languages, one column each.
        also_held: GTINs held for missing source data (E23) as well — named, so a product in both
            places reads as one product with two jobs rather than two counts that disagree.
    """
    heading = [f"## 1. {TITLE}", ""]
    if video is None:
        lines = [
            *heading,
            "No readable video mapping is configured for this client, so no product can be "
            "joined to a video here.",
            "",
        ]
        if video_map_issues:
            entries = [UnassignedVideo(_language(i.field), i.value) for i in video_map_issues]
            lines += _backlog_lines(tuple(entries), client_id)
        return lines
    status = video.status
    return [
        *heading,
        *_headline(status, also_held),
        *_held_lines(status, languages, also_held),
        *_clash_lines(status),
        "### 1c. Videos still waiting for a barcode",
        "",
        *_backlog_lines(
            status.unassigned,
            client_id,
            publish_without_video=status.publish_without_video,
            suggestions=video.suggestions,
        ),
        *_folder_lines(status),
        *_signoff_lines(video),
    ]


def held_count(
    video: VideoReport | None,
    video_held: list[str],
    mandatory_gaps: dict[str, list[MandatoryGap]],
) -> str:
    """The E24 count — §1's own number, so the Summary and §1 cannot disagree.

    Without a join it falls back to ``video_held``, which leaves out products E23 already holds.
    With one it counts every held product and says how many are held for both reasons: on the pilot
    that is 48, 8 of them also E23, where the old count said 40 beside a section saying 48.
    """
    if video is None:
        return f"{len(video_held)} GTINs"
    held = video.status.held
    both = sum(product.gtin in mandatory_gaps for product in held)
    return f"{len(held)} GTINs" + (f" ({both} also E23)" if both else "")


def summary_rows(video: VideoReport | None, video_map_issues: list[SourceIssue]) -> list[list[str]]:
    """The file-side and sheet rows, each counted from what §1 lists."""
    if video is None:
        return [
            [
                "Media",
                "Videos not yet mapped to a GTIN",
                str(len(video_map_issues)),
                "Client",
                "Unknown — these name no product",
            ]
        ]
    status = video.status
    review = video.signoff
    needs_a_person = (
        str(sum(len(review.plan.of(outcome)) for outcome in (AMBIGUOUS, REJECTED)))
        if review is not None
        else "—"
    )
    return [
        [
            "Media",
            "Videos not yet mapped to a GTIN",
            str(len(status.unassigned)),
            "Client",
            "Unknown — these name no product",
        ],
        [
            "Media",
            "Products with two videos in one language",
            str(len(status.clashing)),
            "Client",
            "Neither video is attached",
        ],
        [
            "Media",
            "Video files outside the mapping, or missing from their folder",
            str(len(status.not_in_map) + len(status.files_missing)),
            "Operator",
            "Unknown — these name no product",
        ],
        [
            "Media",
            "Sign-off sheet rows that need a person",
            needs_a_person,
            "Client",
            "No — §1d",
        ],
    ]


def _headline(status: VideoStatus, also_held: frozenset[str]) -> list[str]:
    total = len(status.products)
    attached = sum(product.attaches_a_video for product in status.products)
    held = status.held
    both = sum(product.gtin in also_held for product in held)
    if status.publish_without_video:
        return [_headline_publishing_bare(status, attached, total, also_held), ""]
    sentence = (
        f"**{attached} of {total}** in-scope products have a confirmed video in every language. "
        f"**{len(held)}** are held and will not be published at all — in any language, by a run "
        "that reports success."
    )
    if both:
        sentence += (
            f" {both} of those are also held for missing source data (E23), so they need both."
        )
    return [sentence, ""]


def _headline_publishing_bare(
    status: VideoStatus, attached: int, total: int, also_held: frozenset[str]
) -> str:
    """The headline when a missing video marks a product rather than holding it."""
    bare = status.without_video
    sentence = (
        f"**{attached} of {total}** in-scope products have a confirmed video in every language. "
        f"**{len(bare)}** publish with no video in at least one language "
        "(media.publish_without_video) — the page goes live and looks finished, so each is a job "
        "for the client."
    )
    if status.held:
        sentence += (
            f" **{len(status.held)}** are held because two videos are confirmed for one language "
            "(1b)."
        )
    both = sum(product.gtin in also_held for product in bare)
    if both:
        sentence += f" {both} of those {len(bare)} are held anyway for missing source data (E23)."
    return sentence


def _held_lines(
    status: VideoStatus, languages: Sequence[str], also_held: frozenset[str]
) -> list[str]:
    header = ["GTIN", "Product", *languages, "Waiting on"]
    if status.publish_without_video:
        return _bare_lines(status, languages, also_held, header)
    rows = [
        [
            f"`{product.gtin}`" + (" ¹" if product.gtin in also_held else ""),
            cell(product.name),
            *(_MARK.get(product.by_language.get(lang, ""), _NONE) for lang in languages),
            waiting_on(product),
        ]
        for product in status.held
    ]
    return [
        "### 1a. Held — no confirmed video in every language (E24)",
        "",
        "One row per product a run will hold. ● one confirmed video · ○ none · **2** two "
        "confirmed, so neither is attached. ¹ also held for missing source data (E23).",
        "",
        "**This is the product side, and 1c is the file side. Nothing in the tool joins them** — "
        "a video with no barcode names no product, so it cannot be listed against one. The "
        "client's sign-off sheet is what joins them (1d). A product needing a language nobody "
        "has filmed yet is not a mapping job at all.",
        "",
        *table(header, rows),
        "",
    ]


def _bare_lines(
    status: VideoStatus,
    languages: Sequence[str],
    also_held: frozenset[str],
    header: list[str],
) -> list[str]:
    """1a when a missing video marks a product: the ones a run publishes without one."""
    rows = [
        [
            f"`{product.gtin}`" + (" ¹" if product.gtin in also_held else ""),
            cell(product.name),
            *(_MARK.get(product.by_language.get(lang, ""), _NONE) for lang in languages),
            waiting_on(product),
        ]
        for product in status.without_video
    ]
    return [
        "### 1a. Published without a video in some language",
        "",
        "One row per product a run publishes with a language missing its video. ● one confirmed "
        "video · ○ none. ¹ held anyway for missing source data (E23). When the client confirms a "
        "video, the next run adds it to the page that is already live.",
        "",
        *table(header, rows),
        "",
    ]


def _clash_lines(status: VideoStatus) -> list[str]:
    rows = [
        [
            f"`{product.gtin}`",
            cell(product.name),
            language,
            cell(", ".join(product.files[language])),
        ]
        for product in status.clashing
        for language in product.languages_in(CLASHING)
    ]
    return [
        "### 1b. Mapped to two videos",
        "",
        "Each of these has two videos confirmed for one language. The tool cannot choose between "
        "them, so the product is **held**"
        + ("" if status.publish_without_video else " — it is in 1a too —")
        + " and §0 shows that language as missing. Keep one in `mapping.yml` and mark the "
        "other `skip`; the product publishes on the next run.",
        "",
        *table(["GTIN", "Product", "Language", "Files"], rows),
        "",
    ]


def _backlog_lines(
    entries: tuple[UnassignedVideo, ...],
    client_id: str,
    *,
    publish_without_video: bool = False,
    suggestions: Mapping[tuple[str, str], VideoCandidate] | None = None,
) -> list[str]:
    """The unassigned files — a count, a sample, and how many more. No HTML: the report is read raw.

    It used to wrap every filename in ``<details><summary>``, which folds on a rendering surface
    and does nothing on the one this report is read on. So it is short by *being* short.
    """
    mapping = f"`input/{client_id}/videos/mapping.yml`"
    lines = [
        f"**{len(entries)}** video files have no GTIN assigned yet. This is the one list in the "
        "report not narrowed to the selection: a file nobody has assigned names no product, so "
        f"there is nothing to narrow it by. Client to map each filename → GTIN in {mapping} (or "
        "mark `skip`). "
        + (
            "A product publishes without one meanwhile, and gets it on the next run once mapped."
            if publish_without_video
            else "A product only becomes publishable once it has a confirmed video in "
            "**every** language."
        ),
        "",
    ]
    if entries and suggestions is not None:
        # The complete report's version: every file, with a starting point for the client. A
        # suggestion is a fuzzy match on the name — a hint to confirm, never an assignment.
        return [
            *lines[:-1],
            " The *best match* is the export product whose name is closest to the filename — a "
            "hint to check against the video, not an answer.",
            "",
            *table(
                ["Language", "File", "Best match in the export"],
                [
                    [e.language, cell(e.file), _suggestion(suggestions.get((e.language, e.file)))]
                    for e in entries
                ],
            ),
            "",
        ]
    if entries:
        lines += [f"- `{e.language}` — {cell(e.file)}" for e in entries[:VIDEO_SAMPLE]]
        if (remaining := len(entries) - VIDEO_SAMPLE) > 0:
            lines.append(
                f"- _…and {remaining} more — every unassigned file is in {mapping}, which is "
                "where they are assigned._"
            )
        lines.append("")
    return lines


def _suggestion(candidate: VideoCandidate | None) -> str:
    if candidate is None:
        return "—"
    return f"`{candidate.gtin}` {cell(candidate.name)} ({candidate.score:.2f})"


def _folder_lines(status: VideoStatus) -> list[str]:
    """The two gaps the mapping check always found and the report never showed."""
    lines = [
        f"**{len(status.not_in_map)}** files are in a video folder with no row in the mapping, and "
        f"**{len(status.files_missing)}** rows name a file that is not in its folder.",
        "",
    ]
    for label, entries in (
        ("Not in the mapping", status.not_in_map),
        ("Not in the folder", status.files_missing),
    ):
        if entries:
            shown = ", ".join(f"`{e.language}` {cell(e.file)}" for e in entries[:VIDEO_SAMPLE])
            more = len(entries) - VIDEO_SAMPLE
            lines += [f"- {label}: {shown}" + (f" _…and {more} more_" if more > 0 else ""), ""]
    return lines


def _signoff_lines(video: VideoReport) -> list[str]:
    heading = ["### 1d. What the client's sign-off sheet would still change", ""]
    review = video.signoff
    if review is None:
        return [*heading, video.signoff_absent, ""]
    plan = review.plan
    n = {outcome: len(plan.of(outcome)) for outcome in (CHANGE, UNCHANGED, AMBIGUOUS)}
    rejected, blank = plan.of(REJECTED), plan.of(BLANK)
    verdict = (
        "**The sheet cannot be applied as it is**: it gives a product two videos in one language "
        "(listed below), and a product carries one video per language."
        if plan.refused
        else "Changes are applied with one button in the operator shell; the sheet leads for every "
        "barcode it names, and a barcode it does not name keeps its video."
    )
    return [
        *heading,
        f"Sheet **{cell(review.given_name)}**, received {review.at} (kept as "
        f"`{cell(review.sheet)}`), re-read against the mapping as it is now.",
        "",
        f"Of its {len(plan.rows)} rows: **{n[CHANGE]}** would change a video's barcodes, "
        f"{n[UNCHANGED]} already match, **{n[AMBIGUOUS]}** would give a product a second video in "
        f"one language (1b lists the {len(video.status.clashing)} already in the selection), "
        f"**{len(rejected)}** are rejected, and {len(blank)} are blank. {verdict}",
        "",
        "**What the mapping would become:**",
        "",
        *table(
            ["Language", "Video", "Now", "After", "Sheet row"],
            [
                [
                    cell(change.language),
                    cell(change.file),
                    cell(describe(change.before)),
                    cell(describe(change.after)),
                    ", ".join(map(str, change.lines)) or "— another row took its barcode",
                ]
                for change in plan.changes
            ],
        ),
        "",
        "**Two videos for one product — the sheet is refused until one goes:**",
        "",
        *table(
            ["Row", "Language", "File", "Sheet says", "Why not"],
            [_row_cells(row) for row in plan.of(AMBIGUOUS)],
        ),
        "",
        "**Rejected — for the client to correct in the sheet:**",
        "",
        *table(
            ["Row", "Language", "File", "Barcode as read", "Why"], [_row_cells(r) for r in rejected]
        ),
        "",
    ]


def _row_cells(row: SignoffRow) -> list[str]:
    """The sheet's own row number first: it is the only address the person fixing it has."""
    return [str(row.line), cell(row.language), cell(row.file), cell(row.given), cell(row.detail)]


def _language(field: str) -> str:
    return field.rsplit(".", 1)[-1] if "." in field else field
