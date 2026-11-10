"""Tests for ``scripts/check_languages.py`` and the comparison step ``run_generate`` now runs.

The Messages API is mocked (``pytest-httpx``). What is pinned: a judged product is not sent again
while its values are unchanged, ``--dry-run`` calls nothing, a client without a generator cannot
run it, and in ``run_generate`` the comparison runs after the copy — and its failure is reported
without failing the copy that was already written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from lib.config import ClientConfig, ExportConfig, GeneratorConfig, GS1Config, WordPressConfig
from lib.gdsn import GdsnSource
from lib.language_check import VERDICT_DIFFERS, load_checks
from lib.records import LocalisedText, ProductRecord
from scripts import check_languages, run_generate

GTIN = "08713195006109"
_KEY = "TEST_KEY"
_CHECK_PATH = Path("output/democlient/data/language_check.json")


def _config(*, generator: bool = True) -> ClientConfig:
    return ClientConfig(
        client_id="democlient",
        display_name="Democlient",
        gs1=GS1Config(
            account_number_test="8712345000010",
            client_id_env_test="GS1_CID",
            client_secret_env_test="GS1_SEC",
        ),
        export=ExportConfig(
            format="gdsn",
            path="input/democlient.xlsx",
            gdsn_map={
                "description_short": GdsnSource(
                    sheet="MarketingInformation", attribute="1083", localised=True
                )
            },
        ),
        wordpress=WordPressConfig(
            site_url="https://wp.test",
            username="bot",
            app_password_env="WP_PASS",
            default_language="nl",
            languages=["nl", "fr"],
            slug_pattern="p-{gtin}",
            target_url_pattern="{site_url}/{lang_segment}{post_type}/{slug}/",
        ),
        generator=(
            GeneratorConfig(enabled=True, model="claude-sonnet-5", api_key_env=_KEY)
            if generator
            else None
        ),
    )


def _write_products(fr: str = "Brosse pour vos cheveux") -> None:
    product = ProductRecord(
        gtin=GTIN,
        brand="Noviplast",
        product_name=LocalisedText(values={"nl": "Pet spa", "fr": "Pet spa"}),
        net_content="1 H87",
        description_short=LocalisedText(values={"nl": "Borstel voor je huisdier", "fr": fr}),
    )
    path = Path("output/democlient/data/products.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([product.model_dump(mode="json")]), encoding="utf-8")


def _check_answer() -> dict[str, Any]:
    finding = {
        "field": "description_short",
        "language": "fr",
        "verdict": "differs",
        "note": "Human hair, not pets.",
    }
    return {
        "content": [
            {
                "type": "tool_use",
                "id": "t",
                "name": "report_language_disagreements",
                "input": {"findings": [finding]},
            }
        ]
    }


def _copy_answer() -> dict[str, Any]:
    return {
        "content": [
            {"type": "tool_use", "id": "t", "name": "produce_copy", "input": {"usps": ["Tagline"]}}
        ]
    }


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_KEY, "sk-test")


# --- the command --------------------------------------------------------------


@pytest.mark.usefixtures("_env")
def test_a_judged_product_is_not_sent_again_while_its_values_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config())
    _write_products()
    httpx_mock.add_response(json=_check_answer())

    assert check_languages.main([]) == 0
    assert check_languages.main([]) == 0

    assert len(httpx_mock.get_requests()) == 1
    checks = load_checks(_CHECK_PATH)
    assert checks is not None
    assert [f.verdict for f in checks.results[0].findings] == [VERDICT_DIFFERS]
    assert checks.provenance == "api:claude-sonnet-5"


@pytest.mark.usefixtures("_env")
def test_an_edited_value_is_judged_again(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config())
    httpx_mock.add_response(json=_check_answer(), is_reusable=True)
    _write_products()
    check_languages.main([])

    _write_products(fr="Brosse pour votre animal")
    check_languages.main([])

    assert len(httpx_mock.get_requests()) == 2  # noqa: PLR2004 — once per version of the value


@pytest.mark.usefixtures("_env")
def test_dry_run_counts_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config())
    _write_products()

    assert check_languages.main(["--dry-run"]) == 0

    assert "would compare 1 product(s), keep 0" in capsys.readouterr().err
    assert not _CHECK_PATH.exists()


@pytest.mark.usefixtures("_env")
def test_without_a_generator_block_it_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config(generator=False))
    _write_products()

    assert check_languages.main([]) == 2  # noqa: PLR2004 — the config-error exit code
    assert "generator" in capsys.readouterr().err


# --- inside run_generate --------------------------------------------------------


def _write_voice() -> None:
    path = Path("prompts/democlient/generation.v1.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("voice", encoding="utf-8")


def _route(*, check_status: int = 200) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        tool = json.loads(request.content)["tool_choice"]["name"]
        if tool == "report_language_disagreements":
            return httpx.Response(check_status, json=_check_answer())
        return httpx.Response(200, json=_copy_answer())

    return respond


@pytest.mark.usefixtures("_env")
def test_generating_through_the_api_also_compares_the_languages(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_generate, "get_client", lambda _cid: _config())
    _write_products()
    _write_voice()
    httpx_mock.add_callback(_route(), is_reusable=True)

    assert run_generate.main(["democlient", "--backend", "api"]) == 0

    assert "compared languages for 1 product(s), kept 0" in capsys.readouterr().err
    assert load_checks(_CHECK_PATH) is not None


@pytest.mark.usefixtures("_env")
def test_a_failed_comparison_is_reported_and_does_not_fail_the_written_copy(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_generate, "get_client", lambda _cid: _config())
    _write_products()
    _write_voice()
    httpx_mock.add_callback(_route(check_status=400), is_reusable=True)

    assert run_generate.main(["democlient", "--backend", "api"]) == 0

    assert "1 NOT compared" in capsys.readouterr().err
    assert Path("output/democlient/data/generation_results.json").exists()
    assert load_checks(_CHECK_PATH) is None  # asked again next run, not recorded as "all agree"


@pytest.mark.usefixtures("_env")
def test_without_a_key_nothing_is_compared_and_the_copy_still_stands(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_generate, "get_client", lambda _cid: _config())
    _write_products()
    _write_voice()
    httpx_mock.add_callback(_route(), is_reusable=True)
    real = run_generate.run_check

    def keyless(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.delenv(_KEY)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_generate, "run_check", keyless)

    assert run_generate.main(["democlient", "--backend", "api"]) == 0

    assert "languages NOT compared" in capsys.readouterr().err
    assert not _CHECK_PATH.exists()


@pytest.mark.usefixtures("_env")
def test_a_failed_call_is_named_and_the_command_exits_1(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config())
    _write_products()
    httpx_mock.add_response(status_code=400, json={"error": "bad"})

    assert check_languages.main([]) == 1

    assert f"NOT compared {GTIN}" in capsys.readouterr().err


@pytest.mark.usefixtures("_env")
def test_all_reaches_products_off_the_product_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_config`` names no process list, so ``in_scope`` keeps nothing; ``--all`` keeps all."""
    monkeypatch.setattr(check_languages, "in_scope", lambda _cfg, _products: [])
    monkeypatch.setattr(check_languages, "get_client", lambda _cid: _config())
    _write_products()

    check_languages.main(["--dry-run"])
    check_languages.main(["--dry-run", "--all"])

    err = capsys.readouterr().err
    assert "would compare 0 product(s)" in err
    assert "would compare 1 product(s)" in err


@pytest.mark.usefixtures("_env")
def test_an_unexpected_error_in_the_comparison_does_not_fail_the_written_copy(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_generate, "get_client", lambda _cid: _config())
    _write_products()
    _write_voice()
    httpx_mock.add_callback(_route(), is_reusable=True)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("language_check.json is open in another program")

    monkeypatch.setattr(run_generate, "run_check", broken)

    assert run_generate.main(["democlient", "--backend", "api"]) == 0

    assert "languages NOT compared (language_check.json is open" in capsys.readouterr().err
    assert Path("output/democlient/data/generation_results.json").exists()
