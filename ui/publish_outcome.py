"""What the dialog after a real run says, read from the run's own log. Pure.

Operator, 2026-10-07: the real run ended with a toast and the red button still pressable — "this
way the user can be confident that the run was successful or not". A toast is gone in seconds and
says what the screen *thinks*; the run log says what each row actually did. So the dialog is built
from the log, and it has to be closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lib.records import RunOutcome

#: The status a unit that failed carries in the run log.
_ERROR = "error"


def verdict(
    outcomes: Sequence[RunOutcome], *, returncode: int, permanent: bool
) -> tuple[str, str, str]:
    """``(headline, body, kind)`` for :func:`ui.theme.announce`.

    A failure is any row in error **or** a non-zero exit — a run can die before it writes a row.
    No rows and a clean exit is not called a success: writing nothing and reporting it is the one
    outcome indistinguishable from working.
    """
    failed = [o for o in outcomes if o.status == _ERROR]
    if failed or returncode != 0:
        counted = f"{len(failed)} of {len(outcomes)} page(s) failed" if outcomes else "It stopped"
        return (
            "Run failed",
            f"{counted} (exit {returncode}). Nothing is undone automatically — the Runs screen "
            "shows which rows, and whether their pages are live anyway.",
            "danger",
        )
    if not outcomes:
        return (
            "Nothing was written",
            "The run finished without writing a single row. Check the plan before running again.",
            "warn",
        )
    body = f"{len(outcomes)} page(s) published, 0 errors."
    if permanent:
        barcodes = {o.gtin for o in outcomes if o.gs1_set}
        body += f" GS1 Digital Link records written for {len(barcodes)} barcode(s)."
    return "Published", body + " Every row is on the Runs screen.", "quiet"
