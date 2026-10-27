"""Start a batch fresh: set this batch's live inputs aside, so the Data screen is empty again.

The shell remembers everything between sittings — it reads the batch from disk — so "start over"
used to mean finding three files by hand. This moves them, and only them:

* the **selection** a run reads (``process/selection/selections.xlsx``) and its ticked snapshot,
* the **uploaded list** it was chosen from (``process/uploads/product-list.xlsx``),
* the **GS1 export** a run reads (``process/uploads/GS1 export/export.xlsx``),
* the **parsed products** the export was read into (``output/{client}/data/products.json``) — left
  behind, it would keep every other screen describing the export just set aside.

**Moved, never deleted**, into ``input/{client}/superseded/cleared-{stamp}/`` with a note saying
what happened. ``input/`` is gitignored, so a delete cannot be undone; and each of the three inputs
already has a dated copy beside it (archived on the way in), so the move loses nothing a person
could want back.

**Deliberately untouched:** every dated archive, the video mapping (the client's sign-off — weeks of
work, and not part of any one batch), the sign-off sheets, ``state.json``, generated copy, the run
folders, and anything live. A fresh batch is a new *intention*; none of those is one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

from lib.input_layout import archive_path, client_root, unique
from lib.ticked_snapshot import path_for

#: Where cleared batches go, under the client's input folder. The layout already reserves it for
#: inputs set aside (see :mod:`lib.input_layout`).
SUPERSEDED_DIR: Final = "superseded"

#: The note left in each cleared folder, for somebody who finds it in Finder.
NOTE_NAME: Final = "README.md"

_NOTE: Final = """# A batch cleared from the Data screen

*Clear all — start fresh* moved these files here at {stamp} (UTC). They were the batch's live
inputs: the selection a run reads, the list it was chosen from, the GS1 export, and the products
that export was read into.

Nothing was deleted. Every upload and every saved selection also has a dated copy under
`process/uploads/` and `process/selection/`. To go back to this batch, upload the export and the
list again on the Data screen — or copy these files back to the paths `clients.yml` names.
"""


def clear_batch(*, export: Path, selection: Path, products: Path, stamp: str) -> list[Path]:
    """Move this batch's live inputs aside. Returns where each moved file now is.

    Args:
        export: The export a run reads — ``export.path`` in ``clients.yml``, resolved.
        selection: The selection a run reads — ``process_list.path``, resolved. The uploaded list
            is found beside it by :func:`lib.input_layout.archive_path`.
        products: The parsed ``products.json``.
        stamp: Names the folder; the caller's clock, so this stays testable.

    Returns:
        The moved files' new paths, empty when there was nothing to move — in which case no folder
        is made either.

    Raises:
        ValueError: If ``export`` does not sit under a client's ``process/`` folder, so there is no
            ``superseded/`` to put anything in. Nothing is moved.
    """
    root = client_root(export)
    if root is None:
        raise ValueError(f"{export} is not under a client's process/ folder — nowhere to move to")
    candidates = (selection, archive_path(selection), path_for(selection), export, products)
    present = [p for p in candidates if p.is_file()]
    if not present:
        return []
    folder = unique(root / SUPERSEDED_DIR / f"cleared-{stamp}")
    folder.mkdir(parents=True)
    moved = [Path(shutil.move(str(path), str(unique(folder / path.name)))) for path in present]
    (folder / NOTE_NAME).write_text(_NOTE.format(stamp=stamp), encoding="utf-8")
    return moved
