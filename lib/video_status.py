"""Which in-scope products have a video in each language, and which videos have no product yet.

The join nothing else makes. :mod:`lib.media_video` is about the mapping file, keyed
``(language, file)``, and leaves a gap's ``gtin`` blank on purpose: a file nobody has assigned names
no product. :mod:`lib.holds` is keyed by publishable unit and stops at the first rule that holds
it, so it cannot say *which* language is missing. :mod:`lib.preflight` speaks in
``CheckResult``\\ s. So the sentence a client can act on — *this selected product is held, and it is
waiting on a French video* — had no producer, and somebody hand-made a file of that shape instead.

**What stops this being one more opinion about the hold.** There were six computations of it.
Here, a language counts as having a video exactly when :meth:`lib.media_video.VideoMap.resolve`
returns a file for it — the method ``run_execute`` calls to attach one. :data:`MISSING` and
:data:`CLASHING` only split that method's ``None`` in two, by the same canonical count
:func:`lib.media_video.check_video_map` reports ambiguity with. "Has a video here" and "gets a
video on the page" cannot drift apart, because the first is defined by the second.

**Held is the gate's word, and only the gate's.** :func:`lib.media_video.fully_mapped_gtins`
decides what may publish: a product with exactly one confirmed video in every language. So
:attr:`VideoStatus.held` is the products that do not attach a video everywhere — :data:`MISSING`
*or* :data:`CLASHING` in some language — which is that rule exactly, pinned by a test against it.
The gate used to admit a GTIN confirmed to two files in one language, which then published with no
video there; it holds one now, and :attr:`VideoStatus.clashing` is the subset the report names
with both filenames, so one can be marked ``skip``.

Pure: no config, no file I/O, no clock. The caller loads the mapping and lists the folders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from lib.media_video import CONFIRMED, VideoMap, canon_gtin, check_video_map, state_of
from lib.records import ProductRecord

#: No row confirms this product in this language.
MISSING: Final = "missing"
#: Exactly one file is confirmed to it — the product gets that video.
HAS_VIDEO: Final = "confirmed"
#: Two or more files are confirmed to it, so ``resolve`` picks none and the page gets no video.
CLASHING: Final = "clashing"


class UnassignedVideo(NamedTuple):
    """A file-side gap: it names a file, and no product."""

    language: str
    file: str


@dataclass(frozen=True)
class ProductVideo:
    """One in-scope product and where each of its languages stands.

    Attributes:
        gtin: GTIN-14, as :attr:`lib.records.ProductRecord.gtin14` spells it.
        name: The product name for a person — the first configured language that has one, else "".
        by_language: :data:`HAS_VIDEO`, :data:`MISSING` or :data:`CLASHING`, one per configured
            language.
        files: The filenames confirmed to it in each language, sorted. Two names under a
            :data:`CLASHING` language are the two to choose between.
    """

    gtin: str
    name: str
    by_language: Mapping[str, str]
    files: Mapping[str, tuple[str, ...]]

    @property
    def attaches_a_video(self) -> bool:
        """Whether every language gets a video on the page."""
        return all(state == HAS_VIDEO for state in self.by_language.values())

    def languages_in(self, state: str) -> list[str]:
        """The languages in ``state``, in configured order."""
        return [language for language, found in self.by_language.items() if found == state]


@dataclass(frozen=True)
class VideoStatus:
    """The product side and the file side of the video work, side by side and never joined.

    They cannot be joined: a file with no barcode names no product, which is the whole of why it
    is listed. The client's sign-off sheet is what joins them.

    Attributes:
        products: Every in-scope product, worst first (most languages without a video), then GTIN.
        unassigned: Rows in the mapping with no barcode filled in yet.
        not_in_map: Files in a video folder that have no row in the mapping.
        files_missing: Rows naming a file that is not in the folder.
        publish_without_video: ``media.publish_without_video`` — whether a missing video holds a
            product or only marks it. Carried here so :attr:`held` stays the gate's word: the same
            mapping holds 48 products under one setting and 2 under the other.
    """

    products: tuple[ProductVideo, ...]
    unassigned: tuple[UnassignedVideo, ...]
    not_in_map: tuple[UnassignedVideo, ...]
    files_missing: tuple[UnassignedVideo, ...]
    publish_without_video: bool = False

    @property
    def held(self) -> tuple[ProductVideo, ...]:
        """The products a run holds (E24): two videos competing for a language, always; and no
        video in some language, unless the client publishes without one."""
        if self.publish_without_video:
            return self.clashing
        return tuple(product for product in self.products if not product.attaches_a_video)

    @property
    def without_video(self) -> tuple[ProductVideo, ...]:
        """The products a run publishes with no video in at least one language.

        Empty unless :attr:`publish_without_video`: otherwise every such product is held instead.
        """
        if not self.publish_without_video:
            return ()
        return tuple(
            product
            for product in self.products
            if not product.attaches_a_video and not product.languages_in(CLASHING)
        )

    @property
    def clashing(self) -> tuple[ProductVideo, ...]:
        """The products with two videos confirmed in at least one language."""
        return tuple(product for product in self.products if product.languages_in(CLASHING))


def video_status(
    vmap: VideoMap,
    scoped: Sequence[ProductRecord],
    *,
    languages: Sequence[str],
    files_by_language: Mapping[str, list[str]],
    publish_without_video: bool = False,
) -> VideoStatus:
    """Join the in-scope products to the mapping, and list what the mapping cannot join.

    Args:
        vmap: The confirmed mapping.
        scoped: The products in scope — the selection list's, not the catalogue's.
        languages: The site's languages; a product needs a video in each.
        files_by_language: The filenames found in each language's video folder.
        publish_without_video: See :attr:`VideoStatus.publish_without_video`.
    """
    products = sorted(
        (_product(vmap, product, languages) for product in scoped),
        key=lambda p: (-sum(state != HAS_VIDEO for state in p.by_language.values()), p.gtin),
    )
    gaps: dict[str, list[UnassignedVideo]] = {}
    for issue in check_video_map(vmap, dict(files_by_language)):
        language = issue.field.partition(".")[2]
        gaps.setdefault(issue.issue, []).append(UnassignedVideo(language, issue.value))
    return VideoStatus(
        products=tuple(products),
        unassigned=tuple(sorted(gaps.get("video_unconfirmed", []))),
        not_in_map=tuple(sorted(gaps.get("video_missing_from_map", []))),
        files_missing=tuple(sorted(gaps.get("video_file_missing", []))),
        publish_without_video=publish_without_video,
    )


def waiting_on(product: ProductVideo) -> str:
    """What the product is waiting for, in a few words — "" when it gets a video everywhere.

    The Data screen's grid and the report both say this, so it is spelled once.
    """
    parts = []
    if missing := product.languages_in(MISSING):
        parts.append(f"no confirmed video in {', '.join(missing)}")
    parts.extend(f"two videos in {language}" for language in product.languages_in(CLASHING))
    return "; ".join(parts)


def _product(vmap: VideoMap, product: ProductRecord, languages: Sequence[str]) -> ProductVideo:
    gtin = product.gtin14
    by_language: dict[str, str] = {}
    files: dict[str, tuple[str, ...]] = {}
    for language in languages:
        confirmed = sorted(
            {
                entry.file
                for entry in vmap.by_language.get(language, [])
                if state_of(entry.gtin) == CONFIRMED and canon_gtin(entry.gtin) == gtin
            }
        )
        files[language] = tuple(confirmed)
        by_language[language] = _state(vmap, gtin, language, len(confirmed))
    return ProductVideo(
        gtin=gtin, name=_name(product, languages), by_language=by_language, files=files
    )


def _state(vmap: VideoMap, gtin: str, language: str, confirmed: int) -> str:
    """``resolve`` decides; the count only says why it said no."""
    if vmap.resolve(gtin, language) is not None:
        return HAS_VIDEO
    return CLASHING if confirmed > 1 else MISSING


def _name(product: ProductRecord, languages: Sequence[str]) -> str:
    values = product.product_name.values
    return next((values[lang] for lang in languages if values.get(lang)), "")
