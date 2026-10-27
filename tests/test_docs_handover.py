"""What the operator's machine cannot get from a clone, and what the doc promises about it.

Five files are gitignored and have to be handed over separately. The install rehearsal found the
doc naming **two** of them, and the omission with permanent consequences — ``state.json`` — was one
of the three it did not mention. Without that file every already-published GTIN classifies as NEW:
a second page each, and another GS1 record each, which can never be deleted.

That is not a defect a reader notices, because a doc that lists two files reads exactly as
confidently as one that lists five. So the list is asserted here instead, against the names the code
and the shipped example config actually use — a rename on either side fails this test rather than
silently making the doc wrong.

This checks that each file is *named and explained*. It cannot check that the explanation is any
good; that is what review is for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from lib.config import DEFAULT_CLIENTS_PATH
from lib.state import STATE_FILENAME

_ROOT: Final = Path(__file__).resolve().parent.parent
_DOC: Final = _ROOT / "docs" / "operator-install.md"
_EXAMPLE_CONFIG: Final = _ROOT / "clients.example.yml"

#: Spelled out rather than read from ``lib.env.ENV_PATH``: no module on the test path may import
#: ``lib.env`` — see ``tests/lib/test_env.py``, which fails the suite if one does. That rule is
#: what keeps production credentials out of the pytest process, and it outranks tidiness here.
_ENV_FILENAME: Final = ".env"

#: The example client the docs are written around.
_EXAMPLE_CLIENT: Final = "democlient"


def _doc() -> str:
    return _DOC.read_text("utf-8")


def _example_client() -> dict[str, object]:
    data = yaml.safe_load(_EXAMPLE_CONFIG.read_text("utf-8"))
    client = data["clients"][_EXAMPLE_CLIENT]
    assert isinstance(client, dict)
    return client


def _configured_basename(*path: str) -> str:
    """The filename at a dotted key path in the example client's config."""
    node: object = _example_client()
    for key in path:
        assert isinstance(node, dict), f"clients.example.yml has no {'.'.join(path)}"
        node = node[key]
    assert isinstance(node, str)
    return Path(node).name


def _handover_files() -> dict[str, str]:
    """The five files, keyed by name, valued by why the operator's machine needs one."""
    return {
        DEFAULT_CLIENTS_PATH.name: "the site settings",
        _ENV_FILENAME: "the credentials",
        _configured_basename("process_list", "path"): "the scope of a run",
        STATE_FILENAME: "the ledger of what is already published",
        _configured_basename("media", "video_map_path"): "which video belongs to which product",
    }


@pytest.mark.parametrize("filename", sorted(_handover_files()), ids=lambda n: n)
def test_the_install_doc_names_every_file_that_must_be_handed_over(filename: str) -> None:
    """A file the doc does not name is a file that does not reach the machine."""
    assert filename in _doc(), (
        f"docs/operator-install.md never mentions {filename} ({_handover_files()[filename]}), "
        "so nothing tells the maintainer to send it"
    )


def test_the_install_doc_says_the_ledger_has_to_come_back() -> None:
    """``state.json`` travels both ways. Two divergent ledgers publish the same product twice."""
    assert "Returning the ledger" in _doc(), (
        "docs/operator-install.md no longer explains that state.json must return to the "
        "maintainer after a publish, or which copy wins"
    )


def test_the_install_doc_says_the_scope_list_is_uploaded_like_the_other_two() -> None:
    """It has an upload now, and this test used to assert the opposite.

    It was right when it was written: the Data screen only *edited* a list already on disk, and
    the doc had once claimed otherwise, which sent an operator looking for a button that did not
    exist. The button exists now, and the same failure runs the other way — an operator told to
    copy the file into a folder by hand will do exactly that, on a machine where the app would
    have validated it first and kept their original. So the assertion is inverted rather than
    deleted: this page is the one an operator learns the file handover from, and it must not be
    a release behind the screen.

    The filename stays in the doc either way — the sibling test below needs to find it there.
    """
    doc = _doc()
    process_list = _configured_basename("process_list", "path")
    assert "no upload control for it" not in doc, (
        f"docs/operator-install.md still says {process_list} has no upload. It has one, on Data, "
        "beneath the export."
    )
    assert (
        "All\n**three have an upload control**" in doc or "three have an upload control" in doc
    ), (
        "docs/operator-install.md must say that all three files that change hands are uploaded, "
        "so nobody goes looking for a folder to copy one into"
    )


# --- the example config is the only record of client policy that lives in git ----------------


def test_the_example_config_demonstrates_the_mandatory_field_options() -> None:
    """`clients.yml` is gitignored, so the example is where these options are documented.

    A real client's `required` markings never reach the repository — which means the example is
    the only thing a second client can be built from, and the only evidence in git that the
    options exist at all. It went one release without them; this stops that recurring.
    """
    gdsn_map = _example_client()["export"]["gdsn_map"]  # type: ignore[index,call-overload]
    assert isinstance(gdsn_map, dict)

    required = {name for name, src in gdsn_map.items() if src.get("required")}
    grouped = {
        name: src["required_group"] for name, src in gdsn_map.items() if src.get("required_group")
    }

    assert required, "clients.example.yml shows no `required: true` field"
    assert grouped, "clients.example.yml shows no `required_group` — the either-or form"
    # The group needs at least two members, or it demonstrates nothing an either-or is for.
    assert len(set(grouped.values())) == 1
    assert len(grouped) >= 2


def test_the_example_config_never_marks_a_field_required_and_grouped() -> None:
    """They answer different questions; the example must not model the contradiction."""
    gdsn_map = _example_client()["export"]["gdsn_map"]  # type: ignore[index,call-overload]
    both = [
        name
        for name, src in gdsn_map.items()  # type: ignore[union-attr]
        if src.get("required") and src.get("required_group")
    ]
    assert both == []
