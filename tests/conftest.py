"""Session-wide guard: the suite must not write into the operator's real ``output/``.

Two tests called ``parse_export.main(["acme", …])`` without ``monkeypatch.chdir(tmp_path)``, and
the script writes its source-issues report to ``Path("output") / client_id`` — relative to the
current directory. So a plain ``pytest`` created ``output/acme/`` in the working tree, beside the
live client's state, plan and report. Harmless in itself and gitignored, which is exactly why it
sat there unnoticed until someone asked what had been written.

The guard is here rather than in a single test because the failure mode is *any* script driven
without a temp cwd, and it costs one directory listing per session.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

#: The real output tree, resolved once from this file so a test's own ``chdir`` cannot move it.
_OUTPUT: Path = Path(__file__).resolve().parent.parent / "output"


@pytest.fixture(scope="session", autouse=True)
def _no_writes_to_the_real_output_tree() -> Iterator[None]:
    before = {p.name for p in _OUTPUT.iterdir()} if _OUTPUT.is_dir() else set()
    yield
    after = {p.name for p in _OUTPUT.iterdir()} if _OUTPUT.is_dir() else set()
    leaked = sorted(after - before)
    assert not leaked, (
        f"the suite created {leaked} under {_OUTPUT}/ — a script was driven without "
        f"`monkeypatch.chdir(tmp_path)`, so it wrote beside the live client's artifacts"
    )
