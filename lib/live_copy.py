"""What the live site actually carries in the two generated fields, per ``(GTIN, language)``.

Every other answer to "does this product need copy?" in this codebase is derived from
``state.json`` — a ledger of what *this machine* wrote. That is fine for classifying a plan and
wrong for this question, for a reason that has nothing to do with correctness of the code: the
ledger does not travel. Two machines publish, two ledgers diverge, and neither can say what is on
the site. The site can.

So this module answers one question from the site alone: **for each product in scope, is there a
tagline and an Eigenschappen block live in every language?** No hashes, no fingerprints, no prior
run. A page that is missing, or live with those fields blank, needs copy written; a page that has
them does not. Whether the *inputs* moved since — the case a fingerprint would catch — is
deliberately not guessed at here. It is the operator's call, made by ticking the product.

**The read that tells the truth is a bare fetch by id.** On this WPML site, adding ``lang=`` to
either the listing or a single-post request makes the REST API answer with ``acf: []`` — an empty
array, for a page whose fields are populated and publicly rendering. Measured, not assumed: the
French pages all read as textless through the language-scoped listing and all carry their text
when fetched by id. A check built on the cheap path would have reported thirteen live, correct
pages as missing their copy, permanently and with confidence. ``context=edit`` is harmless; only
``lang`` does it.

Fetching is the caller's job (``scripts/report_live_copy.py``); everything here is pure, so the
awkward cases — no page, half a page, a field the client does not configure — are testable
without a site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.media_video import canon_gtin

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from lib.records import ProductRecord


@dataclass(frozen=True)
class LiveText:
    """The generated fields of one live page, as the site reports them.

    Attributes:
        gtin: The page's ``meta.gtin``, canonicalised to 14 digits.
        language: The language the page was listed under.
        page_id: The WordPress post id.
        filled: Configured generated fields carrying a non-empty value.
        empty: Configured generated fields that are blank on this page.
    """

    gtin: str
    language: str
    page_id: int
    filled: frozenset[str] = frozenset()
    empty: tuple[str, ...] = ()

    @property
    def has_text(self) -> bool:
        """Whether every configured generated field carries something."""
        return not self.empty


class Bucket(StrEnum):
    """What a product needs, from the site's point of view.

    Three states rather than two, because "needs copy" and "can be given copy" are different
    facts with different owners: one is a button on this machine, the other is a trip to MyGS1.
    Collapsing them produces a Generate button that cannot help the products it names.
    """

    #: Live in every language, with both generated fields present.
    HAS_TEXT = "has_text"
    #: Missing text somewhere, and the export carries something to write it from.
    NEEDS_TEXT = "needs_text"
    #: Missing text somewhere, and the export carries nothing to write it from.
    NO_INPUTS = "no_inputs"
    #: The plan will hold it whatever the site says — not eligible on the Data screen.
    HELD = "held"


@dataclass(frozen=True)
class ProductStatus:
    """One product in scope, and what the site says about it."""

    gtin: str
    name: str
    bucket: Bucket
    #: Languages whose page is absent, or present with a blank generated field.
    missing_languages: tuple[str, ...] = ()
    #: Languages with no page at all, a subset of :attr:`missing_languages`. Separate because the
    #: two read differently to an operator: no page is work the pipeline has not done yet, a blank
    #: field on a live page is work it did and lost — the ACF write path fails silently.
    absent_languages: tuple[str, ...] = ()

    @property
    def needs_generation(self) -> bool:
        """Whether the Generate button can do anything for this product."""
        return self.bucket is Bucket.NEEDS_TEXT


@dataclass(frozen=True)
class LiveCopyReport:
    """Every product in scope, bucketed, with the counts a screen leads with."""

    languages: tuple[str, ...]
    fields: tuple[str, ...]
    products: tuple[ProductStatus, ...]

    def _of(self, bucket: Bucket) -> tuple[ProductStatus, ...]:
        return tuple(product for product in self.products if product.bucket is bucket)

    @property
    def has_text(self) -> tuple[ProductStatus, ...]:
        """Nothing to do: live in every language with both fields present."""
        return self._of(Bucket.HAS_TEXT)

    @property
    def needs_text(self) -> tuple[ProductStatus, ...]:
        """The Generate button's whole job."""
        return self._of(Bucket.NEEDS_TEXT)

    @property
    def no_inputs(self) -> tuple[ProductStatus, ...]:
        """Missing text that no run on this machine can supply. A MyGS1 worklist."""
        return self._of(Bucket.NO_INPUTS)

    @property
    def held(self) -> tuple[ProductStatus, ...]:
        """Not eligible: the plan holds it, so no text written for it would ever be read."""
        return self._of(Bucket.HELD)

    @property
    def counts(self) -> dict[str, int]:
        """The figures, in one place so a screen and a CLI cannot disagree about them."""
        return {
            "in_scope": len(self.products),
            "has_text": len(self.has_text),
            "needs_text": len(self.needs_text),
            "no_inputs": len(self.no_inputs),
            "held": len(self.held),
        }


