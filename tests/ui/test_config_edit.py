"""Tests for ui/config_edit.py — editing clients.yml without damaging it.

``clients.yml`` is a document, not a serialised structure: most of its lines are comments, and
several of them are the only written record of why a value is what it is. So the properties worth
asserting are about what *survives* an edit, not only about what changes.

The last two tests are the ones that matter most. One proves the file is refused rather than
replaced when the result would not load; the other proves the pipeline's own loader then agrees
with what the form wrote — an edit that validated but did not take effect would be the silent
failure this project keeps designing against.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lib.config import ClientConfig, get_client
from lib.errors import ConfigError
from ui.config_edit import apply_edits, write_validated

CONFIG = """\
version: 1

defaults:
  wordpress:
    post_type: page
    languages: [nl]
    multilingual_plugin: none       # none | polylang | wpml

clients:

  # The pilot. Read the comment before changing the environment.
  acme:
    display_name: "Acme B.V."
    enabled: true

    gs1:
      # Production, overriding defaults. Not a preference: the sandbox account has no
      # Digital Link contract, so `test` cannot resolve anything.
      environment: production
      account_number_test: "8712345000010"
      account_number_production: "8712345000003"   # token claim, confirmed by a live GET
      client_id_env_test: ACME_GS1_SANDBOX_ID
      client_secret_env_test: ACME_GS1_SANDBOX_SECRET
      client_id_env_production: ACME_GS1_CLIENT_ID
      client_secret_env_production: ACME_GS1_CLIENT_SECRET

    export:
      format: flat
      path: "./input/acme/products.xlsx"
      column_map:
        GTIN: gtin

    wordpress:
      site_url: "https://www.acme.nl"
      username: "gs1-publisher"
      app_password_env: ACME_WP_APP_PASS
      post_type: acme
      languages: [nl, fr]
      default_language: nl

    # Keep this block: run_plan derives require_generated_copy from it.
    generator:
      enabled: true
      prompt_version: v1
"""


def _path(*parts: str) -> tuple[str, ...]:
    return ("clients", "acme", *parts)


def _loaded(text: str) -> ClientConfig:
    """Load a candidate config from a string, through a real file — the only loader there is."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "clients.yml"
        path.write_text(text, encoding="utf-8")
        return get_client("acme", path)


# --- What survives an edit ----------------------------------------------------


def test_editing_one_value_leaves_every_other_line_untouched() -> None:
    edited = apply_edits(CONFIG, {_path("wordpress", "site_url"): "https://shop.acme.nl"})

    before = CONFIG.splitlines()
    after = edited.splitlines()
    assert len(before) == len(after)
    differing = [n for n, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1
    assert after[differing[0]].strip() == 'site_url: "https://shop.acme.nl"'


def test_a_trailing_comment_survives_the_value_it_explains() -> None:
    """Several of these comments are the only record of why a value is what it is."""
    edited = apply_edits(CONFIG, {_path("gs1", "account_number_production"): "8712345000997"})

    line = next(x for x in edited.splitlines() if "account_number_production" in x)
    assert line.strip().startswith('account_number_production: "8712345000997"')
    assert line.endswith("# token claim, confirmed by a live GET")


def test_an_aligned_comment_keeps_its_column() -> None:
    """The comments in this file are aligned by hand, and its diffs are reviewed by hand."""
    original = next(x for x in CONFIG.splitlines() if "account_number_production" in x)
    edited = apply_edits(CONFIG, {_path("gs1", "account_number_production"): "8712345000997"})

    line = next(x for x in edited.splitlines() if "account_number_production" in x)
    assert line.index("#") == original.index("#")


def test_a_longer_value_pushes_its_comment_along_rather_than_overwriting_it() -> None:
    edited = apply_edits(
        CONFIG, {_path("gs1", "account_number_production"): "8712345000003000000000"}
    )

    line = next(x for x in edited.splitlines() if "account_number_production" in x)
    assert line.endswith("# token claim, confirmed by a live GET")
    assert '"8712345000003000000000"  #' in line


def test_the_files_quoting_style_survives_a_value_that_would_not_need_quotes() -> None:
    """Rewriting `"gs1-publisher"` as `gs1-publisher` is a diff that is not a change."""
    edited = apply_edits(CONFIG, {_path("wordpress", "username"): "another-bot"})

    assert 'username: "another-bot"' in edited
    assert _loaded(edited).wordpress.username == "another-bot"


def test_the_defaults_block_is_never_touched() -> None:
    """Every path starts at the client, so a form cannot change another client's behaviour."""
    edited = apply_edits(CONFIG, {_path("wordpress", "post_type"): "product"})

    defaults, _, clients = edited.partition("clients:")
    assert "post_type: page" in defaults
    assert "post_type: product" in clients


def test_a_key_only_inherited_from_defaults_is_added_to_the_client_block() -> None:
    """Editing an inherited value makes it a per-client override, which is what was meant."""
    edited = apply_edits(CONFIG, {_path("wordpress", "multilingual_plugin"): "wpml"})

    assert "multilingual_plugin: none" in edited.partition("clients:")[0]
    assert "multilingual_plugin: wpml" in edited.partition("clients:")[2]
    assert _loaded(edited).wordpress.multilingual_plugin == "wpml"


def test_a_missing_block_is_created_with_its_key() -> None:
    edited = apply_edits(
        CONFIG,
        {
            _path("process_list", "path"): "./input/acme/process-list.xlsx",
            _path("process_list", "gtin_column"): "Barcode",
        },
    )

    config = _loaded(edited)
    assert config.process_list is not None
    assert config.process_list.path == "./input/acme/process-list.xlsx"
    assert config.process_list.gtin_column == "Barcode"


def test_a_list_is_written_in_the_flow_style_the_file_already_uses() -> None:
    edited = apply_edits(CONFIG, {_path("wordpress", "languages"): ["nl", "fr", "de"]})

    assert "languages: [nl, fr, de]" in edited
    assert _loaded(edited).wordpress.languages == ["nl", "fr", "de"]


def test_an_inserted_key_does_not_steal_the_comment_above_the_next_block() -> None:
    """The `generator` comment introduces `generator`, not whatever is inserted before it."""
    edited = apply_edits(CONFIG, {_path("wordpress", "slug_pattern"): "p-{gtin}"})

    lines = edited.splitlines()
    comment = next(n for n, line in enumerate(lines) if "run_plan derives" in line)
    assert lines[comment + 1].strip() == "generator:"


def test_the_file_keeps_its_trailing_newline() -> None:
    assert apply_edits(CONFIG, {_path("display_name"): "Acme N.V."}).endswith("\n")


# --- Quoting ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ("acme", "acme"),  # a bare token stays bare
        ("ACME_WP_APP_PASS", "ACME_WP_APP_PASS"),
        ("https://www.acme.nl", '"https://www.acme.nl"'),  # a colon needs quotes
        ("./input/acme/products.xlsx", '"./input/acme/products.xlsx"'),
        ("8712345000003", '"8712345000003"'),  # digits are not a number here
        ("no", '"no"'),  # YAML would read this as False
        ("", '""'),
        ('say "hi"', '"say \\"hi\\""'),
    ],
)
def test_values_are_quoted_only_where_plain_style_would_change_them(
    value: str, written: str
) -> None:
    edited = apply_edits(CONFIG, {_path("wordpress", "post_type"): value})

    assert f"post_type: {written}" in edited.partition("clients:")[2]


