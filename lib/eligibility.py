"""Which products of a batch are eligible to publish, why the rest are not, and which lack a video.

The Data screen's funnel — *in product list → eligible → selected* — needs three answers about each
product the export carries: may it publish at all, and if not why, and if so is it missing a video
somewhere. Each already has exactly one producer, and this module only joins them:

* **not eligible, and why** is :func:`lib.holds.held_products` — the plan's own rules in the plan's
  own order (E23 missing data, E24 the video gate, E22 no image), with the E23 gaps generation
  cannot close. A screen that re-derived "eligible" would sooner or later offer a tick box on a
  product the plan then drops in silence;
* **the video words** are :func:`lib.video_status.waiting_on`, so the screen and report §1 say the
  same thing about the same product ("no confirmed video in fr", "two videos in nl").

A barcode on the list that the export does not carry is not a question for this module: it has no
record to ask about. The screen lists those itself.

**A links-only batch is judged on one thing: its link.** It writes a GS1 record pointing at a page
that already exists and nothing else — no page, so no copy, no video, no image, and no mandatory
page field matters (operator, 2026-10-09). What can be wrong is the target: there is none (no
*Link naar site* and no page of ours), it is not on the client's site, or it does not load
(:mod:`lib.link_targets`). Those go in :attr:`Eligibility.bad_link`, and are the screen's own table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from lib.errors import VideoMapError
from lib.gates import Mode
from lib.holds import held_products
from lib.link_targets import Problems, host_problem
from lib.live_inventory import live_products
from lib.preflight import load_video_status
from lib.records import SkipReason
from lib.video_status import CLASHING, ProductVideo, waiting_on

if TYPE_CHECKING:
    from lib.config import ClientConfig
    from lib.records import ProductRecord, State

#: What a product held for want of a source image is called on screen.
NO_IMAGE: Final = "no product image in the export"

#: What the screen says when the mapping will not load, so no product can be judged on video.
VIDEO_UNKNOWN: Final = "the video mapping could not be read"

#: A links-only product with nowhere to point: no address listed, and no page of ours.
NO_TARGET: Final = "no page to link to — fill in Link naar site, or publish its page first"

#: A links-only product whose address has not been checked yet.
CHECKING: Final = "checking the link…"


@dataclass(frozen=True)
class LinkIssue:
    """Why a links-only product cannot run: its target, and what is wrong with it."""

    url: str | None
    problem: str


@dataclass(frozen=True)
class Eligibility:
    """The batch's products, split the way the funnel needs them. Keyed by GTIN-14.

    Attributes:
        not_eligible: ``{gtin14: why}`` for every product a run will hold — the plan's reason in a
            few words. Everything else in the export may publish.
        missing_video: ``{gtin14: what it waits on}`` for the *eligible* products that publish with
            no video in at least one language (``media.publish_without_video``).
        bad_link: ``{gtin14: issue}`` — links-only batches only: products whose GS1 record would
            have no target, or one that does not load. Including those still being checked.
        problem: Set when the holds could not be decided at all; then nothing is called eligible.
    """

    not_eligible: dict[str, str] = field(default_factory=dict)
    missing_video: dict[str, str] = field(default_factory=dict)
    bad_link: dict[str, LinkIssue] = field(default_factory=dict)
    problem: str | None = None

    def is_eligible(self, gtin14: str) -> bool:
        """Whether a product the export carries may publish."""
        return (
            self.problem is None and gtin14 not in self.not_eligible and gtin14 not in self.bad_link
        )


def link_targets(cfg: ClientConfig, listed: dict[str, str], state: State) -> dict[str, str]:
    """Where each product's GS1 record would point: ``{gtin14: url}``.

    The address the operator listed wins — it names a page this tool did not make; otherwise the
    default-language page this tool published. A product with neither is absent.
    """
    default = cfg.wordpress.default_language
    ours = {
        product.gtin.zfill(14): page.url
        for product in live_products(state)
        for page in product.pages
        if page.language == default and page.url
    }
    return {**ours, **listed}


def links_eligibility(
    cfg: ClientConfig,
    products: list[ProductRecord],
    targets: dict[str, str],
    checked: Problems | None,
) -> Eligibility:
    """Split a links-only batch: a product may run when its target is on the site and loads.

    ``checked`` is :func:`lib.link_targets.check_targets` over ``targets``' addresses, or ``None``
    while that is still running — then every product with an address reads :data:`CHECKING`, and
    none is eligible until it has been looked at.
    """
    bad: dict[str, LinkIssue] = {}
    for product in products:
        gtin14 = product.gtin14
        url = targets.get(gtin14)
        if url is None:
            bad[gtin14] = LinkIssue(None, NO_TARGET)
            continue
        problem = host_problem(url, cfg.wordpress.site_url)
        if problem is None:
            problem = CHECKING if checked is None or url not in checked else checked[url]
        if problem is not None:
            bad[gtin14] = LinkIssue(url, problem)
    return Eligibility(bad_link=bad)


def eligibility_for(  # noqa: PLR0913 — the mode picks which inputs matter
    cfg: ClientConfig,
    products: list[ProductRecord],
    mode: Mode | None,
    *,
    targets: dict[str, str] | None = None,
    checked: Problems | None = None,
) -> Eligibility:
    """:func:`eligibility` for a batch publishing pages, :func:`links_eligibility` for links."""
    if mode is Mode.LINKS:
        return links_eligibility(cfg, products, targets or {}, checked)
    return eligibility(cfg, products)


def eligibility(cfg: ClientConfig, products: list[ProductRecord]) -> Eligibility:
    """Split ``products`` — those the batch's list names and the export carries — for the funnel."""
    try:
        holds = held_products(cfg, products)
    except VideoMapError as exc:
        # Fail closed: a run with an unreadable mapping and the rule on would hold everything.
        return Eligibility(problem=f"{VIDEO_UNKNOWN}: {exc}")
    status = load_video_status(cfg, products)
    words = (
        {p.gtin: _held_for(p, status.publish_without_video) for p in status.products}
        if status
        else {}
    )
    by_gtin = {product.gtin: product.gtin14 for product in products}
    # The video gate only holds anything when the rule is on; otherwise its words are not a reason.
    enforced = cfg.media is not None and cfg.media.restrict_to_mapped_gtins

    not_eligible: dict[str, str] = {}
    for gtin, hold in holds.items():
        gtin14 = by_gtin[gtin]
        video = words.get(gtin14, "") if enforced else ""
        if hold.reason is SkipReason.MISSING_MANDATORY_FIELD:
            reasons = ["missing data: " + ", ".join(gap.label for gap in hold.gaps), video]
        elif hold.reason is SkipReason.NO_CONFIRMED_VIDEO:
            reasons = [video or "no confirmed video"]
        else:
            reasons = [NO_IMAGE, video]
        # Every reason, not only the first rule that fires: the plan attributes a hold to one rule,
        # but the operator has to fix all of them before the product is eligible — and report §1b
        # lists a two-video product whatever else holds it, so the screen must too.
        not_eligible[gtin14] = "; ".join(reason for reason in reasons if reason)

    bare = status.without_video if status is not None else ()
    missing_video = {p.gtin: waiting_on(p) for p in bare if p.gtin not in not_eligible}
    return Eligibility(not_eligible, missing_video)


def _held_for(product: ProductVideo, publish_without_video: bool) -> str:
    """Why the video gate holds ``product``, naming only what actually holds it.

    Under ``media.publish_without_video`` a missing language holds nothing, so "no confirmed video
    in fr; two videos in nl" would send the reader after a video that would not unblock it.
    """
    if not publish_without_video:
        return waiting_on(product)
    clashing = product.languages_in(CLASHING)
    return "; ".join(
        f"two videos in {language} — the client must keep one" for language in clashing
    )
