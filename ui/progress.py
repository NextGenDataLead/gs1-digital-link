"""How far this session has come through the batch — and so which steps it may open.

Data, Content, Preflight and Publish each act on what the step before decided. Reached from the
rail in any order, a session could show one batch on Data and act on another three screens on —
the selection ticked on screen but not saved, copy written for last week's batch. So a step opens
only by pressing **Next** on the one before it, and a new session starts at Data.

**Going back re-locks what follows.** The operator chose the strict rule: visiting an earlier step
means something there may change, and nothing built on it should stay one click away until Next is
pressed there again. Pressing Next without changing anything costs one save of the same rows.

The screens under *This machine* — Setup, Runs, the video mapping — are not steps and are always
open: they describe the machine and past runs, not the batch in progress.

**A step the batch does not need is passed over, not hidden.** A links-only batch writes no page,
so it has no copy to write: Next on Data opens Preflight directly, and Content stays reachable
behind it (it says there is nothing to do). Visiting it still re-locks what follows, like any step.

**Per process, per client, never persisted.** A restart is a new session and starts at Data; that
is the point, since the restart is where the screens most easily disagree. Pure apart from the one
module-level dict, so the rule is tested without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from lib.gates import Mode

#: The batch's steps, in order. A route not in here is not a step and is never locked.
STEPS: Final = ("/data", "/content", "/preflight", "/publish")


def not_needed(mode: Mode | None) -> frozenset[str]:
    """The steps a batch publishing in ``mode`` has nothing to do in.

    Content writes page copy, so a links-only batch — which writes no page — skips it. An unknown
    mode skips nothing: passing a step over is a claim about the batch, and there is none to make.
    """
    return frozenset({"/content"}) if mode is Mode.LINKS else frozenset()


@dataclass
class Progress:
    """The furthest step this session may open, as an index into :data:`STEPS`."""

    reached: int = 0

    def arrive(self, route: str) -> str | None:
        """Open ``route``, or say where to go instead.

        Returns ``None`` when the step may open — and then everything after it re-locks — or the
        route of the furthest step reached when it may not.
        """
        if route not in STEPS:
            return None
        index = STEPS.index(route)
        if index > self.reached:
            return STEPS[self.reached]
        self.reached = index
        return None

    def advance(self, route: str, *, skip: frozenset[str] = frozenset()) -> str | None:
        """Next was pressed on ``route``: open the step after it — and nothing further.

        ``skip`` names steps this batch does not need; they are passed over, so the step after
        them opens instead. Returns the route that opened, or ``None`` when nothing did.
        """
        if route not in STEPS:
            return None
        index = STEPS.index(route)
        if index != self.reached:
            return None
        following = index + 1
        while following < len(STEPS) - 1 and STEPS[following] in skip:
            following += 1
        if following >= len(STEPS):
            return None
        self.reached = following
        return STEPS[following]

    def locked(self) -> set[str]:
        """The steps this session may not open yet."""
        return set(STEPS[self.reached + 1 :])


#: One per client, for the life of the process.
_SESSIONS: dict[str, Progress] = {}


def of(client_id: str | None) -> Progress:
    """This client's progress. A missing client still gets one, so the guard never crashes a page
    that is about to say the config did not load."""
    return _SESSIONS.setdefault(client_id or "", Progress())
