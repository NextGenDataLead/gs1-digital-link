"""Make a file readable and writable by its owner only — ``.env``, which holds the passwords.

``chmod 0o600`` is that on macOS and Linux. On Windows it only toggles read-only, and the file
keeps whatever its folder grants, which in a home folder can include other accounts on the machine.
There the same intent is an NTFS permission: inheritance removed, full control for the current user
only — what ``icacls FILE /inheritance:r /grant:r "%USERNAME%:F"`` does by hand, which the
onboarding guide used to ask a non-technical operator to type.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

_OWNER_ONLY: Final = 0o600


def restrict(path: Path) -> bool:
    """Restrict ``path`` to its owner. Returns whether the Windows step ran and succeeded.

    Best effort on Windows: a machine where ``icacls`` is missing or refuses still has the file,
    and the caller has nothing better to do than carry on. ``False`` there lets it say so.
    """
    path.chmod(_OWNER_ONLY)
    if sys.platform != "win32":
        return True
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return False
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv; the path is our own file
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0
