"""Unit tests for the video→GTIN candidate report's row building (issue #120).

Pure: no filesystem, no config. What the script does with these rows —  CSV or xlsx, where the
file lands — is covered in ``tests/scripts/test_report_video_candidates.py``.
"""

from __future__ import annotations

from lib.media_video import VideoMap
from lib.records import LocalisedText, ProductRecord
from lib.video_candidates import (
    NOT_IN_MAPPING,
    NOT_ON_DISK,
    build_rows,
    cells,
    header,
)

_LANGUAGES = ["nl", "fr"]


def _product(
    gtin: str,
    *,
    nl: str = "",
    fr: str = "",
    extras: dict[str, str] | None = None,
    extras_localised: dict[str, LocalisedText] | None = None,
) -> ProductRecord:
    values = {k: v for k, v in {"nl": nl, "fr": fr}.items() if v}
    return ProductRecord(
        # Padded to the shortest GTIN the record accepts, so a test can name a product "1" and
        # still have `canon_gtin` line it up with a mapping row that says the same.
        gtin=gtin.zfill(8),
        brand="Democlient",
        product_name=LocalisedText(values=values or {"nl": gtin}),
        extras=extras or {},
        extras_localised=extras_localised or {},
    )


def _map(**by_language: list[tuple[str, str]]) -> VideoMap:
    return VideoMap.model_validate(
        {
            "by_language": {
                language: [{"file": f, "gtin": g} for f, g in rows]
                for language, rows in by_language.items()
            }
        }
    )


def _rows(vmap: VideoMap, files: dict[str, list[str]], products: list[ProductRecord], n: int = 3):
    return build_rows(vmap, files, products, _LANGUAGES, top_n=n)


def _by_file(rows: list, filename: str, language: str = "nl"):
    return next(r for r in rows if r.file == filename and r.language == language)


# --- The union: both directions of disagreement are visible ------------------


def test_a_file_on_disk_that_the_mapping_does_not_list_still_gets_a_row() -> None:
    """The report's whole reason for being a union. `--check` reports these as an issue kind; a
    client working through a spreadsheet needs them as rows, with hints, like everything else."""
    rows = _rows(
        _map(nl=[("Mapped.mpg", "")]), {"nl": ["Mapped.mpg", "Stray.mpg"]}, [_product("1")]
    )

    stray = _by_file(rows, "Stray.mpg")
    assert stray.state == NOT_IN_MAPPING
    assert stray.gtin == ""
    assert stray.candidates, "an unmapped file is exactly the row that needs hints"


def test_a_mapping_row_whose_file_is_absent_keeps_the_gtin_it_was_confirmed_to() -> None:
    """NOT ON DISK says the folder and the file disagree — not that the sign-off was lost.

    Blanking the GTIN here would be the failure this report is meant to avoid: the client opens
    the sheet on a machine without the video library and sees their confirmed work gone.
    """
    rows = _rows(_map(nl=[("Gone.mpg", "2987653007434")]), {"nl": []}, [_product("02987653007434")])

    row = _by_file(rows, "Gone.mpg")
    assert row.state == NOT_ON_DISK
    assert row.gtin == "2987653007434"


def test_every_mapping_row_and_every_file_is_counted_exactly_once() -> None:
    vmap = _map(nl=[("A.mpg", ""), ("B.mpg", "1")], fr=[("C.mpg", "")])
    rows = _rows(vmap, {"nl": ["A.mpg", "B.mpg", "D.mpg"], "fr": ["C.mpg"]}, [_product("1")])

    assert [(r.language, r.file) for r in rows] == [
        ("nl", "A.mpg"),
        ("nl", "B.mpg"),
        ("nl", "D.mpg"),
        ("fr", "C.mpg"),
    ]


def test_a_language_nobody_configured_is_reported_rather_than_dropped() -> None:
    """A folder or a mapping block for a language the site does not publish is a mistake worth
    seeing. Silently omitting its rows would make the file look complete."""
    rows = _rows(_map(de=[("Lampe.mpg", "")]), {"de": ["Lampe.mpg"]}, [_product("1")])

    assert [r.language for r in rows] == ["de"]


