"""Tests for ui/progress.py — a step is reached by pressing Next on the one before it.

The operator's rule: every later screen depends on the one before it, so a session must not be
able to show one state on Data and act on another three screens on. Going back re-locks what
follows, until Next is pressed there again.
"""

from __future__ import annotations

import pytest

from lib.gates import Mode
from ui.progress import Progress, not_needed


def test_a_new_session_starts_at_data_with_every_later_step_locked() -> None:
    progress = Progress()

    assert progress.arrive("/data") is None
    assert progress.locked() == {"/content", "/preflight", "/publish"}


@pytest.mark.parametrize("route", ["/content", "/preflight", "/publish"])
def test_a_later_step_typed_in_directly_goes_back_to_the_step_reached(route: str) -> None:
    progress = Progress()

    assert progress.arrive(route) == "/data"


def test_next_unlocks_exactly_the_following_step() -> None:
    progress = Progress()

    progress.advance("/data")

    assert progress.arrive("/content") is None
    assert progress.arrive("/preflight") == "/content"


def test_going_back_relocks_every_step_after_it() -> None:
    """Strict, by the operator's choice: Data may have changed, so nothing built on it stands."""
    progress = Progress()
    progress.advance("/data")
    progress.advance("/content")
    progress.advance("/preflight")

    assert progress.arrive("/data") is None

    assert progress.locked() == {"/content", "/preflight", "/publish"}
    assert progress.arrive("/publish") == "/data"


def test_reloading_the_step_you_are_on_changes_nothing() -> None:
    progress = Progress()
    progress.advance("/data")

    assert progress.arrive("/content") is None
    assert progress.arrive("/content") is None
    assert progress.locked() == {"/preflight", "/publish"}


def test_next_on_a_step_not_reached_does_not_skip_ahead() -> None:
    """A Next pressed from a stale tab must not unlock past what the session actually did."""
    progress = Progress()

    progress.advance("/preflight")

    assert progress.locked() == {"/content", "/preflight", "/publish"}


def test_a_screen_that_is_not_a_step_is_always_open() -> None:
    """Setup, Runs and the rest are about this machine, not the batch."""
    progress = Progress()

    assert progress.arrive("/runs") is None
    assert progress.arrive("/") is None
    assert progress.locked() == {"/content", "/preflight", "/publish"}


def test_a_links_only_batch_goes_from_data_straight_to_preflight() -> None:
    """Content writes page copy; a batch that writes no page has nothing to do there."""
    progress = Progress()

    opened = progress.advance("/data", skip=not_needed(Mode.LINKS))

    assert opened == "/preflight"
    assert progress.locked() == {"/publish"}
    assert progress.arrive("/content") is None, "passed over, not hidden"


def test_visiting_a_passed_over_step_relocks_what_follows_it() -> None:
    progress = Progress()
    progress.advance("/data", skip=not_needed(Mode.LINKS))

    progress.arrive("/content")

    assert progress.locked() == {"/preflight", "/publish"}


@pytest.mark.parametrize("mode", [Mode.PAGES, Mode.BOTH, None])
def test_any_other_batch_still_goes_through_content(mode: Mode | None) -> None:
    progress = Progress()

    assert progress.advance("/data", skip=not_needed(mode)) == "/content"
