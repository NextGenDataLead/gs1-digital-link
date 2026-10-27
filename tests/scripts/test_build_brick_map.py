"""Tests for scripts/build_brick_map.py (Phase 7.5).

The script is read-only orchestration over ``lib.categories``: it drives ``main`` with a
faked ``get_client`` and a temp working directory, asserting the printed draft, the coverage
gate's exit codes, and the usage errors. Resolution/coverage logic itself is covered in
``tests/lib/test_categories.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from lib.config import (
    CategoryConfig,
    ClientConfig,
    ExportConfig,
    GS1Config,
    WordPressConfig,
)
from lib.records import LocalisedText, ProductRecord
from scripts import build_brick_map


def _make_config(categories: CategoryConfig | None = None) -> ClientConfig:
    return ClientConfig(
        client_id="acme",
        display_name="Acme BV",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(path="input/acme.xlsx"),
        wordpress=WordPressConfig(
            site_url="https://wp.test", username="bot", app_password_env="WP_PASS"
        ),
        categories=categories,
    )


def _product(gtin: str, brick: str | None) -> ProductRecord:
    return ProductRecord(
        gtin=gtin,
        brand="Acme",
        product_name=LocalisedText(values={"nl": "Rugsteun"}),
        gpc_brick_code=brick,
    )


def _write_products(path: Path, products: list[ProductRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([p.model_dump(mode="json") for p in products]), encoding="utf-8")


def _patch_client(monkeypatch: pytest.MonkeyPatch, cfg: ClientConfig) -> None:
    monkeypatch.setattr(build_brick_map, "get_client", lambda _cid: cfg)


def _write_datamodel(tmp_path: Path, rows: list[list[Any]]) -> str:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Brick", "Sector"])
    for row in rows:
        sheet.append(row)
    path = tmp_path / "diy.xlsx"
    workbook.save(path)
    return str(path)


# --- Draft mode --------------------------------------------------------------


def test_draft_lists_every_brick_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(
        products, [_product("02987653000001", "10003865"), _product("02987653000002", "10006459")]
    )

    code = build_brick_map.main(["acme", "--products", str(products)])

    out = capsys.readouterr().out
    assert code == 0
    assert "categories:" in out
    assert '"10003865": ""' in out
    assert '"10006459": ""' in out


def test_draft_with_datamodel_annotates_sector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product("02987653000001", "10003865")])
    datamodel = _write_datamodel(tmp_path, [["10003865", "Garden"]])

    code = build_brick_map.main(
        [
            "acme",
            "--products",
            str(products),
            "--datamodel",
            datamodel,
            "--code-column",
            "Brick",
            "--category-column",
            "Sector",
        ]
    )

    assert code == 0
    assert "Garden" in capsys.readouterr().out


def test_datamodel_without_columns_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config())
    products = tmp_path / "products.json"
    _write_products(products, [_product("02987653000001", "10003865")])

    code = build_brick_map.main(["acme", "--products", str(products), "--datamodel", "x.xlsx"])

    assert code == 2
    assert "requires --code-column" in capsys.readouterr().err


# --- Check mode --------------------------------------------------------------


def test_check_exits_1_when_a_brick_is_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    categories = CategoryConfig(terms=["tuin"], brick_category_map={"10003865": "tuin"})
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(categories))
    products = tmp_path / "products.json"
    _write_products(
        products, [_product("02987653000001", "10003865"), _product("02987653000002", "99999999")]
    )

    code = build_brick_map.main(["acme", "--products", str(products), "--check"])

    assert code == 1
    assert "UNMAPPED 99999999" in capsys.readouterr().err


def test_check_exits_0_when_fully_covered_including_override_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    categories = CategoryConfig(
        terms=["tuin", "keuken"],
        brick_category_map={"10003865": "tuin"},
        overrides={"02987653000002": "keuken"},  # covers the otherwise-unmapped brick
    )
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(categories))
    products = tmp_path / "products.json"
    _write_products(
        products, [_product("02987653000001", "10003865"), _product("02987653000002", "77777777")]
    )

    assert build_brick_map.main(["acme", "--products", str(products), "--check"]) == 0


def test_check_without_categories_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_client(monkeypatch, _make_config(categories=None))
    products = tmp_path / "products.json"
    _write_products(products, [_product("02987653000001", "10003865")])

    code = build_brick_map.main(["acme", "--products", str(products), "--check"])

    assert code == 2
    assert "no categories config" in capsys.readouterr().err