def test_an_inline_mapping_is_refused_rather_than_mangled() -> None:
    """`gdsn_map` rows are written inline and hand-aligned; this form does not edit them."""
    text = CONFIG.replace("      column_map:\n        GTIN: gtin", "      column_map: { GTIN: g }")

    with pytest.raises(ConfigError, match="inline"):
        apply_edits(text, {("clients", "acme", "export", "column_map", "GTIN"): "gtin14"})


# --- Validate before replacing ------------------------------------------------


def test_an_invalid_result_is_refused_and_the_file_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "clients.yml"
    path.write_text(CONFIG, encoding="utf-8")
    broken = apply_edits(CONFIG, {_path("wordpress", "multilingual_plugin"): "weglot"})

    with pytest.raises(ConfigError, match="multilingual_plugin"):
        write_validated(path, broken)

    assert path.read_text(encoding="utf-8") == CONFIG
    assert not (tmp_path / "clients.yml.candidate").exists()
    assert not (tmp_path / "clients.yml.bak").exists()


def test_a_valid_result_is_written_and_the_previous_version_kept(tmp_path: Path) -> None:
    path = tmp_path / "clients.yml"
    path.write_text(CONFIG, encoding="utf-8")
    edited = apply_edits(CONFIG, {_path("wordpress", "site_url"): "https://shop.acme.nl"})

    backup = write_validated(path, edited)

    assert backup.read_text(encoding="utf-8") == CONFIG
    assert get_client("acme", path).wordpress.site_url == "https://shop.acme.nl"


def test_what_the_form_wrote_is_what_the_pipeline_then_loads(tmp_path: Path) -> None:
    """The point of the whole screen: an edit that validated but did not take effect is worse
    than one that was refused."""
    path = tmp_path / "clients.yml"
    path.write_text(CONFIG, encoding="utf-8")
    changes: dict[tuple[str, ...], str | list[str]] = {
        _path("display_name"): "Acme N.V.",
        _path("wordpress", "post_type"): "product",
        _path("wordpress", "languages"): ["nl", "fr", "de"],
        _path("gs1", "environment"): "test",
        _path("export", "path"): "./input/acme/2026-Q3.xlsx",
    }

    write_validated(path, apply_edits(path.read_text(encoding="utf-8"), changes))

    config = get_client("acme", path)
    assert config.display_name == "Acme N.V."
    assert config.wordpress.post_type == "product"
    assert config.wordpress.languages == ["nl", "fr", "de"]
    assert config.gs1.environment == "test"
    assert config.export.path == "./input/acme/2026-Q3.xlsx"
