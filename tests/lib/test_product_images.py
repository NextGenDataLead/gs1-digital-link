"""Tests for lib/product_images.py — which picture goes where on a page.

Each case is one the 49 live products actually have (export of 2026-11-09), named by its shape.
"""

from __future__ import annotations

from lib.product_images import choose, out_of_packaging
from lib.records import ProductImage

GTIN = "08713195008127"


def _img(code: str, *, primary: bool = False) -> ProductImage:
    name = f"{GTIN}_{code}.tif"
    return ProductImage(name=name, uri=f"https://cdn/{name}", primary=primary)


def test_the_19th_character_says_out_of_packaging() -> None:
    assert out_of_packaging(_img("C1C0_s01"))
    assert not out_of_packaging(_img("C1C1_s01"))
    assert not out_of_packaging(ProductImage(name="short.tif", uri="u"))


def test_one_unpacked_primary_picture_is_banner_and_regular_and_no_gallery() -> None:
    only = _img("A1N0", primary=True)
    assert choose([only]) == (only, only, ())


def test_the_unpacked_picture_is_regular_even_when_another_is_primary() -> None:
    """12 products: the primary shows the product in its packaging; the page leads with it out."""
    packed = _img("C1C1", primary=True)
    unpacked = _img("C1C0")
    pictures = choose([packed, unpacked])
    assert pictures.regular == unpacked
    assert pictures.header == packed
    assert pictures.gallery == (packed,)


def test_of_several_unpacked_the_primary_wins() -> None:
    first = _img("C1C0_s01")
    primary = _img("C1C0_s02", primary=True)
    assert choose([first, primary]).regular == primary


def test_of_several_unpacked_and_none_primary_the_first_wins() -> None:
    """13 products. Feed order decides, and the banner falls back to that same picture."""
    images = [_img("C1C1_s01"), _img("C1C0_s01"), _img("C1C1_s02"), _img("C1C0_s02")]
    pictures = choose(images)
    assert pictures.regular == images[1]
    assert pictures.header == images[1]
    assert pictures.gallery == (images[0], images[2], images[3])


def test_no_unpacked_picture_takes_the_primary() -> None:
    """Super 5: one picture, in its packaging, primary — it is still the regular picture."""
    left = _img("C1L1")
    primary = _img("C1N1", primary=True)
    pictures = choose([left, primary])
    assert pictures.regular == primary
    assert pictures.gallery == (left,)


def test_no_unpacked_and_no_primary_takes_the_first() -> None:
    """08713195004181: two in-packaging views, neither primary."""
    left, right = _img("C1L1"), _img("C1R1")
    assert choose([left, right]) == (left, left, (right,))


def test_the_gallery_keeps_the_banner_picture_and_feed_order() -> None:
    packed = _img("C1C1", primary=True)
    side = _img("C1L1")
    unpacked = _img("C1C0")
    assert choose([packed, side, unpacked]).gallery == (packed, side)


def test_no_pictures_places_nothing() -> None:
    assert choose([]) == (None, None, ())
