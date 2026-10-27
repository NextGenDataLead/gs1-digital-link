"""The shell's cached reading of what batch is in force.

The cache is the risky part. Every screen renders this, and it costs two workbook parses plus three
digests, so it has to be cached — but a cache that served a stale answer would reintroduce exactly
the failure this whole change removes: a screen describing a batch that is not the one on disk.

So the key is the files themselves. Nothing has to remember to clear anything.

No NiceGUI: ``ui.context`` imports only ``lib`` and ``ui.REPO_ROOT``.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from lib.batch import Chosen
from lib.config import (
    ClientConfig,
    ExportConfig,
    GS1Config,
    ProcessListConfig,
    QRConfig,
    TemplateConfig,
    WordPressConfig,
)
from lib.input_layout import PROCESS_DIR
from lib.records import LocalisedText, ProductRecord
from ui import context

GTIN_A = "02987653007359"
GTIN_B = "02987653007360"


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    """Module-level and process-lifetime, so one test's batch must not reach the next."""
    context._BATCHES.clear()


def _list(path: Path, gtins: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Artikelnr.", "Barcode"])
    for index, gtin in enumerate(gtins):
        sheet.append([str(1000 + index), gtin])
    workbook.save(path)
    return path


def _config(tmp_path: Path, *, with_list: bool = True) -> ClientConfig:
    """Absolute paths, so ``_resolved`` is a no-op and ``REPO_ROOT`` stays out of it."""
    root = tmp_path / "input" / "acme"
    export = root / PROCESS_DIR / "uploads" / "GS1 export" / "export.xlsx"
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_bytes(b"exported")
    process_list = (
        ProcessListConfig(path=str(root / PROCESS_DIR / "selection" / "selections.xlsx"))
        if with_list
        else None
    )
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
        export=ExportConfig(path=str(export)),
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
        process_list=process_list,
    )


def test_a_client_with_no_selection_has_no_batch_to_describe(tmp_path: Path) -> None:
    """No ``process_list`` block plans every product, so there is no selection in force."""
    assert context.batch_in_force(_config(tmp_path, with_list=False)) is None


def test_the_batch_is_read_from_disk_with_nothing_uploaded_this_process(tmp_path: Path) -> None:
    """The reversal, at the layer the screens call."""
    cfg = _config(tmp_path)
    assert cfg.process_list is not None
    _list(Path(cfg.process_list.path), [GTIN_A, GTIN_B])

    batch = context.batch_in_force(cfg)

    assert batch is not None
    assert batch.ready
    assert batch.selection.rows == 2
    assert batch.chosen_against is Chosen.NOT_RECORDED


def test_the_same_files_are_answered_from_cache(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert cfg.process_list is not None
    _list(Path(cfg.process_list.path), [GTIN_A])

    first = context.batch_in_force(cfg)

    assert context.batch_in_force(cfg) is first, "two parses per screen render, for no new answer"


def test_saving_a_new_selection_invalidates_the_cache(tmp_path: Path) -> None:
    """The failure a cache would reintroduce: a screen describing the previous batch."""
    # Arrange
    cfg = _config(tmp_path)
    assert cfg.process_list is not None
    selection = Path(cfg.process_list.path)
    _list(selection, [GTIN_A, GTIN_B])
    before = context.batch_in_force(cfg)
    assert before is not None and before.selection.rows == 2

    # Act: the operator unticks a row and saves.
    _list(selection, [GTIN_A])
    after = context.batch_in_force(cfg)

    # Assert
    assert after is not None
    assert after.selection.rows == 1, "the cache outlived the file it described"


def test_a_new_export_invalidates_the_cache_too(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert cfg.process_list is not None
    _list(Path(cfg.process_list.path), [GTIN_A])
    first = context.batch_in_force(cfg)

    Path(cfg.export.path).write_bytes(b"a different export entirely")

    assert context.batch_in_force(cfg) is not first


def test_two_clients_do_not_share_one_cached_batch(tmp_path: Path) -> None:
    """Keyed by the live selection's path.

    The repo is one client per checkout, but nothing forces that.
    """
    first = _config(tmp_path / "one")
    second = _config(tmp_path / "two")
    assert first.process_list is not None and second.process_list is not None
    _list(Path(first.process_list.path), [GTIN_A])
    _list(Path(second.process_list.path), [GTIN_A, GTIN_B])

    assert context.batch_in_force(first).selection.rows == 1  # type: ignore[union-attr]
    assert context.batch_in_force(second).selection.rows == 2  # type: ignore[union-attr]


# --- the batch as a scope, for Content's review ------------------------------------------


def test_the_review_scope_is_the_saved_batch_and_not_the_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passed nothing, the review showed every product the results file had ever held.

    On the e2e run that was text for 6 products of an earlier batch, under a batch of 3 that had
    none yet — the section looked filled in when this batch's text did not exist.
    """
    cfg = _config(tmp_path)
    assert cfg.process_list is not None
    _list(Path(cfg.process_list.path), [GTIN_A])
    catalogue = [
        ProductRecord(gtin=gtin, brand="Acme", product_name=LocalisedText(values={"nl": "x"}))
        for gtin in (GTIN_A, GTIN_B)
    ]
    monkeypatch.setattr(context, "load_products", lambda cid: catalogue)

    scope = context.batch_scope("acme", cfg)

    assert scope is not None
    assert scope.gtins == frozenset({GTIN_A})
    assert context.split_results({GTIN_B: {"nl": {}}}, scope).in_scope == {}


def test_no_list_on_disk_is_no_scope_rather_than_everything(tmp_path: Path) -> None:
    assert context.batch_scope("acme", _config(tmp_path)) is None
