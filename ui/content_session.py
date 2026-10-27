"""The Content screen's live-site check, kept for the session so coming back does not lose it.

Operator, 2026-10-07: going back to Content hid steps 2 and 3 again — "this should only happen at a
new session not when skipping back and forth to it". The screen used to forget the check on every
draw, so every visit started at step 1.

**Kept per client and per batch, for the life of the process.** A restart starts clean, which is
the operator's rule for a new session. A change on Data starts clean too: the check was of the
batch saved then, and says nothing about one saved since. The batch is identified by its barcodes,
the same set the screen's review is scoped to (:func:`ui.context.batch_scope`).

Only a check that read the site is kept — a failed one has nothing to come back to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContentChecks:
    """The last successful live-site check per client, with the batch it was made for."""

    _kept: dict[str, tuple[frozenset[str], Any]] = field(default_factory=dict)

    def keep(self, client_id: str, batch: frozenset[str], payload: Any) -> None:
        """Remember this check as the one for ``batch``."""
        self._kept[client_id] = (batch, payload)

    def find(self, client_id: str, batch: frozenset[str]) -> Any:
        """The check kept for exactly this batch, or ``None``."""
        kept = self._kept.get(client_id)
        if kept is None or kept[0] != batch:
            return None
        return kept[1]


#: The one per process.
CHECKS = ContentChecks()
