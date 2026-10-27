"""Edit one row's GTIN in ``mapping.yml`` without disturbing anything else in it.

The video mapping is the operator's own document, and the parts of it this screen must not touch
are the parts that carry the most information:

* **Confirmed rows are client sign-off.** A GTIN in this file means someone looked at a video and
  said which product it shows. Nothing here may overwrite one as a side effect of editing another,
  and the file is never re-drafted from the folder — ``build_video_map`` prints a draft to stdout
  and stays a terminal job for exactly that reason.
* **The trailing comments are the evidence.** Each row carries the fuzzy hint its GTIN was chosen
  from, and which hints were rejected. That is the record of *why* a mapping is what it is, and a
  form that round-tripped the file through ``yaml.safe_dump`` would delete all of it.

So this edits the **text**, one row at a time: it finds the row by its filename inside its
language's block, rewrites the ``gtin`` value in place, and keeps the key order, the spacing and
the comment column. Everything else in the file is left byte for byte.

Two operations, and neither removes a video from the file:

* :func:`set_assignment` — give a video its barcodes (one row per barcode, the first in place, the
  rest directly below it), mark it ``skip``, or clear it back to unset. A row goes only when the
  same video keeps another; :func:`set_gtin` is the one-barcode form.
* :func:`append_rows` — add rows for files found on disk that the mapping does not list yet, so a
  new video does not send the operator back to a text editor.

:func:`write_validated` then refuses any candidate that will not load, or that has lost a row.

No NiceGUI here, so the whole thing is testable without a browser. Shape and validation live in
:mod:`lib.media_video`; this module knows only about lines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from lib import media_video
from lib.errors import VideoMapError
from lib.media_video import canon_gtin, load_video_map, state_of
from ui.text_edit import split_comment, with_comment

#: A top-level ``nl:`` / ``fr:`` key — the start of a language's rows.
_LANGUAGE_RE: Final = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*$")

#: A sequence item. The mapping is written one row per line, as ``build_video_map`` drafts it.
_ITEM_RE: Final = re.compile(r"^(\s*-\s*)(.*)$")

#: The ``gtin`` value inside a flow-style row, quoted or bare.
_GTIN_RE: Final = re.compile(r"(gtin\s*:\s*)('[^']*'|\"[^\"]*\"|[^,}\s]*)")

#: The sentinel meaning "this video maps to no product" — a decision, not a gap. Taken from
#: :mod:`lib.media_video` rather than spelled again, so the screen and the pipeline cannot come to
#: disagree about what ``skip`` means; :func:`lib.media_video.state_of` is re-exported here for
#: the same reason, and this module's callers keep reaching it as ``video_map_edit.state_of``.
SKIP: Final = media_video.SKIP

#: Indent for a row appended to a language that has none yet, matching the drafted style.
_ROW_INDENT: Final = "  "


@dataclass(frozen=True)
class VideoRow:
    """One row of the mapping, and where it is in the file.

    Attributes:
        language: The language block this row sits in.
        file: The video filename the row names.
        gtin: The GTIN as written — ``""`` unset, ``"skip"``, or a real GTIN.
        line: 0-based index of the row's line, so an edit can find it again.
        note: The trailing comment as written, including its ``#``. Usually the fuzzy hints.
    """

    language: str
    file: str
    gtin: str
    line: int
    note: str

    @property
    def state(self) -> str:
        """``unset``, ``skip`` or ``confirmed`` — what the screen groups the row by."""
        return state_of(self.gtin)


def parse(text: str) -> list[VideoRow]:
    """Read every row out of the mapping, keeping its line number and its comment.

    Args:
        text: The file contents.

    Returns:
        Every row, in file order.

    Raises:
        VideoMapError: If a row is not written as a single-line flow mapping. Every row this tool
            drafts is; one that has been reformatted by hand cannot be edited in place without
            guessing where it ends, and guessing wrong would corrupt a file that represents client
            sign-off. Refusing says so, and the terminal still works.
    """
    rows: list[VideoRow] = []
    language = ""
    for n, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        matched_language = _LANGUAGE_RE.match(line)
        if matched_language:
            language = matched_language.group(1)
            continue
        item = _ITEM_RE.match(line)
        if item is None:
            continue
        body, comment = split_comment(item.group(2))
        body = body.strip()
        if not body.startswith("{"):
            raise VideoMapError(
                f"line {n + 1} of the video mapping is not written as one row per line "
                f"({stripped[:40]!r}). This screen edits the drafted `- {{file: …, gtin: …}}` "
                "form; edit this file in a text editor, or re-draft it with "
                "`python -m scripts.build_video_map`."
            )
        parsed = _row_fields(body, n)
        rows.append(
            VideoRow(
                language=language,
                file=str(parsed.get("file", "")),
                gtin=str(parsed.get("gtin", "") or ""),
                line=n,
                note=comment.strip(),
            )
        )
    return rows


def set_gtin(text: str, language: str, file: str, gtin: str) -> str:
    """Return ``text`` with one video mapped to one value — a GTIN, ``"skip"``, or ``""`` (unset).

    Raises:
        VideoMapError: If no row in that language names that file, or a row cannot be parsed.
    """
    return set_assignment(text, language, file, (gtin,) if gtin else ())


def set_assignment(text: str, language: str, file: str, assignment: Sequence[str]) -> str:
    """Return ``text`` with one video's rows set to ``assignment``, every other byte untouched.

    One row per value. A row that already carries one of the values is left exactly as written —
    13-digit spelling and all — so applying a sheet that changes nothing for a video is no diff.
    The other rows take the remaining values in order, keeping their comments; a value with no row
    left gets a new one directly below the video's last row; a row with no value left is dropped.
    There is always at least one value, so the video itself never leaves the file.

    Args:
        text: The file contents.
        language: The language block the video is in.
        file: The filename identifying the video.
        assignment: Its new GTINs, or ``("skip",)``, or ``()`` to clear it back to unset.

    Returns:
        The edited text.

    Raises:
        VideoMapError: If no row in that language names that file, or a row cannot be parsed.
    """
    rows = [r for r in parse(text) if r.language == language and r.file == file]
    if not rows:
        raise VideoMapError(f"the video mapping has no row for {file!r} in {language!r}")

    values = list(dict.fromkeys(assignment)) or [""]
    lines = text.splitlines()
    # Each value claims the first row already saying it; those rows are left alone.
    kept: set[int] = set()
    left: list[str] = []
    for value in values:
        match = next((r for r in rows if r.line not in kept and _same_value(r.gtin, [value])), None)
        if match is None:
            left.append(value)
        else:
            kept.add(match.line)
    # The rest take the remaining values in file order; whatever is still over is dropped. A video
    # always keeps at least one row, because there is always at least one value.
    free = [row for row in rows if row.line not in kept]
    rewritten = {
        row.line: _row_with(lines[row.line], value) for row, value in zip(free, left, strict=False)
    }
    # A dropped row's comment is evidence — why that barcode was chosen — so it stays in the file,
    # as a comment line where the row was.
    dropped = {row.line: row.note for row in free[len(left) :]}
    first = _ITEM_RE.match(lines[rows[0].line])
    indent = first.group(1) if first else f"{_ROW_INDENT}- "
    added = [
        f"{indent}{{file: {_quoted(file)}, gtin: {_quoted(value)}}}" for value in left[len(free) :]
    ]

    # Rows are either added or dropped, never both, so the video's last row is kept whenever there
    # is anything to add below it.
    out: list[str] = []
    for n, line in enumerate(lines):
        if n in dropped:
            if dropped[n]:
                out.append(line[: len(line) - len(line.lstrip())] + dropped[n])
            continue
        out.append(rewritten.get(n, line))
        if n == rows[-1].line:
            out.extend(added)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _quoted(value: str) -> str:
    """A YAML double-quoted scalar. JSON's string syntax is one, escapes included — so a filename
    with a quote or a backslash in it names that file, rather than breaking or renaming the row."""
    return json.dumps(value, ensure_ascii=False)


def _same_value(written: str, values: Sequence[str]) -> bool:
    """Whether a row's ``gtin`` cell already says one of ``values`` — canonically, for a GTIN."""
    state = state_of(written)
    for value in values:
        if state != state_of(value):
            continue
        if state != media_video.CONFIRMED or canon_gtin(written) == canon_gtin(value):
            return True
    return False