def has_copy_inputs(product: ProductRecord) -> bool:
    """Whether the export carries anything to write selling copy from.

    The rule is **attr 1083 (marketing message) or attr 1067 (feature/benefit), in any language**.
    Those two are the only source values that carry what a product is *for*; the rest of
    :class:`~lib.generator.GenerationInputs` — net content, the three dimensions, material — are
    facts, and facts are added to the page deterministically whether or not anything is generated.
    A product with neither is not a generation that will come out thin; it is a generation with no
    subject.

    **Any language, not this one.** A value the feed carries in Dutch and not French is a
    translation gap the producer fills, which is the whole reason ``translate: true`` exists — so
    asking per language would report a French unit as unwritable while the run was about to write
    it.
    """
    for localised in (product.description_short, product.description_long):
        if localised and any(str(value or "").strip() for value in localised.values.values()):
            return True
    return False


def classify(
    products: Iterable[ProductRecord],
    live: Mapping[tuple[str, str], LiveText],
    languages: Iterable[str],
    *,
    held: Iterable[str] = (),
) -> LiveCopyReport:
    """Bucket every product against what the site carries.

    Args:
        products: The products in scope — the process list's, not the whole catalogue.
        live: ``(gtin14, language) -> LiveText`` for every tool-made page found on the site.
            A missing key means no page in that language, which is not an error: it is the
            ordinary state of a product that has never published.
        languages: The client's configured languages. Every one is required for a product to
            count as done — a product live in Dutch and absent in French needs copy.
        held: GTINs the plan will hold (:func:`lib.holds.held_products`) — what the Data screen
            shows without a tick box. Bucketed :attr:`Bucket.HELD` before the site is consulted:
            the saved selection keeps those rows only so a run can name them, and treating them
            as chosen offered to write text for 3 held products in a batch of 3 ticked ones.
            Counted rather than dropped, so a hold still reads as outstanding work.

    Returns:
        The report. Products keep the order they were given, so a screen renders them in the
        order the operator's own list did.
    """
    languages = tuple(languages)
    holds = {canon_gtin(gtin) for gtin in held}
    fields = _configured_fields(live.values())
    statuses = []
    for product in products:
        gtin = product.gtin14
        missing, absent = [], []
        for language in languages:
            found = live.get((gtin, language))
            if found is None:
                missing.append(language)
                absent.append(language)
            elif not found.has_text:
                missing.append(language)
        bucket = Bucket.HELD if gtin in holds else _bucket(bool(missing), has_copy_inputs(product))
        statuses.append(
            ProductStatus(
                gtin=gtin,
                name=_name(product, languages),
                bucket=bucket,
                missing_languages=tuple(missing),
                absent_languages=tuple(absent),
            )
        )
    return LiveCopyReport(languages=languages, fields=fields, products=tuple(statuses))


def _bucket(missing: bool, inputs: bool) -> Bucket:
    """Has text / needs it and can have it / needs it and cannot."""
    if not missing:
        return Bucket.HAS_TEXT
    return Bucket.NEEDS_TEXT if inputs else Bucket.NO_INPUTS


def _name(product: ProductRecord, languages: tuple[str, ...]) -> str:
    """A product name in whichever configured language carries one, for a screen to show.

    Falls back to the empty string rather than to the GTIN: the GTIN is already its own column,
    and a name column repeating it reads as a name.
    """
    for language in languages:
        value = product.product_name.values.get(language)
        if value and value.strip():
            return value.strip()
    return ""


def _configured_fields(found: Iterable[LiveText]) -> tuple[str, ...]:
    """Every generated field name the fetch looked at, for a report to name them."""
    fields: set[str] = set()
    for text in found:
        fields |= set(text.filled) | set(text.empty)
    return tuple(sorted(fields))
