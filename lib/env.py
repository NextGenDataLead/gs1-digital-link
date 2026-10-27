"""Load ``.env`` into the process environment for command-line entry points.

``.env`` in the data folder (the repository root by default) is the single source of truth for
credentials (OD-1, ``docs/OPEN_DECISIONS.md``). Nothing used to load it: the secrets reached the
code only because Claude Code injected an ``env`` block from ``~/.claude/settings.json`` into
every command it ran, which meant a script run from a plain terminal could not reach GS1 at all.

``override=False`` keeps real environment variables winning over the file, so CI and any
deliberate one-off override still work.

**Call this from the ``if __name__ == "__main__":`` block of ``scripts/*.py`` — not from
``main()``.** Never at ``lib/`` import time either: a library must not have import side
effects.

The distinction between the ``__main__`` block and ``main()`` is load-bearing, not stylistic.
``.env`` carries all four variables that the staging guards gate on (``WP_STAGING_URL``,
``WP_STAGING_USER``, ``DEMOCLIENT_WP_APP_PASS``, ``STAGING_GTIN``), so anything that loads it
inside the test path arms tests that write to the live WordPress site and the GS1 production
resolver — the failure ``addopts = "-m 'not staging'"`` exists to prevent. Nine test modules
under ``tests/scripts/`` call ``main()`` directly, so a call sited there would load
production credentials into the pytest process on every plain ``pytest`` run. Sited in the
``__main__`` block, ``python -m scripts.run_plan`` loads ``.env`` and the tests never do.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

from lib.data_dir import data_root

_log = logging.getLogger(__name__)

#: ``.env`` in the data folder (:mod:`lib.data_dir` — the repository root unless ``GS1_DATA_DIR``
#: says otherwise), resolved absolutely so a script behaves the same however it was invoked.
ENV_PATH: Path = data_root() / ".env"


def load_env() -> bool:
    """Load the data folder's ``.env`` without overriding existing environment variables.

    Returns ``True`` if the file was found and read. A missing ``.env`` is not an error —
    the environment may legitimately be populated another way (CI, an exported shell) — so
    this logs at debug level and returns ``False``, leaving the eventual
    ``MissingCredentialError`` to name the specific variable that is absent.
    """
    if not ENV_PATH.is_file():
        _log.debug("no .env at %s — relying on the ambient environment", ENV_PATH)
        return False
    load_dotenv(ENV_PATH, override=False)
    return True
