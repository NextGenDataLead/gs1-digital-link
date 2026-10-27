"""Smoke tests for the repository skeleton.

These verify the packages are importable and the test toolchain is wired up.
Real coverage arrives per phase (see ``docs/IMPLEMENTATION_SPEC.md`` §11).
"""

import lib
import scripts


def test_lib_package_importable() -> None:
    """The shared library package imports cleanly."""
    assert lib.__name__ == "lib"


def test_scripts_package_importable() -> None:
    """The scripts package imports cleanly."""
    assert scripts.__name__ == "scripts"
