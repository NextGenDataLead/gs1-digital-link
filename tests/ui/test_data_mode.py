"""Step 4's choice belongs to one batch: a new product list starts the next one with none.

The run type is saved with the selection and shown again on a restart, which is right for the
batch it was saved with. Carried over to a *new* list, it was a links-only choice waiting under a
batch meant for both. Needs NiceGUI only for the module import.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nicegui", reason="the ui extra is not installed here")

from ui.pages.data import _forgets_mode  # noqa: E402


@pytest.mark.parametrize("which", ["list", "cleared"])
def test_a_new_list_or_a_cleared_batch_forgets_the_run_type(which: str) -> None:
    assert _forgets_mode(which)


@pytest.mark.parametrize("which", ["export", ""])
def test_a_new_export_or_a_redraw_keeps_the_run_type(which: str) -> None:
    """A new export re-checks the same batch; the first draw must keep what was saved."""
    assert not _forgets_mode(which)
