"""How the shell may be served. No NiceGUI here, so the main CI job can test it.

``--container`` listens on every interface and relies on the image's port mapping
(``127.0.0.1:8477`` in ``compose.yml``) to stay on the operator's machine. On a workstation there
is no mapping, and the shell would answer the whole office network — so it refuses to start there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Files the container runtimes create in every container: Docker's, then Podman's.
CONTAINER_MARKERS: Final = (Path("/.dockerenv"), Path("/run/.containerenv"))


def inside_container(markers: tuple[Path, ...] = CONTAINER_MARKERS) -> bool:
    """Whether this process runs in a container — the only place ``--container`` is safe."""
    return any(marker.exists() for marker in markers)
