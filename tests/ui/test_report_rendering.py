"""The §0 header marks, against the markdown renderer the Data screen actually uses.

This report is read on **two** surfaces, and the mandatory/optional distinction has now been got
wrong once on each. `**product·3301**` was invisible when rendered, because markdown makes header
cells bold anyway. The `MANDATORY<br>…` group label that replaced it rendered correctly and put a
literal HTML tag in front of everyone reading the markdown as text.

A trailing `~` fixed the tag and introduced a third problem — a bare symbol still sends the
reader to the legend — so the label is now the word `(optional)`. Which is the one form with no
markdown meaning at all, and that is worth pinning rather than assuming: `~~x~~` is strikethrough
in several flavours, `*` and `_` are emphasis, and a renderer growing an extra could eat any of
them silently. These tests check the surviving label on `ui/pages/data.py`'s actual path:
``ui.markdown`` → markdown2 with ``['fenced-code-blocks', 'tables']``.

Lives under ``tests/ui/`` because markdown2 arrives with NiceGUI: in the required CI job, which
installs only ``.[dev]``, there is nothing here to import.
"""

from __future__ import annotations

import pytest

from lib.gdsn import GdsnSource
from lib.quality_report import MatrixInput, render_quality_report
from lib.records import LocalisedText, ProductRecord

pytest.importorskip("markdown2", reason="the ui extra is not installed here")

import markdown2  # noqa: E402

#: What `ui.markdown` passes to markdown2 (NiceGUI's `Markdown.default_extras`).
_EXTRAS = ["fenced-code-blocks", "tables"]


def _report() -> str:
    matrix = MatrixInput(
        products=[
            ProductRecord(
                gtin="02987653000001",
                brand="Democlient",
                product_name=LocalisedText(values={"nl": "a", "fr": "b"}),
            )
        ],
        gdsn_map={
            "product_name": GdsnSource(sheet="S", attribute="3301", localised=True, required=True)
        },
        gdsn_extras={"material": GdsnSource(sheet="S", attribute="Material")},
        video_confirmed={"nl": set(), "fr": set()},
    )
    return render_quality_report(
        client_id="democlient",
        source_issues=[],
        generated_issues=[],
        video_map_issues=[],
        category_issues=[],
        products={},
        snapshot="2026-07-27",
        freshness={},
        languages=["nl", "fr"],
        matrix=matrix,
    )


def test_the_optional_label_survives_rendering_and_the_header_carries_no_html() -> None:
    report = _report()
    html = markdown2.markdown(report, extras=_EXTRAS)

    assert "<th>material (optional)</th>" in html  # reaches the screen as written
    assert "<th>product·3301</th>" in html  # mandatory: unmarked, and no <strong> needed
    # The other half, and the reason the group label was rejected: nothing in the header may be
    # HTML, because the raw markdown is a surface too.
    header = next(line for line in report.splitlines() if line.startswith("| # |"))
    assert "<" not in header and ">" not in header


def test_the_installed_markdown_renderer_still_makes_header_cells_bold_on_its_own() -> None:
    """The premise of the fix, checked rather than assumed.

    If a future renderer stopped emitting ``<th>`` — or started ignoring the bold that comes with
    it — the whole reason for replacing `**…**` with a group label would be gone, and this test is
    what would say so instead of the change quietly becoming cargo cult.
    """
    html = markdown2.markdown("| a | b |\n|---|---|\n| 1 | 2 |\n", extras=_EXTRAS)

    assert "<th>a</th>" in html  # no <strong> needed: <th> is bold by default in every browser
