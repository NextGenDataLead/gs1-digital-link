"""Which of a product's pictures goes where on its page (operator, 2026-11-18).

A page has three picture places: the banner at the top (``header``), the picture beside the text
(``regular``), and a gallery under it. The feed holds every picture the client gave the product,
and the rule is:

* **regular** — the product *out of its packaging*. The GS1 image naming convention says which:
  the 19th character of the file name is ``0`` out of the packaging and ``1`` in it
  (``08713195008127_C1C0_s01.tif``). Of several, the primary one, else the first. A product with
  none shows its primary picture there, else its first.
* **header** — the primary picture, else the regular one, so a banner is never empty when a
  picture exists.
* **gallery** — every picture except the regular one, in feed order. It *replaces* what the page
  had: the feed is the whole set, so a picture the client removed in MyGS1 leaves the page too.

Pure: no I/O, so the rule is testable on its own and the run loop only uploads what it returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, NamedTuple

from lib.records import ProductImage

#: Index of the packaging character in a GS1 image file name: 14-digit GTIN, ``_``, image type,
#: facing, angle, then this one.
_PACKAGING_INDEX: Final = 18
#: That character for a product shown out of its packaging.
_OUT_OF_PACKAGING: Final = "0"


class PagePictures(NamedTuple):
    """The pictures one page carries, by place. ``None`` / empty when the product has none."""

    header: ProductImage | None
    regular: ProductImage | None
    gallery: tuple[ProductImage, ...]


def out_of_packaging(image: ProductImage) -> bool:
    """Whether the file name says the product is shown out of its packaging."""
    name = image.name
    return len(name) > _PACKAGING_INDEX and name[_PACKAGING_INDEX] == _OUT_OF_PACKAGING


def _first(images: Sequence[ProductImage], *, primary: bool) -> ProductImage | None:
    """The first primary picture when ``primary``, else the first picture at all."""
    return next((image for image in images if image.primary or not primary), None)


def choose(images: Sequence[ProductImage]) -> PagePictures:
    """Place a product's pictures on its page — see the module docstring for the rule."""
    if not images:
        return PagePictures(None, None, ())
    unpacked = [image for image in images if out_of_packaging(image)]
    candidates = unpacked or list(images)
    regular = _first(candidates, primary=True) or candidates[0]
    header = _first(images, primary=True) or regular
    gallery = tuple(image for image in images if image != regular)
    return PagePictures(header, regular, gallery)
