"""``python -m ui`` — start the operator shell.

Deliberately does **not** call :func:`lib.env.load_env`. This process holds no credentials: every
command it runs is a subprocess, and each of those loads ``.env`` in its own ``__main__`` block,
which is the arrangement ``tests/lib/test_env.py`` enforces. Loading it here would put production
secrets into a long-lived desktop process for no benefit, and would arm the staging-guard
variables inside it.

It does enter the data folder (:mod:`lib.data_dir`), which holds no secret: the shell calls ``lib``
in-process too, and those calls resolve ``Path("output") / …`` against the working directory.
"""

from __future__ import annotations

import argparse

from lib.data_dir import enter_data_dir
from ui.app import main
from ui.serving import inside_container


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ui", description="The GS1 Digital Link operator shell.")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Serve in a browser tab instead of a native window (still loopback-only)",
    )
    parser.add_argument(
        "--container",
        action="store_true",
        help="Run inside the container image: browser mode, listening for the port mapping",
    )
    return parser.parse_args()


if __name__ in {"__main__", "__mp_main__"}:  # NiceGUI re-imports under this name on some platforms
    _args = _parse_args()
    if _args.container and not inside_container():
        raise SystemExit(
            "--container is for the container image only: it listens on every network interface "
            "and relies on the image's port mapping to stay on this machine. Use --browser here."
        )
    enter_data_dir()
    main(native=not _args.browser, container=_args.container)
