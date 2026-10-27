"""Tests for ui/form.py — writing only what the operator actually changed.

The Setup screen shows the *resolved* config, with the ``defaults`` block merged in. Saving all of
it would copy every inherited default into the client's own block, so a form nobody touched would
quietly turn `post_type`, `languages` and `environment` into per-client overrides — and the next
change to `defaults` would stop reaching this client with nothing to show that it had.

So the property under test is narrow and load-bearing: an untouched field produces no edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ui.form import FieldSet, split_list


@dataclass
class _Box:
    """A stand-in for a NiceGUI input: the only thing the field set reads is ``value``."""

    value: Any


def _fields() -> tuple[FieldSet, _Box, _Box, _Box]:
    fields = FieldSet(prefix=("clients", "acme"))
    site = _Box("https://www.acme.nl")
    post_type = _Box("acme")
    languages = _Box("nl, fr")
    fields.add(("wordpress", "site_url"), site, "https://www.acme.nl")
    fields.add(("wordpress", "post_type"), post_type, "acme")
    fields.add(("wordpress", "languages"), languages, ["nl", "fr"], split_list)
    return fields, site, post_type, languages


def test_an_untouched_form_produces_no_edits() -> None:
    fields, *_ = _fields()

    assert fields.changes() == {}


def test_only_the_changed_field_is_written() -> None:
    fields, site, _, _ = _fields()

    site.value = "https://shop.acme.nl"

    assert fields.changes() == {
        ("clients", "acme", "wordpress", "site_url"): "https://shop.acme.nl"
    }


def test_every_path_is_rooted_at_the_client() -> None:
    """Structurally, not by convention: there is no path a caller can pass that reaches
    ``defaults`` and so changes another client's behaviour."""
    fields, site, _, _ = _fields()
    site.value = "https://shop.acme.nl"

    assert all(path[:2] == ("clients", "acme") for path in fields.changes())


def test_retyping_the_same_value_is_not_a_change() -> None:
    fields, site, _, _ = _fields()

    site.value = "  https://www.acme.nl  "

    assert fields.changes() == {}


def test_a_list_compares_as_a_list_not_as_typed_text() -> None:
    """`"nl,fr"` and `"nl, fr"` are the same languages; only a real difference is an edit."""
    fields, _, _, languages = _fields()

    languages.value = "nl,fr"
    assert fields.changes() == {}

    languages.value = "nl, fr, de"
    assert fields.changes() == {("clients", "acme", "wordpress", "languages"): ["nl", "fr", "de"]}


def test_clearing_a_field_is_a_change_to_blank_rather_than_a_no_op() -> None:
    fields, site, _, _ = _fields()

    site.value = ""

    assert fields.changes() == {("clients", "acme", "wordpress", "site_url"): ""}


def test_correcting_a_just_saved_mistake_is_still_a_change() -> None:
    """After a save the baseline must move, or the revert reads as 'nothing to do' and writes
    nothing — exactly when the operator most needs it to write something."""
    fields, site, _, _ = _fields()
    site.value = "https://typo.acme.nl"

    fields.commit()  # what the screen does once the file is on disk

    assert fields.changes() == {}
    site.value = "https://www.acme.nl"
    assert fields.changes() == {("clients", "acme", "wordpress", "site_url"): "https://www.acme.nl"}


def test_reading_a_field_back_for_the_cross_field_checks() -> None:
    fields, _, post_type, languages = _fields()

    post_type.value = "product"
    languages.value = "nl, de"

    assert fields.text("wordpress", "post_type") == "product"
    assert fields.items("wordpress", "languages") == ["nl", "de"]
    assert fields.initial("wordpress", "post_type") == "acme"


def test_an_unregistered_path_reads_as_empty_rather_than_raising() -> None:
    """A screen that hides a field for a client without that block must not crash the save."""
    fields, *_ = _fields()

    assert fields.text("process_list", "gtin_column") == ""
    assert fields.items("wordpress", "absent") == []


def test_split_list_drops_what_a_stray_comma_leaves_behind() -> None:
    assert split_list("nl, , fr,") == ["nl", "fr"]
    assert split_list(None) == []