# --- State ------------------------------------------------------------------


def test_a_gtin_of_only_whitespace_is_unset_not_a_product_that_does_not_exist() -> None:
    """`canon_gtin(" ")` is `00000000000000`. Read as confirmed it would show a mapped row with
    no names, and — through the same classifier — hold the real product out of every run."""
    rows = _rows(_map(nl=[("A.mpg", "   ")]), {"nl": ["A.mpg"]}, [_product("1")])

    assert _by_file(rows, "A.mpg").state == "unset"
    assert _by_file(rows, "A.mpg").names == {}


def test_skip_is_a_decision_and_reads_as_one() -> None:
    rows = _rows(_map(nl=[("Advert.mpg", "SKIP")]), {"nl": ["Advert.mpg"]}, [_product("1")])

    assert _by_file(rows, "Advert.mpg").state == "skip"


# --- The names that identify a product ---------------------------------------


def test_a_mapped_row_shows_the_marketing_and_logistics_names_not_only_product_name() -> None:
    """The finding the report exists to encode: `product_name` is the short generic one
    (`siliconenbak`), and what identifies the product to somebody looking at a video file is in
    the two name extras (`Pipe Sticks 12pc`)."""
    product = _product(
        "02987653006796",
        nl="afvoerreinigingsstick",
        extras_localised={
            "marketing_name": LocalisedText(
                values={"nl": "Democlient Afvoerreinigingsstick afbreekbaar geel"}
            ),
            "logistics_name": LocalisedText(values={"fr": "Pipe Sticks 12pc"}),
        },
    )
    rows = _rows(
        _map(nl=[("PipeSticks.mpeg", "2987653006796")]), {"nl": ["PipeSticks.mpeg"]}, [product]
    )

    row = _by_file(rows, "PipeSticks.mpeg")
    assert row.state == "confirmed"
    assert row.names["product_name.nl"] == "afvoerreinigingsstick"
    assert row.names["marketing_name.nl"] == "Democlient Afvoerreinigingsstick afbreekbaar geel"
    assert row.names["logistics_name.fr"] == "Pipe Sticks 12pc"


def test_a_thirteen_digit_mapping_gtin_still_finds_its_fourteen_digit_product() -> None:
    """The mapping is hand-keyed with 13 digits and the feed carries 14. A raw `==` here would
    leave every confirmed row's names blank, which reads as "this GTIN is not in the feed"."""
    rows = _rows(
        _map(nl=[("A.mpg", "2987653007434")]),
        {"nl": ["A.mpg"]},
        [_product("02987653007434", nl="cliplamp")],
    )

    assert _by_file(rows, "A.mpg").names["product_name.nl"] == "cliplamp"


def test_a_name_the_record_carries_flat_is_still_shown() -> None:
    """A `products.json` written before `extras_localised` existed holds one string for every
    language. Reading only the localised map would blank the names on those files."""
    product = _product("1", nl="lamp", extras={"marketing_name": "Democlient Bulb man"})
    rows = _rows(_map(nl=[("A.mpg", "1")]), {"nl": ["A.mpg"]}, [product])

    row = _by_file(rows, "A.mpg")
    assert cells(row, _LANGUAGES, 3)[header(_LANGUAGES, 3).index("mapped_marketing_name.nl")] == (
        "Democlient Bulb man"
    )


def test_a_confirmed_gtin_the_feed_does_not_carry_leaves_the_names_blank() -> None:
    """Worth seeing rather than raising: the GTIN is in the sheet, the names beside it are not."""
    rows = _rows(_map(nl=[("A.mpg", "9999999999999")]), {"nl": ["A.mpg"]}, [_product("1")])

    row = _by_file(rows, "A.mpg")
    assert row.state == "confirmed"
    assert row.names == {}


# --- Candidates: the value and the field are the point ------------------------


