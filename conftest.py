"""Pytest configuration.

The presence of this file at the repository root puts the root on ``sys.path``
so tests can use absolute imports (``from lib... import ...``) without requiring
an editable install.
"""

import os

# A maintainer who exports GS1_DATA_DIR for the shell would otherwise point every module-level
# default — ``.env``, ``clients.yml`` — at a real installation's data for the whole test run.
# Dropped here, before any test module imports ``lib``, so the suite always sees the repository.
os.environ.pop("GS1_DATA_DIR", None)
