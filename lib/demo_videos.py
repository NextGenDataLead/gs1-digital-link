"""The demo client's videos: placeholder files, a mapping, and the sign-off sheet a client returns.

Without these, ``democlient``'s three held products were held by E23 and E22 and **none by E24**, so
the half of the Data screen that is about videos could not be rehearsed at all — and the one client
it *could* be rehearsed against is the real one, which a rehearsal has corrupted three times.

Every state the screen and the report distinguish is here once, on purpose:

====================  ============================================================================
products 0–5, 11      a video in nl and fr — publish
product 6             nl confirmed, fr row **unset** — held until the sign-off sheet fills it
product 8             **two files confirmed in nl**, one in fr — held until one is marked ``skip``
                      (§1a and §1b)
product 9             nl only, and already held for missing data — needs both (the ¹ in §1a)
products 7, 10        nothing — held for missing data and for video
``Demo promo.mp4``    ``skip`` — a decision, not a gap
``Mystery clip.mp4``  unset — waiting for a barcode (§1c, with product 6's French row)
====================  ============================================================================

So before the sheet is applied seven products pass the gate and five are held; after it, eight
pass. The ninth that :mod:`lib.demo_export` promises publishes once one of product 8's two Dutch
videos is marked ``skip`` in the mapping — which is the demo of what to do about §1b.

The sheet carries one row per outcome the import decides, and calls its barcode column
``current_gtin`` — the real sheet's spelling, which the column pickers exist because of:

* a **change** (product 6's French video), an **unchanged** row, a **shared** video (product 0's
  Dutch video given to product 7 as well — two barcodes in one cell; product 7 stays held for its
  missing data, so no count moves), a **blank**, and a **rejection** — ``8.7132E+12``, which is
  what Excel does to a barcode it was not told is text.

Pure: no I/O. :mod:`scripts.make_demo_export` writes these where the config says.
"""

from __future__ import annotations

from typing import Final, NamedTuple

from lib.demo_export import CATALOGUE

_NL: Final = "nl"
_FR: Final = "fr"

#: Exactly what a placeholder video file contains. The tool lists video folders by extension and
#: never opens a file outside a transcode, so a few bytes are a video as far as a rehearsal goes.
PLACEHOLDER: Final = b"demo video placeholder - not a real video\n"

#: The file the sheet rejects, and the one nobody has assigned yet.
MYSTERY: Final = "Mystery clip.mp4"
PROMO: Final = "Demo promo.mp4"


class DemoVideo(NamedTuple):
    """One row of the mapping. ``gtin`` is the cell: a GTIN-14, ``"skip"``, or ``""`` (unset)."""

    language: str
    file: str
    gtin: str


def _nl(index: int, suffix: str = "") -> str:
    return f"{CATALOGUE[index].name['nl']}{suffix}.mp4"


def _fr(index: int) -> str:
    return f"{CATALOGUE[index].name.get('fr', CATALOGUE[index].name['nl'])} FR.mp4"


def _both(index: int) -> list[DemoVideo]:
    gtin = CATALOGUE[index].gtin
    return [DemoVideo(_NL, _nl(index), gtin), DemoVideo(_FR, _fr(index), gtin)]


def videos() -> tuple[DemoVideo, ...]:
    """Every row of the demo mapping, nl first, then fr — the order the file is written in."""
    rows = [video for index in (0, 1, 2, 3, 4, 5, 11) for video in _both(index)]
    rows += [
        DemoVideo(_NL, _nl(6), CATALOGUE[6].gtin),
        DemoVideo(_FR, _fr(6), ""),  # filled by the sign-off sheet
        DemoVideo(_NL, _nl(8), CATALOGUE[8].gtin),
        DemoVideo(_NL, _nl(8, " v2"), CATALOGUE[8].gtin),  # the second nl video: neither attaches
        DemoVideo(_FR, _fr(8), CATALOGUE[8].gtin),
        DemoVideo(_NL, _nl(9), CATALOGUE[9].gtin),
        DemoVideo(_NL, PROMO, "skip"),
        DemoVideo(_NL, MYSTERY, ""),
    ]
    return tuple(sorted(rows, key=lambda video: video.language != _NL))


def files() -> dict[str, list[str]]:
    """The placeholder files to put in each language's folder — one per mapping row."""
    found: dict[str, list[str]] = {}
    for video in videos():
        found.setdefault(video.language, []).append(video.file)
    return found


def mapping_text() -> str:
    """``mapping.yml`` in the shape the shell edits: one ``{file, gtin}`` row per line.

    Not ``yaml.safe_dump``: that writes block style, which the mapping editor refuses to touch —
    so a demo mapping written that way could not be rehearsed in the one place it exists for.
    """
    lines: list[str] = []
    for language in (_NL, _FR):
        lines.append(f"{language}:")
        lines += [
            f"- {{file: {video.file}, gtin: '{video.gtin}'}}"
            for video in videos()
            if video.language == language
        ]
    return "\n".join(lines) + "\n"


def signoff_rows() -> list[list[object]]:
    """The sheet the client sends back: a header, then one row per outcome the import decides.

    Barcodes are written 13-digit, as a person types them; the import canonicalises to 14.
    """
    return [
        ["language", "file", "current_gtin"],
        [_FR, _fr(6), CATALOGUE[6].gtin[1:]],  # change — fills the unset row
        [_FR, _fr(1), CATALOGUE[1].gtin[1:]],  # unchanged — already signed off exactly so
        # change — one video, two products: product 0 keeps it, product 7 shares it
        [_NL, _nl(0), f"{CATALOGUE[0].gtin[1:]}, {CATALOGUE[7].gtin[1:]}"],
        [_NL, PROMO, None],  # blank — not reached yet
        [_NL, MYSTERY, "8.7132E+12"],  # rejected — Excel's scientific notation
    ]
