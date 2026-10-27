"""Where an installation keeps its data, as opposed to its code.

The code folder holds what a release ships: ``lib/``, ``scripts/``, ``ui/``, ``schema/``,
``reference/``, ``templates/_default/``. The **data folder** holds what one installation owns and an
update must never touch: ``.env``, ``clients.yml``, ``input/`` and ``output/`` — and inside
``output/``, ``state.json``, the ledger of permanent GS1 writes. A client's own prompt and page
templates are data too, under ``input/{client}/`` (:mod:`lib.client_files`).

By default the two are the same folder, which is how every installation before this module
worked, and nothing changes for them. Setting ``GS1_DATA_DIR`` separates them: the container
mounts its data at ``/data``, and an installer can put the data beside the user's documents so
that replacing the code folder cannot replace the ledger.

Every data path in this project is relative — ``Path("output") / client_id / …`` in code, and
``./input/…`` in ``clients.yml`` — so the data folder is simply the working directory. An entry
point calls :func:`enter_data_dir` from its ``if __name__ == "__main__":`` block, beside
:func:`lib.env.load_env` and for the same reason: tests call ``main()`` directly and must keep
their own working directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

#: The folder this release was installed into. Read-only assets resolve against it.
CODE_ROOT: Final = Path(__file__).resolve().parent.parent

#: Names a separate data folder. Unset or blank means the code folder.
DATA_DIR_ENV: Final = "GS1_DATA_DIR"


def data_root() -> Path:
    """The data folder: ``$GS1_DATA_DIR`` when set, else the code folder.

    Raises:
        NotADirectoryError: ``GS1_DATA_DIR`` names something that is not an existing folder.
            Falling back to the code folder instead would quietly start a second ledger there,
            which is the one outcome this setting exists to prevent.
    """
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    if not configured:
        return CODE_ROOT
    path = Path(configured).expanduser().resolve()
    if not path.is_dir():
        raise NotADirectoryError(
            f"{DATA_DIR_ENV} is set to {configured!r}, which is not an existing folder. "
            "Create it, or unset the variable to keep data in the code folder."
        )
    return path


def enter_data_dir() -> Path:
    """Make the data folder the working directory, when one is configured; return it.

    With ``GS1_DATA_DIR`` unset this changes nothing — a script keeps resolving its relative
    paths against wherever it was started from, exactly as before the setting existed.
    """
    root = data_root()
    if os.environ.get(DATA_DIR_ENV, "").strip():
        # Absolute from here on: a child started inside the data folder would otherwise resolve a
        # relative value against it, and look for the data folder inside itself.
        os.environ[DATA_DIR_ENV] = str(root)
        os.chdir(root)
    return root
