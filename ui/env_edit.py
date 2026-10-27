"""Report which credentials ``.env`` holds, and set new ones — without ever reading one back.

``clients.yml`` names the environment variables; ``.env`` holds their values. That split is the
whole credential design (OD-1), and this module is the only place in the shell that touches the
second half of it.

**It does not load ``.env``.** Not with ``lib.env``, not with ``python-dotenv``, not by any other
route — ``tests/lib/test_env.py`` walks the AST of every module under ``ui/`` and fails the build
if one does. The reason is not tidiness: loading it would put a production WordPress application
password and GS1 production OAuth credentials into a long-lived desktop process, and would arm the
four staging-guard variables inside it. The subprocesses load it themselves, in their own
``__main__`` blocks, which is why the shell can stay ignorant. ``ENV_PATH`` is therefore spelled
out here rather than imported from ``lib.env``, whose import alone would trip that check.

**Every field over this module is write-only.** :func:`describe` answers *whether* a name has a
value and how many whitespace-separated groups it has — never the value. A form that showed a
secret back would put it in a screenshot, a support ticket and a shoulder-surf, and would buy
nothing: an operator who wants to know the password is correct should press Test, which asks
WordPress.

The group count is not trivia. A WordPress application password is issued as six space-separated
groups, and the single commonest credential failure in this project is a value that lost its
quotes in ``.env`` and was truncated at the first — producing a 401 with a password the operator
is certain is right. So values written here are always quoted, and a short one is reported as
short before anything is run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lib.owner_only import restrict
from ui import DATA_ROOT

#: Spelled out rather than imported from ``lib.env`` — see the module docstring.
ENV_PATH: Final = DATA_ROOT / ".env"

#: A WordPress application password is issued as six space-separated groups.
APP_PASSWORD_GROUPS: Final = 6

#: ``NAME=``, with the optional ``export`` prefix ``.env`` files sometimes carry.
_ASSIGNMENT: Final = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

#: The shortest a quoted value can be: the two quotes themselves.
_QUOTED_MINIMUM: Final = 2

#: Written above any name this shell appends, so a hand-edited file stays legible afterwards.
_APPENDED_HEADER: Final = "# --- Added by the operator shell ---"


@dataclass(frozen=True)
class Secret:
    """What may safely be said about one credential: that it is there, and how long it is."""

    name: str
    present: bool
    groups: int

    @property
    def looks_truncated(self) -> bool:
        """Whether a set value has fewer groups than a WordPress application password has.

        Only meaningful for that one variable, so the caller decides whether to ask. Said here
        because the constant and the reasoning belong together.
        """
        return self.present and self.groups < APP_PASSWORD_GROUPS


def describe(names: Sequence[str], path: Path = ENV_PATH) -> dict[str, Secret]:
    """Report presence and group count for each name. Values are read and discarded, never kept.

    Args:
        names: The variable names from ``clients.yml``.
        path: The ``.env`` file to inspect.

    Returns:
        One :class:`Secret` per requested name, in the order asked. A name the file does not
        mention, or mentions with an empty value, is reported as absent — those are the same
        failure to an operator, and both produce the same ``MissingCredentialError`` later.
    """
    found: dict[str, tuple[bool, int]] = {}
    for name, raw in _assignments(path):
        if name not in names:
            continue
        value = _unquote(raw)
        found[name] = (bool(value), len(value.split()))
    return {
        name: Secret(name, *found.get(name, (False, 0))) for name in dict.fromkeys(names) if name
    }


def write_values(values: Mapping[str, str], path: Path = ENV_PATH) -> Path:
    """Set each named variable, keeping the previous file beside it. Returns the backup path.

    A blank value means *leave this one alone*, because the fields over this function are
    write-only: an operator who edits the site URL and saves must not thereby erase the three
    credentials whose boxes they could not see and so did not fill in.

    Existing assignments are rewritten in place, so the file's comments — which document the
    quoting rule and the staging guards — survive. New names are appended under a header.

    Args:
        values: ``{env var name: new value}``. Empty and whitespace-only values are ignored.
        path: The ``.env`` file to write.

    Returns:
        The path the previous version was kept at.
    """
    wanted = {name: value.strip() for name, value in values.items() if value.strip()}
    original = path.read_text(encoding="utf-8") if path.is_file() else ""

    lines = original.splitlines()
    remaining = dict(wanted)
    for n, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) in remaining:
            name = match.group(1)
            lines[n] = f"{name}={_render(remaining.pop(name))}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(_APPENDED_HEADER)
        lines.extend(f"{name}={_render(value)}" for name, value in remaining.items())

    backup = path.parent / f"{path.name}.bak"
    if path.is_file():
        backup.write_bytes(path.read_bytes())
        restrict(backup)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restrict(path)
    return backup


def _assignments(path: Path) -> list[tuple[str, str]]:
    """Every ``NAME=value`` in the file, as raw text. Comments and blanks are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    pairs = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def _unquote(raw: str) -> str:
    """The value a ``.env`` reader would see: quotes removed, trailing comment dropped."""
    value = raw.strip()
    if len(value) >= _QUOTED_MINIMUM and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def _render(value: str) -> str:
    """Quote a value so it survives the file.

    Single quotes by default, because a WordPress application password contains spaces and an
    unquoted one is truncated at the first — the failure this whole module is shaped around.
    A value containing a single quote falls back to double quotes with escapes, which is how
    ``python-dotenv`` reads them back.
    """
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
