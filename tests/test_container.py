"""The container image: code in the image, an installation's data in the ``/data`` volume.

Two failures here would be expensive and silent. A credential or a ledger copied into an image
layer cannot be taken back out of a pushed image; and a shell published on more than loopback is
a production publishing tool on the office network. Both are checked statically, since the suite
runs without Docker. CI's container job builds the image and starts it for real.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from ui.serving import inside_container

_ROOT: Final = Path(__file__).resolve().parent.parent
_DOCKERFILE: Final = _ROOT / "Dockerfile"
_DOCKERIGNORE: Final = _ROOT / ".dockerignore"
_COMPOSE: Final = _ROOT / "compose.yml"
_ENTRYPOINT: Final = _ROOT / "docker" / "entrypoint.sh"
_WORKFLOW: Final = _ROOT / ".github" / "workflows" / "container.yml"

#: Everything an installation owns. None of it may enter the build context.
_DATA_PATTERNS: Final = (".env", ".env.*", "clients.yml", "clients.yml.*", "input/", "output/")

#: What the code reads from beside itself (``CODE_ROOT``), plus the code.
_SHIPPED_DIRS: Final = ("lib", "scripts", "ui", "schema", "reference", "templates", "prompts")


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _instructions(keyword: str) -> list[str]:
    """Top-level instructions only — ``HEALTHCHECK``'s indented ``CMD`` is not the image's."""
    return [
        line.split(None, 1)[1]
        for line in _dockerfile().splitlines()
        if line[:1].strip() and line.split(None, 1)[:1] == [keyword]
    ]


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return loaded


# --- nothing an installation owns goes into the image ----------------------------------------


def test_the_build_context_excludes_every_data_path() -> None:
    lines = {line.strip() for line in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    missing = [pattern for pattern in _DATA_PATTERNS if pattern not in lines]
    assert not missing, f".dockerignore lets data into the image: {missing}"
    assert "!.env.example" in lines, "the example is what a new data folder is seeded from"


def test_the_image_never_copies_the_whole_context() -> None:
    """A ``COPY . .`` would take whatever .dockerignore forgets — the next secret file included."""
    for source in _instructions("COPY"):
        assert not re.match(r"\.\s", source), f"copies the whole build context: COPY {source}"


def test_the_image_ships_everything_the_code_reads_beside_itself() -> None:
    copied = " ".join(_instructions("COPY"))
    missing = [name for name in _SHIPPED_DIRS if f"{name}/" not in copied]
    assert not missing, f"the image would run without: {missing}"


def test_the_image_keeps_its_data_in_the_volume_and_runs_as_a_user() -> None:
    assert "GS1_DATA_DIR=/data" in _dockerfile()
    assert any("/data" in volume for volume in _instructions("VOLUME"))
    users = _instructions("USER")
    assert users, "the image runs as root"
    assert users[-1].strip() not in {"root", "0"}, "the image runs as root"


def test_the_image_starts_the_shell_in_container_mode() -> None:
    (command,) = _instructions("CMD")
    assert '"--container"' in command and '"ui"' in command


def test_the_code_is_installed_editable() -> None:
    """``CODE_ROOT`` is ``lib/``'s parent: in site-packages it would not find ``schema/``."""
    assert "--no-editable" not in _dockerfile()


# --- reachable from this machine only ----------------------------------------------------------


def test_compose_publishes_the_shell_on_loopback_only() -> None:
    for name, service in _compose()["services"].items():
        for mapping in service.get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} publishes {mapping!r} beyond this machine"
            )


def test_compose_mounts_the_data_folder() -> None:
    (service,) = _compose()["services"].values()
    assert any(str(volume).endswith(":/data") for volume in service["volumes"])


def test_compose_pulls_the_image_ci_publishes() -> None:
    (service,) = _compose()["services"].values()
    image = service["image"].split(":", 1)[0]
    assert image in _WORKFLOW.read_text(encoding="utf-8"), (
        f"compose.yml pulls {image}, which the container workflow does not publish"
    )


# --- the entrypoint seeds a new data folder and never touches an existing one -------------------


@pytest.fixture
def code_dir(tmp_path: Path) -> Path:
    code = tmp_path / "code"
    code.mkdir()
    (code / "clients.example.yml").write_text("example: config\n", encoding="utf-8")
    (code / ".env.example").write_text("EXAMPLE=\n", encoding="utf-8")
    return code


def _entrypoint(data: Path, code: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GS1_DATA_DIR": str(data), "GS1_CODE_DIR": str(code)}
    return subprocess.run(  # noqa: S603 — fixed argv
        ["sh", str(_ENTRYPOINT), *argv], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits to check")
def test_a_new_data_folder_is_seeded_from_the_examples(tmp_path: Path, code_dir: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    result = _entrypoint(data, code_dir, "echo", "started")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("started"), "the command runs after seeding"
    assert (data / "clients.yml").read_text(encoding="utf-8") == "example: config\n"
    assert (data / ".env").read_text(encoding="utf-8") == "EXAMPLE=\n"
    assert stat.S_IMODE((data / ".env").stat().st_mode) == 0o600, ".env is owner-only"
    assert (data / "input").is_dir() and (data / "output").is_dir()


def test_an_existing_data_folder_is_left_exactly_as_it_is(tmp_path: Path, code_dir: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "clients.yml").write_text("theirs\n", encoding="utf-8")
    (data / ".env").write_text("SECRET=theirs\n", encoding="utf-8")

    result = _entrypoint(data, code_dir, "true")

    assert result.returncode == 0, result.stderr
    assert (data / "clients.yml").read_text(encoding="utf-8") == "theirs\n"
    assert (data / ".env").read_text(encoding="utf-8") == "SECRET=theirs\n"
    assert "New data folder" not in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32", reason="the container is Linux; Windows has no euid or mode bits"
)
def test_an_unwritable_data_folder_stops_with_a_reason(tmp_path: Path, code_dir: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    data = tmp_path / "data"
    data.mkdir(mode=0o500)
    try:
        result = _entrypoint(data, code_dir, "true")
    finally:
        data.chmod(0o700)
    assert result.returncode == 1
    assert "not writable" in result.stderr


# --- --container refuses to run outside a container ---------------------------------------------


def test_container_mode_is_recognised_inside_a_container(tmp_path: Path) -> None:
    marker = tmp_path / ".dockerenv"
    marker.touch()
    assert inside_container((tmp_path / "absent", marker))


def test_container_mode_refuses_a_workstation(tmp_path: Path) -> None:
    """Outside a container nothing maps the port to loopback: the shell would face the network."""
    assert not inside_container((tmp_path / ".dockerenv", tmp_path / ".containerenv"))
