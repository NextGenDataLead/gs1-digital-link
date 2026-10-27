"""Edit named scalars in ``clients.yml`` without disturbing anything else in it.

``clients.yml`` is not a serialised data structure — it is a document. Most of its lines are
comments, and several of them are the only written record of why a value is what it is: why the
pilot's ``environment`` overrides the ``test`` default, why ``account_number_production`` was
corrected after a 200 proved nothing, why the ``generator`` block must stay even on a machine with
no API key. Round-tripping through ``yaml.safe_load``/``safe_dump`` would delete every one of them,
and a round-trip YAML library would still collapse the hand-alignment of the ``gdsn_map`` rows.

So this module edits the **text**. It locates a key by walking indentation, rewrites the value on
that one line — keeping the key, the indent, and any trailing comment — and leaves every other byte
alone. A key that does not exist yet is inserted into its block rather than merged from above.

Three rules make that safe enough to put behind a form:

* **Only the client's own block is ever written.** Every path starts ``clients.{client_id}``, so a
  form can never edit the shared ``defaults`` block and change another client's behaviour. A value
  inherited from ``defaults`` and then edited becomes a per-client override, which is what the
  operator meant.
* **Unchanged fields are not written at all.** The caller passes only what differs, so saving an
  untouched form produces no diff and an inherited default stays inherited.
* **The result is validated before it replaces the file.** :func:`write_validated` writes a
  candidate beside the original, runs :func:`lib.preflight.check_config` over it — the same check
  the doctor runs, reporting *every* offending field rather than the first — and only then swaps it
  in, keeping the previous version as ``clients.yml.bak``.

No NiceGUI here, so the whole thing is testable without a browser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from lib.errors import ConfigError
from lib.preflight import check_config
from ui.text_edit import split_comment, with_comment

#: This project's config files are indented two spaces per level, and a key inserted into an
#: empty block has nothing to copy its indent from.
INDENT_STEP: Final = 2

#: A string safe to write unquoted. Deliberately narrow: it excludes ``:``, spaces and ``#``, so
#: URLs and paths are quoted (as the hand-written file already quotes them) and only bare tokens
#: like ``wpml``, ``nl`` or an env-var name stay plain.
_PLAIN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]*")

#: The shortest a quoted value can be: the two quotes themselves.
_QUOTED_MINIMUM: Final = 2

#: Tokens YAML would read as a boolean or null if left unquoted. A ``post_type`` of ``no`` is
#: absurd, but a ``gtin_column`` named ``No`` is not.
_RESERVED: Final = frozenset({"true", "false", "yes", "no", "on", "off", "y", "n", "null", "~"})


def apply_edits(text: str, edits: Mapping[tuple[str, ...], str | list[str]]) -> str:
    """Return ``text`` with each path set to its value, everything else untouched.

    Args:
        text: The current file contents.
        edits: ``{("clients", client_id, "wordpress", "site_url"): "https://…"}``. A value may be
            a list, which is written in flow style (``[nl, fr]``) to match the file's own style.

    Returns:
        The edited text.

    Raises:
        ConfigError: If a path leads through a key whose value is written inline (``{a: b}``).
            Those are the ``gdsn_map`` rows, which this form does not edit and must not mangle.
    """
    lines = text.splitlines()
    for path, value in edits.items():
        rendered = _render_list(value) if isinstance(value, list) else _render_scalar(value)
        lines = _set(lines, path, rendered)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def write_validated(path: Path, text: str) -> Path:
    """Validate ``text`` as a config, then write it, keeping the previous version.

    The candidate is validated *as a file* rather than as a string, because that is what
    :func:`lib.preflight.check_config` takes and reusing it is the point: the form and the doctor
    then agree about what a valid config is, instead of holding two opinions that drift.

    Args:
        path: The config file to replace.
        text: The full candidate contents.

    Returns:
        The path the previous version was kept at.

    Raises:
        ConfigError: If the candidate is not a valid config. The file on disk is untouched, and
            the message names every offending field — the whole reason the schema is used for
            validation even though it is useless for generating the form.
    """
    candidate = path.parent / f"{path.name}.candidate"
    candidate.write_text(text, encoding="utf-8")
    try:
        result = check_config(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    if result.failed:
        raise ConfigError(result.detail.replace(str(candidate), str(path)))

    backup = path.parent / f"{path.name}.bak"
    if path.exists():
        backup.write_bytes(path.read_bytes())
    path.write_text(text, encoding="utf-8")
    return backup


# --- Locating a key in the text ----------------------------------------------


class _Block:
    """A half-open range of lines, and the indent of the keys directly inside it."""

    __slots__ = ("end", "indent", "start")

    def __init__(self, start: int, end: int, indent: int) -> None:
        self.start = start
        self.end = end
        self.indent = indent


def _set(lines: list[str], path: Sequence[str], rendered: str) -> list[str]:
    """Set one path, creating any missing block along the way. Returns a new list."""
    lines = list(lines)
    block = _Block(0, len(lines), 0)
    for key in path[:-1]:
        at = _find_key(lines, block, key)
        if at is None:
            at = _insertion_point(lines, block)
            lines.insert(at, f"{' ' * block.indent}{key}:")
            block = _Block(at + 1, at + 1, block.indent + INDENT_STEP)
        else:
            block = _body_of(lines, at, block)

    leaf = path[-1]
    at = _find_key(lines, block, leaf)
    if at is None:
        lines.insert(_insertion_point(lines, block), f"{' ' * block.indent}{leaf}: {rendered}")
        return lines
    indent, previous, comment, comment_at = _parts(lines[at], leaf)
    head = f"{indent}{leaf}: {_match_quoting(previous, rendered)}"
    # Comments in this file are hand-aligned into a column, and several of them are the only
    # record of why the value being edited is what it is.
    lines[at] = with_comment(head, comment, comment_at)
    return lines


def _find_key(lines: list[str], block: _Block, key: str) -> int | None:
    """The index of ``key`` as a direct child of ``block``, or ``None``."""
    for n in range(block.start, min(block.end, len(lines))):
        indent = _indent_of(lines[n])
        if indent is None or indent != block.indent:
            continue
        stripped = lines[n].strip()
        if stripped == f"{key}:" or stripped.startswith(f"{key}:"):
            return n
    return None


def _body_of(lines: list[str], key_line: int, outer: _Block) -> _Block:
    """The block nested under the key at ``key_line``.

    Raises:
        ConfigError: If the key carries an inline value. Descending into ``{ sheet: …, … }`` by
            treating the next lines as its body would write the new key into the wrong place.
    """
    key_indent = _indent_of(lines[key_line]) or 0
    _, value, _, _ = _parts(lines[key_line], lines[key_line].strip().split(":", 1)[0])
    if value:
        raise ConfigError(
            f"{lines[key_line].strip().split(':', 1)[0]!r} is written inline in clients.yml; "
            "this form does not edit inline mappings"
        )

    start = key_line + 1
    end = min(outer.end, len(lines))
    for n in range(start, end):
        indent = _indent_of(lines[n])
        if indent is not None and indent <= key_indent:
            end = n
            break
    indent = next(
        (i for n in range(start, end) if (i := _indent_of(lines[n])) is not None),
        key_indent + INDENT_STEP,
    )
    return _Block(start, end, indent)


def _insertion_point(lines: list[str], block: _Block) -> int:
    """Where a new key goes: after the block's last real content.

    Not simply ``block.end``. The lines immediately before a block ends are often blank, or a
    comment introducing the *next* key at the level above — and a key inserted after that comment
    would silently take the comment's subject.
    """
    last = block.start
    for n in range(block.start, min(block.end, len(lines))):
        if _indent_of(lines[n]) is not None:
            last = n + 1
    return last


def _indent_of(line: str) -> int | None:
    """The line's indent, or ``None`` when it holds no content — blank, or a whole-line comment."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return len(line) - len(line.lstrip())


