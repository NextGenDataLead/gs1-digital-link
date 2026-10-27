"""Compare what is live on the site against what ``state.json`` says was published.

``state.json`` is the ledger: it records what *this machine* wrote, and every classification a
run makes reads from it. Nothing has ever checked it against the site, and the two diverge in
three ways — one of which the tool causes itself:

* **A run that fails part-way.** The real case that prompted this: a product published in Dutch
  and failed on French (a refused media upload). Sibling-blocking correctly held the product
  rather than half-publishing it, so **the row was recorded as an error and nothing was written
  to state** — while the Dutch page was live, correct and publicly reachable. The ledger had ten
  entries and the site had eleven tool-made pages, and nothing in the tool could say so. A later
  run classifies that product NEW; only the slug lookup inside the WordPress client stops it
  creating a duplicate.
* **Another machine.** The operator's copy of ``state.json`` and the maintainer's diverge the
  moment either publishes — see ``docs/operator-install.md``, "Returning the ledger".
* **A human in wp-admin.** A page deleted, trashed or unpublished by hand leaves its state entry
  claiming it is live.

So this module answers one question — *is what we believe what is there?* — in both directions,
from data. Fetching is the caller's job (``scripts/reconcile.py``); everything here is pure, so
the awkward cases are testable without a site.

**Read-only by construction.** Nothing here writes, and the script that feeds it only GETs. It
deliberately reports rather than repairs: every divergence it can find has more than one correct
resolution, and choosing between them needs a person who knows which machine published last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from lib.media_video import canon_gtin
from lib.records import State

#: WordPress post statuses that mean the page is publicly reachable.
_LIVE_STATUSES: Final = frozenset({"publish"})


class Divergence(StrEnum):
    """What kind of disagreement a row records.

    Named from the site's point of view — "live but unrecorded" rather than "state is missing an
    entry" — because the site is the thing that is true, and the ledger is the thing that can be
    wrong about it.
    """

    LIVE_NOT_RECORDED = "live_not_recorded"
    RECORDED_NOT_LIVE = "recorded_not_live"
    DIFFERENT_PAGE = "different_page"
    NOT_PUBLISHED = "not_published"


#: What each divergence means, and what it usually is — the sentence an operator reads.
EXPLANATIONS: Final[dict[Divergence, str]] = {
    Divergence.LIVE_NOT_RECORDED: (
        "a page exists on the site carrying this GTIN, and state has no entry for it. Usually a "
        "run that failed part-way (the page was written, the row was logged as an error, and "
        "nothing was recorded), or a publish from another machine whose state.json has not come "
        "back. The next run will classify this product as NEW."
    ),
    Divergence.RECORDED_NOT_LIVE: (
        "state says this was published and no page on the site carries the GTIN. Usually a page "
        "deleted or trashed by hand in wp-admin, or a state file that belongs to a different "
        "site. The next run will classify it UNCHANGED and leave the gap."
    ),
    Divergence.DIFFERENT_PAGE: (
        "state records a different page id than the one live at this GTIN. Two pages for one "
        "product, or a page recreated by hand. Whichever is right, the ledger is pointing at "
        "the wrong one."
    ),
    Divergence.NOT_PUBLISHED: (
        "the page exists but is not published — drafted or trashed. state's own wp_status may "
        "still say otherwise, in which case a run reads it as live and skips it."
    ),
}


@dataclass(frozen=True)
class LivePage:
    """One page found on the site, reduced to what a reconciliation needs.

    Attributes:
        gtin: The GTIN from ``meta.gtin``, canonicalised to 14 digits.
        language: The language the page was found under.
        page_id: The WordPress post id.
        slug: The page slug.
        status: The WordPress post status, verbatim.
        url: The page's permalink, where the site gave one.
    """

    gtin: str
    language: str
    page_id: int
    slug: str
    status: str
    url: str = ""

    @property
    def is_live(self) -> bool:
        """Whether this page is publicly reachable."""
        return self.status in _LIVE_STATUSES


@dataclass(frozen=True)
class Finding:
    """One `(GTIN, language)` where the site and the ledger disagree."""

    gtin: str
    language: str
    kind: Divergence
    detail: str

    @property
    def explanation(self) -> str:
        """What this kind of divergence usually means."""
        return EXPLANATIONS[self.kind]


@dataclass(frozen=True)
class Report:
    """The whole comparison: what was looked at, and what disagreed.

    ``checked`` matters as much as ``findings``. A report of zero findings over zero pages is
    what a wrong post type, a wrong language or an unauthenticated client produces, and it looks
    exactly like a clean site unless the denominator is on the page.
    """

    languages: list[str]
    live_pages: int
    state_entries: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def agrees(self) -> bool:
        """Whether the site and the ledger say the same thing."""
        return not self.findings

    def of_kind(self, kind: Divergence) -> list[Finding]:
        """Every finding of one kind."""
        return [f for f in self.findings if f.kind is kind]

    @property
    def summary(self) -> str:
        """One line: what was compared, and how it came out."""
        scope = (
            f"{self.live_pages} live page(s) across {', '.join(self.languages)} "
            f"vs {self.state_entries} state entr(ies)"
        )
        if self.agrees:
            return f"{scope} — they agree"
        counts = ", ".join(
            f"{len(self.of_kind(kind))} {kind.value}" for kind in Divergence if self.of_kind(kind)
        )
        return f"{scope} — {len(self.findings)} divergence(s): {counts}"


def reconcile(live: list[LivePage], state: State, languages: list[str]) -> Report:
    """Compare live pages against the ledger, in both directions.

    Args:
        live: Every tool-made page found on the site, across the languages checked.
        state: The ledger as loaded from ``state.json``.
        languages: The languages that were actually looked at. A language not in this list is
            not reported as missing — it was never checked, which is not the same thing.

    Returns:
        The report. Findings are ordered by GTIN then language, so two runs over an unchanged
        site produce identical output and a diff of two reports means something.
    """
    live_by_key = {(canon_gtin(page.gtin), page.language): page for page in live}
    state_by_key = {
        (canon_gtin(gtin), language): entry
        for gtin, entries in state.entries.items()
        for language, entry in entries.items()
        if language in languages
    }

    findings: list[Finding] = []
    for key in sorted(set(live_by_key) | set(state_by_key)):
        gtin, language = key
        page = live_by_key.get(key)
        entry = state_by_key.get(key)

        if page is not None and entry is None:
            findings.append(
                Finding(
                    gtin,
                    language,
                    Divergence.LIVE_NOT_RECORDED,
                    f"page {page.page_id} ({page.slug}) is on the site, state has no entry",
                )
            )
            continue
        if page is None and entry is not None:
            findings.append(
                Finding(
                    gtin,
                    language,
                    Divergence.RECORDED_NOT_LIVE,
                    f"state records page {entry.wp_page_id} at {entry.wp_url}, "
                    "nothing on the site carries this GTIN",
                )
            )
            continue
        if page is None or entry is None:  # pragma: no cover - the key came from one of them
            continue

        if page.page_id != entry.wp_page_id:
            findings.append(
                Finding(
                    gtin,
                    language,
                    Divergence.DIFFERENT_PAGE,
                    f"state records page {entry.wp_page_id}, the site serves {page.page_id} "
                    f"({page.slug})",
                )
            )
        elif not page.is_live:
            findings.append(
                Finding(
                    gtin,
                    language,
                    Divergence.NOT_PUBLISHED,
                    f"page {page.page_id} is {page.status!r} on the site; "
                    f"state records wp_status {entry.wp_status!r}",
                )
            )

    return Report(
        languages=list(languages),
        live_pages=len(live_by_key),
        state_entries=len(state_by_key),
        findings=findings,
    )