def _row_with(line: str, gtin: str) -> str:
    """One row's line with its ``gtin`` replaced, its comment kept in its column."""
    item = _ITEM_RE.match(line)
    if item is None:  # pragma: no cover - parse() already matched this line
        raise VideoMapError("a video mapping row is no longer a row")
    body, comment = split_comment(item.group(2))
    column = len(item.group(1)) + len(body) if comment else 0
    head = item.group(1) + _with_gtin(body.rstrip(), gtin)
    return with_comment(head, comment.strip(), column)


def apply_edits(text: str, edits: Mapping[tuple[str, str], str | Sequence[str]]) -> str:
    """Apply a screen's whole batch of ``{(language, file): assignment}`` in one pass.

    One pass because one Save should produce one backup and one diff. Applying each row as it is
    confirmed would leave a trail of ``.bak`` files whose newest is one edit behind the mistake
    the operator wants to undo.

    Args:
        text: The file contents.
        edits: Keyed by ``(language, file)``: a video's whole new assignment, or a single value as
            :func:`set_gtin` takes it.

    Returns:
        The edited text.

    Raises:
        VideoMapError: If any edit names a video that is not there.
    """
    for (language, file), value in edits.items():
        if isinstance(value, str):
            text = set_gtin(text, language, file, value)
        else:
            text = set_assignment(text, language, file, value)
    return text


