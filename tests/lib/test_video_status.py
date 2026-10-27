"""Unit tests for lib/video_status.py — the per-product video join.

Pure: a mapping and some products in, a status out. No config, no folders on disk.
"""

from __future__ import annotations

import itertools

from lib.media_video import VideoMap, VideoMapEntry, fully_mapped_gtins, video_gate
from lib.records import LocalisedText, ProductRecord
from lib.video_status import (
    CLASHING,
    HAS_VIDEO,
    MISSING,
    UnassignedVideo,
    VideoStatus,
    video_status,
    waiting_on,
)

_LANGUAGES = ["nl", "fr"]
_A = "02987653000001"
_B = "02987653000002"
_C = "02987653000003"


def _product(gtin: str, **names: str) -> ProductRecord:
    return ProductRecord(gtin=gtin, brand="Democlient", product_name=LocalisedText(values=names))


def _map(**rows: list[tuple[str, str]]) -> VideoMap:
    return VideoMap(
        by_language={
            language: [VideoMapEntry(file=file, gtin=gtin) for file, gtin in entries]
            for language, entries in rows.items()
        }
    )


def _status(
    vmap: VideoMap,
    *gtins: str,
    files: dict[str, list[str]] | None = None,
    publish_without_video: bool = False,
) -> VideoStatus:
    on_disk = files
    if on_disk is None:  # every mapped file is on disk unless the test says otherwise
        on_disk = {lang: [e.file for e in entries] for lang, entries in vmap.by_language.items()}
    return video_status(
        vmap,
        [_product(g) for g in gtins],
        languages=_LANGUAGES,
        files_by_language=on_disk,
        publish_without_video=publish_without_video,
    )


def test_a_product_has_a_video_exactly_when_resolve_attaches_one() -> None:
    """The anti-drift pin: every cell agrees with the method ``run_execute`` attaches videos with.

    Built over every combination of the cell states a row can hold — confirmed once, confirmed
    twice, spelled 13-digit, `skip`, blank, whitespace — so a re-derivation that disagreed with
    ``resolve`` on any of them fails here rather than on a live page.
    """
    spellings = ["2987653000001", _A, "skip", "", "   "]
    for nl, fr in itertools.product(spellings, repeat=2):
        vmap = _map(
            nl=[("a.mpg", nl), ("b.mpg", nl)] if nl == _A else [("a.mpg", nl)],
            fr=[("c.mpg", fr)],
        )
        (product,) = _status(vmap, _A).products
        for language in _LANGUAGES:
            attached = vmap.resolve(_A, language) is not None
            assert (product.by_language[language] == HAS_VIDEO) is attached, (nl, fr, language)
        assert product.attaches_a_video is all(
            vmap.resolve(_A, lang) is not None for lang in _LANGUAGES
        )


def test_held_is_exactly_what_the_gate_holds() -> None:
    """``held`` is the gate's word: the same combinations, checked against ``fully_mapped_gtins``.

    The Data screen counts its Video column as holds, so the two must never disagree about which
    products a run skips.
    """
    spellings = ["2987653000001", _A, "skip", "", "   "]
    for nl, fr in itertools.product(spellings, repeat=2):
        vmap = _map(
            nl=[("a.mpg", nl), ("b.mpg", nl)] if nl == _A else [("a.mpg", nl)],
            fr=[("c.mpg", fr)],
        )
        held = {p.gtin for p in _status(vmap, _A).held}
        assert held == ({_A} - fully_mapped_gtins(vmap, _LANGUAGES)), (nl, fr)


def test_held_and_without_video_are_exactly_what_the_gate_decides_under_either_setting() -> None:
    """Held is what the gate does not admit; without-video is what it admits incompletely.

    The same combinations as above, under both settings. The Data screen, the report and the run
    all read one of the two, so they must partition the products exactly as ``run_execute`` will.
    """
    spellings = ["2987653000001", _A, "skip", "", "   "]
    for setting, (nl, fr) in itertools.product(
        [False, True], itertools.product(spellings, repeat=2)
    ):
        vmap = _map(
            nl=[("a.mpg", nl), ("b.mpg", nl)] if nl == _A else [("a.mpg", nl)],
            fr=[("c.mpg", fr)],
        )
        gate = video_gate(vmap, _LANGUAGES, publish_without_video=setting)
        status = _status(vmap, _A, publish_without_video=setting)
        held = {p.gtin for p in status.held}
        bare = {p.gtin for p in status.without_video}
        assert held == ({_A} - {g for g in [_A] if gate.admits(g)}), (setting, nl, fr)
        assert bare == ({_A} - held - gate.confirmed), (setting, nl, fr)