def test_the_winning_value_and_field_are_carried_so_a_french_slot_holding_english_makes_sense() -> (
    None
):
    """The second finding: the filenames are English and this feed's English sits in the French
    slots, so a high score can land beside a name the reader does not recognise. Without the
    value and the field it came from, that reads as a bug rather than as the answer."""
    products = [
        _product("02987653006796", nl="afvoerreinigingsstick", fr="bâtonnets"),
        _product(
            "02987653000001",
            nl="lamp",
            extras_localised={"logistics_name": LocalisedText(values={"fr": "Pipe Sticks 12pc"})},
        ),
    ]
    rows = _rows(_map(nl=[("PipeSticks_NL.mpeg", "")]), {"nl": ["PipeSticks_NL.mpeg"]}, products)

    best = _by_file(rows, "PipeSticks_NL.mpeg").candidates[0]
    assert best.gtin == "02987653000001"
    assert best.name == "Pipe Sticks 12pc"
    assert best.field == "extras.logistics_name.fr"
    assert best.score > 0.8


def test_the_normalized_name_the_scores_were_computed_against_is_carried() -> None:
    rows = _rows(
        _map(nl=[("BonnetShine_NL_SmallV2.mpg", "")]),
        {"nl": ["BonnetShine_NL_SmallV2.mpg"]},
        [_product("1")],
    )

    assert _by_file(rows, "BonnetShine_NL_SmallV2.mpg").normalized == "bonnet shine"


# --- The grid ----------------------------------------------------------------


def test_every_row_is_the_full_width_even_when_the_feed_offers_fewer_candidates() -> None:
    """A short row would shift every later column up a place, and a spreadsheet gives no sign."""
    columns = header(_LANGUAGES, 3)
    rows = _rows(_map(nl=[("A.mpg", "")]), {"nl": ["A.mpg"]}, [_product("1")], n=3)

    assert len(rows[0].candidates) == 1
    assert len(cells(rows[0], _LANGUAGES, 3)) == len(columns)


def test_top_n_widens_the_header_and_the_rows_together() -> None:
    rows = _rows(
        _map(nl=[("A.mpg", "")]), {"nl": ["A.mpg"]}, [_product(str(n)) for n in range(9)], n=5
    )

    assert header(_LANGUAGES, 5)[-4:] == [
        "candidate_5_gtin",
        "candidate_5_score",
        "candidate_5_value",
        "candidate_5_field",
    ]
    assert len(cells(rows[0], _LANGUAGES, 5)) == len(header(_LANGUAGES, 5))


def test_the_score_is_a_number_and_the_gtin_is_not() -> None:
    """Scores are sorted and filtered by the client, so they stay numeric. A GTIN is text: as a
    number Excel drops its leading zero, and `2987653007434` no longer matches the feed."""
    columns = header(_LANGUAGES, 3)
    rows = _rows(
        _map(nl=[("A.mpg", "02987653007434")]), {"nl": ["A.mpg"]}, [_product("02987653007434")]
    )
    values = cells(rows[0], _LANGUAGES, 3)

    assert isinstance(values[columns.index("candidate_1_score")], float)
    assert values[columns.index("candidate_1_gtin")] == "02987653007434"
    assert values[columns.index("gtin")] == "02987653007434"


def test_the_name_columns_follow_the_configured_language_order() -> None:
    assert header(["fr", "nl"], 1)[5:11] == [
        "mapped_product_name.fr",
        "mapped_product_name.nl",
        "mapped_marketing_name.fr",
        "mapped_marketing_name.nl",
        "mapped_logistics_name.fr",
        "mapped_logistics_name.nl",
    ]


# --- A shared video ----------------------------------------------------------


def test_a_video_shared_by_two_products_is_one_row_with_both_barcodes() -> None:
    """The mapping writes a shared video one row per barcode; the sheet shows it once, the barcodes
    joined by commas — the form the sign-off import reads back, so sending the sheet out and taking
    it back untouched changes nothing."""
    vmap = _map(nl=[("Shared.mpg", "2987653007434"), ("Shared.mpg", "02987653000893")])
    products = [_product("02987653007434", nl="Lamp"), _product("02987653000893", nl="Bulb")]

    rows = _rows(vmap, {"nl": ["Shared.mpg"]}, products)

    assert [(r.file, r.gtin, r.state) for r in rows] == [
        ("Shared.mpg", "2987653007434, 02987653000893", "confirmed")
    ]
    assert rows[0].names["product_name.nl"] == "Lamp / Bulb"
