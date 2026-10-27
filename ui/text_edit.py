"""Editing YAML the shell treats as a document rather than as a serialised structure.

Two of this project's YAML files are hand-written and hand-commented, and in both the comments
carry information nothing else records: ``clients.yml`` explains why a value is what it is, and
``input/{client}/videos/mapping.yml`` carries the fuzzy hint each GTIN was chosen from — the
evidence behind a client's sign-off. Round-tripping either through ``yaml.safe_dump`` deletes
every comment, and even a round-trip YAML library collapses their hand-alignment.

So both editors work on the **text**, rewriting one value on one line. This module holds the two
pieces that job needs in both places: finding where a trailing comment starts without being fooled
by a ``#`` inside a quoted string, and putting it back in the column it was written in.

No NiceGUI here, and nothing about either file's shape — see :mod:`ui.config_edit` and
:mod:`ui.video_map_edit` for those.
"""

from __future__ import annotations

from typing import Final

#: Minimum gap between a value and its trailing comment when the original column no longer fits.
_MINIMUM_GAP: Final = 2


def split_comment(text: str) -> tuple[str, str]:
    """Separate a trailing ``#`` comment from a value, respecting quotes.

    A ``#`` inside quotes is part of the value, and a ``#`` not preceded by whitespace is too —
    both appear in real filenames and URLs.

    Args:
        text: Everything after the key's colon, or a whole line.

    Returns:
        ``(value_as_written, comment_including_hash)``. The comment is empty when there is none.
    """
    quote = ""
    for n, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (n == 0 or text[n - 1] in " \t"):
            return text[:n], text[n:]
    return text, ""


def with_comment(head: str, comment: str, column: int) -> str:
    """Re-attach ``comment`` to ``head``, keeping its column where the new value still fits.

    Cosmetic, but these files are read as documents and their diffs reviewed by hand: a value
    that grows by one character should not reflow every comment after it, and a value that grows
    past the column should push its own comment along rather than overwrite it.

    Args:
        head: The rewritten line up to but not including the comment.
        comment: The comment as written, including its ``#``. Empty returns ``head`` unchanged.
        column: The column the comment started at before the edit.

    Returns:
        The full line.
    """
    if not comment:
        return head
    return head + " " * max(column - len(head), _MINIMUM_GAP) + comment
