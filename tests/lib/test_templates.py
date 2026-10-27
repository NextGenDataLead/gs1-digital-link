"""Tests for the product template engine (IMPLEMENTATION_SPEC §4.6, §3.4).

Covers the client→default→error resolution order, the §3.4 variable vocabulary and
language resolution, and edges E12 (unknown ``extras`` key) and E13 (data containing
Mustache/HTML that must not re-render or inject).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lib.config import TemplateConfig
from lib.errors import TemplateError
from lib.records import LocalisedText, ProductRecord
from lib.templates import TemplateEngine

CLIENT_ID = "acme"
CLIENT_META = {"display_name": "Acme B.V.", "id": CLIENT_ID, "default_language": "nl"}


def make_product(**overrides: object) -> ProductRecord:
    """Build a representative :class:`ProductRecord`, overriding fields as needed."""
    base: dict[str, object] = {
        "gtin": "12345670",
        "brand": "Acme",
        "product_name": LocalisedText(values={"nl": "Emmer", "fr": "Seau"}),
        "description_short": LocalisedText(values={"nl": "Korte tekst", "fr": "Texte court"}),
        "net_content": "10 L",
        "category": "Buckets",
        "extras": {"functional_name": "Bucket"},
    }
    base.update(overrides)
    return ProductRecord(**base)  # type: ignore[arg-type]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _default_path(base: Path, language: str) -> Path:
    return base / "templates" / "_default" / f"product.{language}.html"


def _client_path(base: Path, client_id: str, language: str) -> Path:
    return base / "templates" / client_id / f"product.{language}.html"


# --- Resolution order (§4.6) -------------------------------------------------


def test_client_template_preferred_over_default(tmp_path: Path) -> None:
    _write(_client_path(tmp_path, CLIENT_ID, "nl"), "CLIENT {{product_name}}")
    _write(_default_path(tmp_path, "nl"), "DEFAULT {{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "CLIENT Emmer"


def test_the_data_folder_template_wins_over_the_release(tmp_path: Path) -> None:
    """A client's templates travel in its handover pack, under ``input/{client}/templates``."""
    data = tmp_path / "data"
    _write(data / "input" / CLIENT_ID / "templates" / "product.nl.html", "DATA {{product_name}}")
    _write(_client_path(tmp_path, CLIENT_ID, "nl"), "RELEASE {{product_name}}")
    _write(_default_path(tmp_path, "nl"), "DEFAULT {{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path, data_root=data)

    assert engine.render(make_product(), "nl", CLIENT_META) == "DATA Emmer"


def test_the_data_folder_honours_the_files_mapping(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write(data / "input" / CLIENT_ID / "templates" / "nl-page.html", "MAPPED {{product_name}}")
    config = TemplateConfig(override_dir="templates/elsewhere", files={"nl": "nl-page.html"})
    engine = TemplateEngine(CLIENT_ID, config, base_dir=tmp_path, data_root=data)

    assert engine.render(make_product(), "nl", CLIENT_META) == "MAPPED Emmer"


def test_the_release_template_is_the_fallback(tmp_path: Path) -> None:
    """An installation that never moved its templates keeps rendering them."""
    _write(_client_path(tmp_path, CLIENT_ID, "nl"), "RELEASE {{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path, data_root=tmp_path / "empty")

    assert engine.render(make_product(), "nl", CLIENT_META) == "RELEASE Emmer"


def test_falls_back_to_default_when_client_absent(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "nl"), "DEFAULT {{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "DEFAULT Emmer"


def test_missing_template_raises_template_error(tmp_path: Path) -> None:
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    with pytest.raises(TemplateError) as exc:
        engine.render(make_product(), "nl", CLIENT_META)

    assert "acme" in str(exc.value)
    assert "nl" in str(exc.value)


def test_override_dir_and_files_mapping(tmp_path: Path) -> None:
    config = TemplateConfig(override_dir="templates/custom", files={"nl": "page.nl.html"})
    _write(tmp_path / "templates" / "custom" / "page.nl.html", "OVERRIDE {{product_name}}")
    engine = TemplateEngine(CLIENT_ID, config, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "OVERRIDE Emmer"


# --- Variable vocabulary + language resolution (§3.4) ------------------------


def test_renders_language_specific_text(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "fr"), "{{product_name}} / {{description_short}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "fr", CLIENT_META)

    assert out == "Seau / Texte court"


def test_language_falls_back_to_default_language(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "fr"), "{{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(product_name=LocalisedText(values={"nl": "Emmer"}))

    out = engine.render(product, "fr", CLIENT_META)

    assert out == "Emmer"


def test_client_meta_and_scalar_fields(tmp_path: Path) -> None:
    _write(
        _default_path(tmp_path, "nl"),
        "{{brand}}|{{net_content}}|{{category}}|{{client.display_name}}|{{client.id}}",
    )
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "Acme|10 L|Buckets|Acme B.V.|acme"


def test_net_content_unit_code_decoded_per_language(tmp_path: Path) -> None:
    # net_content carries a raw GDSN unit code ("H87"); the render decodes it per language.
    product = make_product(net_content="4 H87")
    _write(_default_path(tmp_path, "nl"), "{{net_content}}")
    _write(_default_path(tmp_path, "fr"), "{{net_content}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    assert engine.render(product, "nl", CLIENT_META) == "4 Stuk"
    assert engine.render(product, "fr", CLIENT_META) == "4 Pièce"


def test_gtin14_is_zero_padded(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "nl"), "{{gtin}}|{{gtin14}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "12345670|00000012345670"


def test_extras_substitution(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "nl"), "{{extras.functional_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    out = engine.render(make_product(), "nl", CLIENT_META)

    assert out == "Bucket"


def test_a_localised_extra_renders_in_the_pages_own_language(tmp_path: Path) -> None:
    """The French template must get the French token, not the Dutch one."""
    for language in ("nl", "fr"):
        _write(_default_path(tmp_path, language), "{{extras.functional_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(
        extras={},
        extras_localised={"functional_name": LocalisedText(values={"nl": "Emmer", "fr": "Seau"})},
    )

    assert engine.render(product, "nl", CLIENT_META) == "Emmer"
    assert engine.render(product, "fr", CLIENT_META) == "Seau"


def test_a_localised_extra_this_language_lacks_falls_back_to_the_default(tmp_path: Path) -> None:
    """Unlike ACF, a template renders one blob and a hole in it reads as a broken page.

    ``client_meta['default_language']`` is already the template engine's fallback for every
    other localised field (§3.4), so extras follow the same rule rather than inventing a
    second one.
    """
    _write(_default_path(tmp_path, "fr"), "[{{extras.functional_name}}]")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(
        extras={}, extras_localised={"functional_name": LocalisedText(values={"nl": "Emmer"})}
    )

    assert engine.render(product, "fr", CLIENT_META) == "[Emmer]"


def test_a_localised_extra_the_template_names_does_not_warn_as_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # E12 warns on an extra no product carries; one that lives in extras_localised is carried.
    _write(_default_path(tmp_path, "nl"), "{{extras.functional_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(
        extras={}, extras_localised={"functional_name": LocalisedText(values={"nl": "Emmer"})}
    )

    with caplog.at_level(logging.WARNING, logger="lib.templates"):
        out = engine.render(product, "nl", CLIENT_META)

    assert out == "Emmer"
    assert "functional_name" not in caplog.text


def test_absent_optional_field_renders_empty(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "nl"), "[{{category}}]")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(category=None)

    out = engine.render(product, "nl", CLIENT_META)

    assert out == "[]"


# --- Edge E12: unknown extras key --------------------------------------------


def test_unknown_extra_renders_empty_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(_default_path(tmp_path, "nl"), "start[{{extras.hs_code}}]end")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="lib.templates"):
        first = engine.render(make_product(), "nl", CLIENT_META)
        second = engine.render(make_product(), "nl", CLIENT_META)

    assert first == "start[]end"
    assert second == "start[]end"
    warnings = [r for r in caplog.records if "hs_code" in r.getMessage()]
    assert len(warnings) == 1


# --- Edge E13: data containing Mustache / HTML -------------------------------


def test_data_with_mustache_and_html_is_inert_and_escaped(tmp_path: Path) -> None:
    _write(_default_path(tmp_path, "nl"), "{{product_name}}")
    engine = TemplateEngine(CLIENT_ID, None, base_dir=tmp_path)
    product = make_product(
        product_name=LocalisedText(values={"nl": "<script>x</script> {{brand}}"})
    )

    out = engine.render(product, "nl", CLIENT_META)

    # HTML is escaped ...
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # ... and the injected Mustache tag is not re-rendered (stays literal, not "Acme").
    assert "{{brand}}" in out
