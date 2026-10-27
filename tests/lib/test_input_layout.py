"""Where every document in ``input/`` lives, and the note that explains it in the folder.

This module had no tests at all while three consumers each carried their own spelling of these
paths — and two of those spellings were wrong for a week, silently, because both sites treat a
missing file as "this client never uploaded one". The paths are the contract now, so they are
asserted here rather than inferred from whichever screen last wrote one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    ProcessListConfig,
    QRConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.input_layout import (
    PROCESS_DIR,
    README_NAME,
    archive_path,
    client_root,
    ensure_uploads_dir,
    export_archive_path,
    export_archives,
    list_archives,
    selection_archives,
    selections_path,
    unique,
    uploads_dir,
    uploads_path,
    write_readme,
)


@pytest.fixture
def client(tmp_path: Path) -> Path:
    """A client folder in the shipped layout, with both live files present."""
    root = tmp_path / "input" / "acme"
    (root / PROCESS_DIR / "selection").mkdir(parents=True)
    (root / PROCESS_DIR / "uploads" / "GS1 export").mkdir(parents=True)
    (root / PROCESS_DIR / "selection" / "selections.xlsx").write_bytes(b"chosen")
    (root / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx").write_bytes(b"exported")
    return root


def _control(client: Path) -> Path:
    return client / PROCESS_DIR / "selection" / "selections.xlsx"


def _export(client: Path) -> Path:
    return client / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx"


# --- The selection half ------------------------------------------------------


def test_the_uploaded_list_sits_in_uploads_under_its_own_word(client: Path) -> None:
    """``product-list``, not ``selection`` — a shared name is how the two folders stop differing."""
    assert archive_path(_control(client)) == client / PROCESS_DIR / "uploads" / "product-list.xlsx"


def test_dated_copies_go_beside_the_document_they_are_copies_of(client: Path) -> None:
    control = _control(client)
    assert uploads_path(control, "20260927T101500") == (
        client / PROCESS_DIR / "uploads" / "product-list-20260927T101500.xlsx"
    )
    assert selections_path(control, "20260927T101500") == (
        client / PROCESS_DIR / "selection" / "selection-20260927T101500.xlsx"
    )


def test_the_export_archives_beside_the_live_export_not_one_level_up(client: Path) -> None:
    """Its subfolder is the operator's, space and all.

    ``clients.yml`` names that folder, not this module.
    """
    assert export_archive_path(_export(client), "20260927T101500") == (
        client / PROCESS_DIR / "uploads" / "GS1 export" / "export-20260927T101500.xlsx"
    )


# --- No helper may create anything -------------------------------------------


@pytest.mark.parametrize(
    "ask",
    [
        pytest.param(uploads_dir, id="uploads_dir"),
        pytest.param(archive_path, id="archive_path"),
        pytest.param(lambda c: uploads_path(c, "20260927T101500"), id="uploads_path"),
        pytest.param(lambda c: selections_path(c, "20260927T101500"), id="selections_path"),
    ],
)
def test_asking_where_a_document_lives_creates_nothing(
    tmp_path: Path, ask: Callable[[Path], Path]
) -> None:
    """``scripts/run_execute`` asks exactly this, and a run writes to ``output/`` only.

    A helper that mkdir'd as a side effect would have a run quietly making folders in the
    operator's gitignored input tree — which nothing ever deletes.
    """
    # Arrange: nothing on disk at all, so any directory afterwards was made by the call.
    control = tmp_path / "input" / "acme" / PROCESS_DIR / "selection" / "selections.xlsx"

    # Act
    ask(control)

    # Assert
    assert not tmp_path.joinpath("input").exists(), (
        f"{ask} created part of input/ just by being asked where a file would go"
    )


def test_only_the_writers_make_the_uploads_folder_and_they_do_it_explicitly(
    tmp_path: Path,
) -> None:
    control = tmp_path / "input" / "acme" / PROCESS_DIR / "selection" / "selections.xlsx"
    assert not uploads_dir(control).exists()

    made = ensure_uploads_dir(control)

    assert made.is_dir()
    assert made == uploads_dir(control), "the writer and the reader must name the same folder"


# --- Collisions --------------------------------------------------------------


def test_a_second_write_in_the_same_second_does_not_overwrite_the_first(tmp_path: Path) -> None:
    """Untick a row, glance at the count, untick another — two saves inside one second."""
    first = tmp_path / "selection-20260927T101500.xlsx"
    first.write_bytes(b"one")
    second = unique(first)
    second.write_bytes(b"two")
    third = unique(first)

    assert second.name == "selection-20260927T101500-1.xlsx"
    assert third.name == "selection-20260927T101500-2.xlsx", (
        "the serial must not compound into -1-2; every version is kept under its own name"
    )
    assert first.read_bytes() == b"one"


def test_archives_come_back_newest_first_by_mtime_never_by_name(tmp_path: Path) -> None:
    """``-1`` sorts *before* the unsuffixed name, because ``-`` precedes ``.``.

    So a same-second pair reads the wrong way round in a lexical listing, and "the newest export"
    would be the older of the two.
    """
    # Arrange: the -1 copy is the later write, and sorts first by name.
    folder = tmp_path / "input" / "acme" / PROCESS_DIR / "uploads" / "GS1 export"
    folder.mkdir(parents=True)
    export = folder / "export.xlsx"
    export.write_bytes(b"live")
    earlier = folder / "export-20260927T101500.xlsx"
    later = folder / "export-20260927T101500-1.xlsx"
    earlier.write_bytes(b"earlier")
    later.write_bytes(b"later")
    os.utime(earlier, (1_000_000, 1_000_000))
    os.utime(later, (2_000_000, 2_000_000))

    # Act / Assert
    assert export_archives(export) == [later, earlier]
    assert export not in export_archives(export), "the live file is not one of its own archives"


def test_each_half_lists_only_its_own_documents(client: Path) -> None:
    control = _control(client)
    uploads_path(control, "20260927T101500").write_bytes(b"sent")
    selections_path(control, "20260927T101600").write_bytes(b"chosen")

    assert [p.name for p in list_archives(control)] == ["product-list-20260927T101500.xlsx"]
    assert [p.name for p in selection_archives(control)] == ["selection-20260927T101600.xlsx"]


def test_listing_a_folder_that_is_not_there_is_empty_not_an_error(tmp_path: Path) -> None:
    control = tmp_path / "nowhere" / PROCESS_DIR / "selection" / "selections.xlsx"
    assert list_archives(control) == []
    assert selection_archives(control) == []
    assert export_archives(tmp_path / "nowhere" / "export.xlsx") == []


# --- Finding the client folder -----------------------------------------------


def test_the_client_root_is_found_however_deep_the_document_sits(client: Path) -> None:
    """Counting parents would have broken the day the export gained its own subfolder."""
    assert client_root(_control(client)) == client
    assert client_root(_export(client)) == client, "two levels under process/, and still the root"


def test_a_path_under_no_process_folder_has_no_client_root(tmp_path: Path) -> None:
    assert client_root(tmp_path / "input" / "acme" / "products.xlsx") is None


def test_the_note_is_written_into_the_client_folder(client: Path) -> None:
    cfg = _config(str(_export(client)))

    written = write_readme(cfg)

    assert written == client / README_NAME
    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert "process/selection/selections.xlsx" in text
    assert "never deleted" in text.lower() or "ever deleted" in text.lower()


def test_a_client_outside_the_layout_gets_no_note_rather_than_an_error(tmp_path: Path) -> None:
    """The pre-layout flat shape is still on disk for at least one client."""
    assert write_readme(_config(str(tmp_path / "input" / "acme" / "products.xlsx"))) is None


def _config(export_path: str) -> ClientConfig:
    """A config carrying only what ``write_readme`` reads, which is ``export.path``."""
    return ClientConfig(
        client_id="acme",
        display_name="Acme BV",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
            environment="test",
            digital_link_url_pattern="https://id.gs1.org/01/{gtin14}",
        ),
        export=ExportConfig(path=export_path),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            post_type="product",
            default_language="nl",
            languages=["nl"],
        ),
        template=TemplateConfig(override_dir=None),
        qr=QRConfig(formats=["svg"], size_mm=20, error_correction="M", dpi=300),
        process_list=ProcessListConfig(path="unused"),
    )
