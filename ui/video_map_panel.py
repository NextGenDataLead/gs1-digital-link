"""The client's video mapping file, as the Data screen's sign-off import reads and writes it.

**The mapping is edited in the file, not in the shell.** ``videos/mapping.yml`` is the client's
sign-off; the operator adjusts it there. The row-by-row editor and its figures that used to sit on
the Data screen, folded under *The mapping, file by file*, were removed on the operator's word
(2026-10-06): what a batch needs to know about videos is already in step 4's *Missing video(s)*
and the data-quality report, and a second editor of one sign-off file invites edits nobody signed.

What is left is the one write the shell still makes to it — filling unset rows from the client's
sign-off sheet (:mod:`ui.video_signoff_panel`) — and the session that write goes through:

**Every write re-reads the file**, so nothing on screen asks to be reloaded, and a file changed on
disk since it was read — edited by hand, which is now the normal way — is re-read before the import
is re-planned, rather than silently overwritten. ``video_map_edit.write_validated`` refusing a
candidate that lost a row stays the backstop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lib.config import ClientConfig
from lib.errors import VideoMapError
from ui import DATA_ROOT, theme, video_map_edit


@dataclass
class MappingSession:
    """The mapping as last read, and the edits not yet written to it.

    Attributes:
        path: The mapping file, resolved against the repository.
        text: The file as last read — what the next save applies edits to.
        rows: ``text`` parsed into rows.
        problem: Why the file could not be read, or ``None``.
    """

    path: Path
    text: str = ""
    rows: list[video_map_edit.VideoRow] = field(default_factory=list)
    problem: str | None = None
    _read_as: tuple[float, int] | None = None

    def reload(self) -> None:
        """Re-read the file."""
        try:
            self.text = self.path.read_text(encoding="utf-8")
            self.rows = video_map_edit.parse(self.text)
        except (OSError, VideoMapError) as exc:
            self.problem, self.rows = str(exc), []
            return
        self.problem = None
        self._read_as = _stat(self.path)

    def stale_on_disk(self) -> bool:
        """Whether the file changed since it was read — edited by hand, or by another window."""
        return self._read_as != _stat(self.path)

    def write(self, candidate: str) -> Path | None:
        """Write ``candidate`` and re-read. The backup, or ``None`` if refused (and said so)."""
        try:
            backup = video_map_edit.write_validated(self.path, candidate)
        except (OSError, VideoMapError) as exc:
            theme.notify_problem(str(exc))
            return None
        self.reload()
        return backup

    def write_edits(self, edits: Mapping[tuple[str, str], str | Sequence[str]]) -> Path | None:
        """Apply ``edits`` to the text as last read, then :meth:`write`. ``None`` if refused.

        Applying is inside the refusal too: an edit naming a row the file no longer has (it was
        edited by hand since) raises, and a click handler must say so rather than raise.
        """
        try:
            candidate = video_map_edit.apply_edits(self.text, edits)
        except VideoMapError as exc:
            theme.notify_problem(str(exc))
            return None
        return self.write(candidate)


def _stat(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime, stat.st_size


#: One per client for the life of the process — see the module docstring.
_SESSIONS: dict[str, MappingSession] = {}


def session_for(cid: str, cfg: ClientConfig) -> MappingSession | None:
    """This client's mapping session, or ``None`` when the client attaches no videos."""
    if cfg.media is None or not cfg.media.video_map_path:
        return None
    configured = Path(cfg.media.video_map_path)
    path = configured if configured.is_absolute() else DATA_ROOT / configured
    session = _SESSIONS.get(cid)
    if session is None or session.path != path:
        session = _SESSIONS[cid] = MappingSession(path)
        session.reload()
    elif session.stale_on_disk():
        session.reload()
    return session