def _parts(line: str, key: str) -> tuple[str, str, str, int]:
    """Split ``key: value  # comment`` into indent, value as written, comment, and its column.

    The comment is kept and rewritten alongside the new value: several of them are the only
    record of why a value is what it is, and a form that dropped them would erase the reasoning
    for the very field it was editing. Its column is returned so the file's alignment survives
    too — cosmetic, but this file is read as a document and its diffs are reviewed by hand.

    The value is returned **as written**, quotes included, so :func:`_match_quoting` can keep the
    style the author chose.
    """
    indent = " " * (len(line) - len(line.lstrip()))
    after_colon = len(indent) + len(key) + 1
    written, comment = split_comment(line[after_colon:])
    return indent, written.strip(), comment, after_colon + len(written) if comment else 0


# --- Rendering values ---------------------------------------------------------


def _match_quoting(previous: str, rendered: str) -> str:
    """Keep the quoting style the file's author chose, where both spellings mean the same thing.

    ``username: "gs1-publisher"`` needs no quotes, but it has them, and rewriting it as
    ``username: gs1-publisher`` puts a change in the diff that is not a change in the config.
    Only the harmless direction is applied — quotes are added back, never stripped, since a
    rendered value that *is* quoted may well need to be.
    """
    if rendered.startswith(('"', "'", "[")):
        return rendered
    if len(previous) >= _QUOTED_MINIMUM and previous[0] == previous[-1] and previous[0] in "\"'":
        return f"{previous[0]}{rendered}{previous[0]}"
    return rendered


def _render_scalar(value: str) -> str:
    """A YAML scalar for ``value``, quoted whenever plain style would change its meaning."""
    if _PLAIN.fullmatch(value) and value.lower() not in _RESERVED:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_list(values: list[str]) -> str:
    """Flow style, matching how ``languages`` and ``formats`` are already written."""
    return "[" + ", ".join(_render_scalar(item) for item in values) + "]"
