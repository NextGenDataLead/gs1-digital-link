"""Tests for ui/env_edit.py — setting credentials without ever reading one back.

Every test writes to a temporary file. None of them may touch the repository's real ``.env``:
it holds production credentials and all four variables the staging guards gate on.

The properties asserted here are the ones that make a credential form safe rather than merely
convenient — values are quoted so a WordPress application password survives its own spaces, an
empty box means "leave this alone" rather than "clear this", the file's comments survive, and the
result is mode 600.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from ui.env_edit import APP_PASSWORD_GROUPS, describe, write_values

EXISTING = """\
# Copy to .env and fill in, then `chmod 600 .env`.
# The value MUST be single-quoted — an unquoted one truncates at the first space.
ACME_WP_APP_PASS='abcd EFGH ijkl MNOP qrst UVWX'

ACME_GS1_CLIENT_ID=already-set
ACME_GS1_CLIENT_SECRET=
"""


def _env(tmp_path: Path, text: str = EXISTING) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# --- Describing, without disclosing -------------------------------------------


def test_a_set_variable_is_reported_present_with_its_group_count(tmp_path: Path) -> None:
    known = describe(["ACME_WP_APP_PASS"], _env(tmp_path))

    secret = known["ACME_WP_APP_PASS"]
    assert secret.present
    assert secret.groups == APP_PASSWORD_GROUPS
    assert not secret.looks_truncated


def test_a_truncated_application_password_is_recognisable_without_showing_it(
    tmp_path: Path,
) -> None:
    """The commonest credential failure here: a value that lost its quotes and stops at a space."""
    path = _env(tmp_path, "ACME_WP_APP_PASS=abcd EFGH ijkl\n")

    secret = describe(["ACME_WP_APP_PASS"], path)["ACME_WP_APP_PASS"]

    assert secret.present
    assert secret.looks_truncated


def test_an_empty_value_is_reported_absent(tmp_path: Path) -> None:
    """Set-but-empty and never-set are the same failure to an operator, and the same later error."""
    known = describe(["ACME_GS1_CLIENT_SECRET", "ACME_NOT_MENTIONED"], _env(tmp_path))

    assert not known["ACME_GS1_CLIENT_SECRET"].present
    assert not known["ACME_NOT_MENTIONED"].present


def test_a_secret_carries_no_value_field() -> None:
    """The type itself must not be able to leak one — a form renders what it is given."""
    from ui.env_edit import Secret  # noqa: PLC0415 — the point is the type's own shape

    assert "value" not in Secret.__dataclass_fields__


def test_describing_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    known = describe(["ACME_WP_APP_PASS"], tmp_path / "absent.env")

    assert not known["ACME_WP_APP_PASS"].present


# --- Writing ------------------------------------------------------------------


def test_a_new_value_replaces_the_old_one_in_place(tmp_path: Path) -> None:
    path = _env(tmp_path)

    write_values({"ACME_GS1_CLIENT_ID": "rotated"}, path)

    assert "ACME_GS1_CLIENT_ID='rotated'" in path.read_text(encoding="utf-8")
    assert "already-set" not in path.read_text(encoding="utf-8")


def test_the_files_comments_survive_a_write(tmp_path: Path) -> None:
    """They document the quoting rule and the staging guards — losing them costs the next reader."""
    path = _env(tmp_path)

    write_values({"ACME_GS1_CLIENT_ID": "rotated"}, path)

    assert "an unquoted one truncates at the first space" in path.read_text(encoding="utf-8")


def test_a_value_with_spaces_is_quoted(tmp_path: Path) -> None:
    path = _env(tmp_path)

    write_values({"ACME_WP_APP_PASS": "aaaa BBBB cccc DDDD eeee FFFF"}, path)

    assert "ACME_WP_APP_PASS='aaaa BBBB cccc DDDD eeee FFFF'" in path.read_text(encoding="utf-8")
    assert describe(["ACME_WP_APP_PASS"], path)["ACME_WP_APP_PASS"].groups == APP_PASSWORD_GROUPS


def test_an_empty_box_leaves_that_credential_alone(tmp_path: Path) -> None:
    """The fields are write-only, so a blank one means 'unchanged' — never 'clear it'.

    Otherwise saving an unrelated field would erase the three credentials whose boxes the
    operator could not see and so did not fill in.
    """
    path = _env(tmp_path)

    write_values({"ACME_WP_APP_PASS": "", "ACME_GS1_CLIENT_ID": "  "}, path)

    text = path.read_text(encoding="utf-8")
    assert "ACME_WP_APP_PASS='abcd EFGH ijkl MNOP qrst UVWX'" in text
    assert "ACME_GS1_CLIENT_ID=already-set" in text


def test_an_unmentioned_variable_is_appended_under_a_header(tmp_path: Path) -> None:
    path = _env(tmp_path)

    write_values({"ACME_GS1_SANDBOX_ID": "new"}, path)

    text = path.read_text(encoding="utf-8")
    assert "Added by the operator shell" in text
    assert text.rstrip().endswith("ACME_GS1_SANDBOX_ID='new'")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits to check")
def test_the_previous_version_is_kept_and_both_files_are_private(tmp_path: Path) -> None:
    path = _env(tmp_path)

    backup = write_values({"ACME_GS1_CLIENT_ID": "rotated"}, path)

    assert backup.read_text(encoding="utf-8") == EXISTING
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no owner-only mode bits to check")
def test_writing_creates_the_file_when_there_is_none(tmp_path: Path) -> None:
    path = tmp_path / ".env"

    write_values({"ACME_WP_APP_PASS": "aaaa BBBB"}, path)

    assert describe(["ACME_WP_APP_PASS"], path)["ACME_WP_APP_PASS"].present
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_value_containing_a_quote_survives_the_round_trip(tmp_path: Path) -> None:
    path = _env(tmp_path)

    write_values({"ACME_GS1_CLIENT_SECRET": "it's fine"}, path)

    line = next(
        x
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.startswith("ACME_GS1_CLIENT_S")
    )
    assert line == 'ACME_GS1_CLIENT_SECRET="it\'s fine"'
    assert describe(["ACME_GS1_CLIENT_SECRET"], path)["ACME_GS1_CLIENT_SECRET"].present