def test_two_files_for_one_product_in_one_language_hold_it() -> None:
    """The trap this module used to only name: two nl videos, so ``resolve`` attaches neither.

    The gate counted a set of confirmed cells, so the GTIN was "mapped" in nl and published with
    no nl video, reporting success. It is held now, and still named as clashing so the report can
    show both files.
    """
    vmap = _map(nl=[("a.mpg", _A), ("b.mpg", _A)], fr=[("c.mpg", _A)])

    status = _status(vmap, _A)
    (product,) = status.products

    assert _A not in fully_mapped_gtins(vmap, _LANGUAGES)  # the gate holds it
    assert product.by_language == {"nl": CLASHING, "fr": HAS_VIDEO}
    assert product.files["nl"] == ("a.mpg", "b.mpg")
    assert waiting_on(product) == "two videos in nl"
    assert [p.gtin for p in status.held] == [_A]
    assert [p.gtin for p in status.clashing] == [_A]


def test_a_product_confirmed_in_one_language_names_the_other() -> None:
    vmap = _map(nl=[("a.mpg", _A)], fr=[])

    (product,) = _status(vmap, _A).products

    assert product.by_language == {"nl": HAS_VIDEO, "fr": MISSING}
    assert waiting_on(product) == "no confirmed video in fr"


def test_a_product_with_nothing_names_every_language_in_configured_order() -> None:
    (product,) = _status(_map(nl=[], fr=[]), _A).products

    assert waiting_on(product) == "no confirmed video in nl, fr"


def test_a_product_with_a_video_everywhere_is_waiting_on_nothing() -> None:
    vmap = _map(nl=[("a.mpg", _A)], fr=[("b.mpg", _A)])

    status = _status(vmap, _A)

    assert waiting_on(status.products[0]) == ""
    assert status.held == ()


def test_skip_is_a_decision_not_an_unassigned_file() -> None:
    """`skip` means the file maps to no product on purpose — not work still to do."""
    status = _status(_map(nl=[("promo.mpg", "skip")], fr=[]), _A)

    assert status.unassigned == ()
    assert status.products[0].by_language["nl"] == MISSING  # and it is no product's video


def test_a_whitespace_cell_is_unassigned_not_the_all_zero_gtin() -> None:
    """``canon_gtin("   ")`` is ``00000000000000``; a classifier that skipped ``state_of`` made it
    a confirmed product. The report's §0 did exactly that, which is the bug this replaces there.
    """
    zero = "00000000000000"
    status = _status(_map(nl=[("a.mpg", "   ")], fr=[]), zero)

    assert status.products[0].by_language["nl"] == MISSING
    assert status.unassigned == (UnassignedVideo("nl", "a.mpg"),)


def test_the_file_side_lists_name_files_never_products() -> None:
    """A file nobody assigned names no product, so each of the three lists carries a filename."""
    vmap = _map(nl=[("unset.mpg", ""), ("gone.mpg", _A)], fr=[])
    on_disk = {"nl": ["unset.mpg", "stray.mpg"], "fr": []}

    status = _status(vmap, _A, files=on_disk)

    assert status.unassigned == (UnassignedVideo("nl", "unset.mpg"),)
    assert status.not_in_map == (UnassignedVideo("nl", "stray.mpg"),)
    assert status.files_missing == (UnassignedVideo("nl", "gone.mpg"),)


def test_products_come_worst_first_then_by_gtin() -> None:
    vmap = _map(nl=[("a.mpg", _A), ("b.mpg", _C)], fr=[("c.mpg", _A)])

    status = _status(vmap, _A, _C, _B)

    # _B lacks both, _C lacks fr, _A lacks nothing.
    assert [p.gtin for p in status.products] == [_B, _C, _A]
    assert [p.gtin for p in status.held] == [_B, _C]
    assert _status(vmap, _B, _C, _A).products == status.products  # input order is irrelevant


def test_the_name_is_the_first_configured_language_that_has_one() -> None:
    vmap = _map(nl=[], fr=[])
    status = video_status(
        vmap,
        [_product(_A, fr="Chiffon"), _product(_B, nl="Doek", fr="Chiffon"), _product(_C)],
        languages=_LANGUAGES,
        files_by_language={},
    )

    assert {p.gtin: p.name for p in status.products} == {_A: "Chiffon", _B: "Doek", _C: ""}


def test_a_thirteen_digit_row_joins_the_fourteen_digit_product() -> None:
    """The mapping is often keyed 13-digit; the product is GTIN-14. They are the same barcode."""
    vmap = _map(nl=[("a.mpg", _A[1:])], fr=[("b.mpg", _A)])

    assert _status(vmap, _A).products[0].attaches_a_video


def test_clashing_lists_only_the_products_with_two_videos_somewhere() -> None:
    vmap = _map(nl=[("a.mpg", _A), ("b.mpg", _A)], fr=[])

    status = _status(vmap, _A, _B)

    assert [p.gtin for p in status.clashing] == [_A]
