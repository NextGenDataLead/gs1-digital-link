"""The local operator shell: a desktop window over the same CLIs a person would type.

Run it with ``python -m ui`` after ``pip install -e ".[ui]"``.

Three design decisions shape everything here, and each is load-bearing.

**The UI subprocesses the scripts; it never imports their ``main()``.** ``load_env()`` lives in
each script's ``if __name__ == "__main__":`` block by design — nine test modules call ``main()``
directly, and ``.env`` carries the four staging-guard variables, so loading it in the test path
would arm tests that write to live WordPress and the GS1 production resolver. A shell that
imported ``main()`` would therefore have **no credentials**; one that called ``load_env()`` itself
would re-arm that hazard inside a long-lived process. Subprocessing sidesteps both, and inherits
the production guard, the ``state.json`` writes, the run JSONL and the ``--only links``
target-serves refusal unchanged — all of which live in ``scripts/``, not ``lib/``.

It also means the UI runs exactly the command a human would, so ``docs/verifying-live.md``, the
skills and the terminal all stay valid as a fallback when something here is wrong.

**Whether this machine reaches Anthropic is decided by one variable, and the default is no.**
With the client's ``generator.api_key_env`` unset, the shell holds no LLM credential and has no
Anthropic egress at all: copy is written on the maintainer's machine in a Claude Code session with
the ``content-generator`` skill, and ``generation_results.json`` reaches this machine as a file
(in a handover pack). A page already live keeps its text without one. Set
that variable and the Content screen offers generation itself, so the shell becomes one access
point from dataset to pages instead of a surface with a hole in the middle.

**The key never enters this process either way.** Generating runs the same subprocess as every
other step, and the child loads ``.env`` in its own ``__main__`` block — which is the first
decision above, not an exception to it. ``tests/ui/test_llm_free_shell.py`` walks the AST of this
package to keep the API call out of here. See ``docs/ui-operator-shell.md``.

**The gates come from** :mod:`lib.gates`, **not from this package.** They are the safety
mechanism, they are also written as prose in ``flow-orchestrator/SKILL.md``, and two
implementations of one safety contract drift silently. A test asserts the two agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from lib.data_dir import CODE_ROOT, data_root

#: The repository root — where the code is. Nothing the operator owns is resolved against it.
REPO_ROOT: Final[Path] = CODE_ROOT

#: The data folder (:mod:`lib.data_dir`): ``.env``, ``clients.yml``, ``input/``, ``output/``.
#: Every output path in this project is built as ``Path("output") / …``, relative to the working
#: directory, so every subprocess is launched from here and every configured path resolves here.
#: The repository root unless ``GS1_DATA_DIR`` names another folder.
DATA_ROOT: Final[Path] = data_root()

__all__ = ["DATA_ROOT", "REPO_ROOT"]
