"""Tests for ui/content_session.py — Content keeps its live-site check for the session.

Operator: going back to Content hid steps 2 and 3 again; that should happen at a new session,
not when moving back and forth. The check is kept per client and per batch: a check of the
batch saved before a change on Data says nothing about the batch saved after it.
"""

from __future__ import annotations

from ui.content_session import ContentChecks

BATCH = frozenset({"02987653004488", "02987653005898"})


def test_a_check_is_found_again_for_the_same_batch() -> None:
    checks = ContentChecks()

    checks.keep("otherclient", BATCH, {"counts": {}})

    assert checks.find("otherclient", BATCH) == {"counts": {}}


def test_a_different_batch_starts_clean() -> None:
    checks = ContentChecks()
    checks.keep("otherclient", BATCH, {"counts": {}})

    assert checks.find("otherclient", BATCH | {"02987653007151"}) is None


def test_a_new_session_starts_clean() -> None:
    """A restart is a new ``ContentChecks`` — nothing is persisted, by design."""
    assert ContentChecks().find("otherclient", BATCH) is None


def test_clients_do_not_share_a_check() -> None:
    checks = ContentChecks()
    checks.keep("otherclient", BATCH, {"counts": {}})

    assert checks.find("democlient", BATCH) is None