def files_missing_from_map(
    text: str, files_by_language: Mapping[str, list[str]]
) -> dict[str, list[str]]:
    """Which files on disk have no row yet, per language.

    Args:
        text: The file contents.
        files_by_language: Filenames found in each configured video folder.

    Returns:
        ``{language: [filename]}``, languages with nothing missing omitted.
    """
    known = {(row.language, row.file) for row in parse(text)}
    missing = {
        language: [name for name in names if (language, name) not in known]
        for language, names in files_by_language.items()
    }
    return {language: names for language, names in missing.items() if names}


def append_rows(text: str, language: str, files: list[str]) -> str:
    """Add unset rows for ``files`` at the end of ``language``'s block.

    Append-only, and it never touches an existing row: a file that turns up in the folder after
    the mapping was drafted is the one case where the alternative is a text editor.

    Args:
        text: The file contents.
        language: The language block to extend, created at the end of the file if absent.
        files: Filenames to add. Ones already present in that language are ignored.

    Returns:
        The edited text.
    """
    known = {row.file for row in parse(text) if row.language == language}
    wanted = [name for name in files if name not in known]
    if not wanted:
        return text

    lines = text.splitlines()
    new_rows = [f'{_ROW_INDENT}- {{file: {_quoted(name)}, gtin: ""}}' for name in wanted]
    at = _end_of_language(lines, language)
    if at is None:
        lines.extend([f"{language}:", *new_rows])
    else:
        lines[at:at] = new_rows
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")


def write_validated(path: Path, text: str) -> Path:
    """Validate ``text`` as a video mapping, then write it, keeping the previous version.

    Two refusals, both aimed at the same thing — this file is a record of client sign-off, and
    the screen that edits it must not be able to lose one:

    * the candidate must load through :func:`lib.media_video.load_video_map`, the same function
      the pipeline and the doctor use, so the screen and the run cannot disagree about what is
      valid;
    * every ``(language, file)`` in the file being replaced must still be there. A video may lose
      a *duplicate* row — one barcode fewer — but nothing here takes a video out of the file, so
      one that has gone is a bug in the text editing, and the right response is to write nothing.

    Args:
        path: The mapping to replace.
        text: The full candidate contents.

    Returns:
        The path the previous version was kept at.

    Raises:
        VideoMapError: If the candidate will not load or has lost a video. The file on disk is
            left exactly as it was.
    """
    candidate = path.parent / f"{path.name}.candidate"
    candidate.write_text(text, encoding="utf-8")
    try:
        load_video_map(candidate)
    finally:
        candidate.unlink(missing_ok=True)

    if path.exists():
        before = {(r.language, r.file) for r in parse(path.read_text(encoding="utf-8"))}
        lost = before - {(r.language, r.file) for r in parse(text)}
        if lost:
            raise VideoMapError(
                f"refusing to write: {len(lost)} video(s) would disappear from the video mapping "
                f"({', '.join(sorted(f'{lang}/{name}' for lang, name in lost))}). Nothing on this "
                "screen removes a video, so this is a fault in the tool — the file is unchanged."
            )

    backup = path.parent / f"{path.name}.bak"
    if path.exists():
        backup.write_bytes(path.read_bytes())
    path.write_text(text, encoding="utf-8")
    return backup


def _row_fields(body: str, line: int) -> dict[str, object]:
    """Parse one flow-style row body into its fields."""
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise VideoMapError(f"line {line + 1} of the video mapping will not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VideoMapError(f"line {line + 1} of the video mapping is not a {{file, gtin}} row")
    return parsed


def _with_gtin(body: str, gtin: str) -> str:
    """Replace (or add) the ``gtin`` value inside a flow-style row body.

    Always quoted. An unquoted GTIN is read back as an integer, which fails validation — and a
    13-digit one would lose its leading zero on the way.
    """
    rendered = f'"{gtin}"'
    if _GTIN_RE.search(body):
        return _GTIN_RE.sub(lambda m: f"{m.group(1)}{rendered}", body, count=1)
    return f"{body[:-1].rstrip().rstrip(',')}, gtin: {rendered}}}"


def _end_of_language(lines: list[str], language: str) -> int | None:
    """The index just past ``language``'s last row, or ``None`` if it has no block."""
    start: int | None = None
    for n, line in enumerate(lines):
        matched = _LANGUAGE_RE.match(line)
        if matched is None:
            continue
        if matched.group(1) == language:
            start = n
        elif start is not None:
            return _last_content(lines, start + 1, n)
    if start is None:
        return None
    return _last_content(lines, start + 1, len(lines))


def _last_content(lines: list[str], start: int, end: int) -> int:
    """One past the last non-blank, non-comment line in ``[start, end)``.

    Not simply ``end``: a block often ends in blank lines or a comment introducing the *next*
    language, and a row appended after that comment would read as belonging to it.
    """
    last = start
    for n in range(start, end):
        stripped = lines[n].strip()
        if stripped and not stripped.startswith("#"):
            last = n + 1
    return last
