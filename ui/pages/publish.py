"""Screen 5 — the gated publish. The screen this whole shell exists for.

Every gate in ``lib.gates`` is rendered here as a form, in step order, and
:class:`ui.session.PublishSession` refuses to build a command while a required one is outstanding.
That refusal is the improvement over prose: a paragraph can be paraphrased, compressed or skipped
when the context is long, and a function that raises cannot.

The screen adds nothing to the contract. It renders the gates the contract declares, in the order
it declares, with the reason each exists shown alongside the question — a form that asks without
saying why teaches an operator to answer without reading, and this flow's whole cost is
concentrated in one unreviewed click.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Final

from nicegui import context as nicegui_context
from nicegui import events, ui

from lib.gates import PERMANENCE_WARNING, REVERSIBLE_NOTE, Gate, GateOption, Mode
from lib.records import PlanClassification, PlanRow, SkipReason
from lib.result_sheet import load_outcomes
from lib.run_files import newest_log
from ui import DATA_ROOT, context, publish_outcome, runner, theme
from ui.session import GateNotAnsweredError, PublishSession

#: Scroll to an element once the page has stopped moving under it.
#:
#: A plain ``scrollIntoView`` here lands in the wrong place, and not by a little: the rebuilt
#: elements and this script reach the browser at almost the same moment, Vue applies the DOM on
#: its own tick, and a scroll issued against the layout that exists *right now* aims at where the
#: gate used to be. Measured on the real plan it landed 562px short — far enough that the gate it
#: was supposed to bring into view was still below the fold. Waiting a fixed number of frames only
#: moves the race.
#:
#: So this waits for the thing it is aiming at to hold still: poll each frame until the element's
#: document position is the same two frames running, then scroll. Bounded, and it gives up
#: silently — a scroll that does not happen costs the operator a scroll, which is what they had
#: before this existed.
_SCROLL_WHEN_SETTLED: Final = """
(() => {
  let previous = null, frames = 0;
  const step = () => {
    const element = document.getElementById('ANCHOR');
    const top = element ? Math.round(element.getBoundingClientRect().top + window.scrollY) : null;
    if (element && top === previous) {
      element.scrollIntoView({behavior: 'smooth', block: 'start'});
      return;
    }
    previous = top;
    if (++frames < 60) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
})()
"""


#: How much of a failed real run's output the outcome dialog shows — the tail, where it says why.
_FAILURE_LINES: Final = 40


def render() -> None:
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Publish",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Publish"),
            "Publish",
            "One gate at a time. Nothing is written until every required one is answered.",
        )
        if cfg is None or cid is None:
            theme.blocked(
                "clients.yml did not load, so this screen has nothing to work from.",
                link_label="Open Setup →",
                route="/setup",
            )
            return
        if cfg.process_list is not None and context.batch_mode(cfg) is None:
            # The mode is chosen with the batch now, on Data. Defaulting it here would be the one
            # guess this screen must never make: half the modes are permanent.
            theme.blocked(
                "This batch does not say what it publishes yet. Choose pages, links or both on the "
                "Data screen (step 4) and press Next there.",
                link_label="Open Data →",
                route="/data",
            )
            return
        _Flow(cid, cfg).build()


#: What each plan count actually decides, under its own figure. The four words are the pipeline's
#: vocabulary, not the operator's, and three of them are about what will *not* happen: UNCHANGED
#: and HELD are both zero-work and only one of them is finished. An operator reading "New: 0"
#: alone concludes there is nothing to do — which is exactly the misreading gate 5 exists to stop.
_COUNT_MEANING: Final = {
    PlanClassification.NEW: "pages that do not exist yet",
    PlanClassification.CHANGED: "live pages this run would rewrite",
    PlanClassification.UNCHANGED: "already live and identical — skipped",
    # **Not** the "held" the doctor and ``lib.holds`` report. That one is a product blocked by
    # missing mandatory data or an unconfirmed video (E23/E24/E22), and those never become rows at
    # all — they are the "never became rows" band under these counts.
    # :class:`~lib.records.PlanClassification.HELD` is a product somebody **deliberately took
    # down** with ``run_unpublish``, whose hashes still match and which would therefore classify
    # UNCHANGED and be quietly republished. One word, two meanings, in one tool — so the figure
    # says which one rather than leaving the operator to carry the distinction.
    PlanClassification.HELD: "unpublished on purpose — a routine run never puts it back",
}


class _Flow:
    """The gate walk for one run, held together by a :class:`PublishSession`."""

    def __init__(self, cid: str, cfg: Any) -> None:
        self.cid = cid
        self.cfg = cfg
        self.production = context.is_production(cfg)
        #: Whether the batch carries its mode (chosen on Data). Only a client with no selection
        #: list — nothing to save a mode with — still picks it here.
        self.mode_from_batch = cfg.process_list is not None
        self.session = PublishSession(
            client_id=cid,
            mode=context.batch_mode(cfg) or Mode.PAGES,
            has_generator=cfg.generator is not None,
            is_production=self.production,
            languages=list(cfg.wordpress.languages),
        )
        #: Whether the dry run has been run at all. Its Proceed/Cancel buttons appear only after
        #: there is output to approve — offering them beforehand invites approving nothing.
        self.has_run_dry = False
        #: The dry run's output, kept so a redraw shows it again. The redraw that offers
        #: Proceed/Cancel used to rebuild the log empty and hidden, so the output the gate asks
        #: the operator to read was gone the moment it finished (operator, 2026-10-07).
        self.dry_log: list[str] = []
        #: The real run's output, once there is one — and the sign that this walk has run. The
        #: button stayed pressable after a real run; a walk runs for real once.
        self.real_log: list[str] | None = None
        #: Whether the next plan re-admits already-published GTINs. Screen state rather than a
        #: gate answer: it changes what the plan *contains*, so it is chosen before the plan is
        #: built and re-chosen for every rebuild, not carried as a decision already made.
        self.include_published = False
        #: The last `doctor --json --offline` payload, refreshed once per redraw. Two gates read
        #: it; before this it was fetched inside gate 3's renderer, so gate 0 had no scope figure
        #: to show and adding one there would have meant a second ~250 ms blocking subprocess per
        #: redraw. One call, one answer, and the gates cannot disagree about the same run.
        self.doctor: Any = None
        #: Whether the body has been built once. The first build is not a redraw, and scrolling
        #: on arrival would move a screen the operator has not read yet.
        self.drawn = False
        #: Bound once, while the page is still being built. ``ui.run_javascript`` resolves its
        #: client from whatever slot is open at the time, and by the time a redraw wants to scroll
        #: it is running under a slot whose element the redraw has just deleted — which raises
        #: "The parent element this slot belongs to has been deleted" **into a background task**,
        #: where nothing on screen shows it. The scroll silently did not happen, and the only
        #: evidence was in the terminal the operator does not read.
        self.client = nicegui_context.client
        self.body = ui.column().classes("w-full gap-6")

    # -- assembly -------------------------------------------------------------

    def build(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        # Before anything reads `session.gates`, and here rather than in `__init__`: the plan is
        # built *during* this walk, at gate 5, so a fact captured once when the screen was
        # constructed is the fact from before there was a plan — and the gate it decides would
        # never appear. Every path that can change the plan ends in a redraw (an answer, the mode
        # toggle, "Build the plan"), so this is the one place that has to be right.
        plan = context.load_plan(self.cid)
        self.session.units_missing_product_name = tuple(
            unit
            for unit in (plan.skipped if plan else ())
            if unit.reason is SkipReason.MISSING_PRODUCT_NAME
        )
        self.doctor, _ = runner.run_json(
            runner.doctor_argv(self.cid, offline=True, mode=self.session.mode)
        )
        self.body.clear()
        with self.body:
            if self.session.mode.is_permanent:
                theme.band(PERMANENCE_WARNING, "danger")
            else:
                theme.band(REVERSIBLE_NOTE)
            if self.session.answers:
                # The session is built fresh on every render of this screen, so opening Data to
                # check something and coming back silently discards every answer. That was always
                # true; it is worth saying now that the rail invites moving around. Said only once
                # there is something to lose, because a warning shown before it applies is a
                # warning that stops being read by the time it does.
                theme.band(
                    "Your answers live on this screen only. Opening another screen and returning "
                    "starts this walk again from the first gate.",
                    "warn",
                )
            for gate in self.session.gates:
                self._gate(gate)
            self._execute_panel()
        if self.drawn:
            self._scroll_to_next()
        self.drawn = True

    def _scroll_to_next(self) -> None:
        """Put the decision that is now next back under the operator's eyes.

        Answering a gate rebuilds every element on this screen, and the screen is long — twenty
        rows of diff at gate 6 alone. The old body scrolled away with the answer, so the reward
        for answering the gate in front of you was a jump to the top of a page whose next question
        is somewhere below the fold. On the longest gates that is most of a screen of scrolling
        per answer, which is exactly the pressure that produces clicking without reading.

        Nothing is scrolled on the first build: on arrival the top *is* the right place.
        """
        target = self.session.next_gate
        anchor = f"gate-{target.id}" if target else "execute"
        self.client.run_javascript(_SCROLL_WHEN_SETTLED.replace("ANCHOR", anchor))

    def _gate(self, gate: Gate) -> None:
        """One gate, open or folded.

        Eight cards stand on this screen in ``both`` mode, and they used to be open all at once —
        an answered gate kept its full body and dimmed to 55% opacity, so the decision the operator
        is *on* sat in a column of decisions already made and decisions that do not apply. That is
        the shape that reads as ceremony, and a flow that reads as ceremony is answered like one.

        Folded is not hidden: every folded gate opens on a click, keeps its **why**, and keeps its
        controls, so an answer can still be re-read or changed. What folding removes is the scroll
        between the operator and the question in front of them.

        The unanswered optional gates fold too — the languages filter, the post-run summary — but
        not one with work in it: gate 6 walks every changed row, and is optional only in the sense
        that a run can avoid reaching it. Collapsing the work the operator has just asked for would
        be the worst of both.
        """
        answered = gate.id in self.session.answers
        if self.session.folds(gate):
            self._folded_gate(gate, answered=answered)
            return
        with ui.element("div").classes("gate").props(f"id=gate-{gate.id}"):
            ui.label(f"STEP {gate.step}{' · REQUIRED' if gate.required else ''}").classes(
                "gate-step"
            )
            with ui.element("div").classes("head-row"):
                ui.label(gate.title).classes("gate-title")
                theme.explanation(gate.purpose, about=gate.title, rich=True)
            if gate.id != "intent":  # gate 0 is the mode and its buttons — see ``_gate_intent``
                ui.label(gate.summary).classes("gate-lede")
            self._gate_body(gate)

    def _folded_gate(self, gate: Gate, *, answered: bool) -> None:
        """The same gate as one line, opening to the whole thing.

        The caption carries the answer rather than a tick, because "answered" is not the fact the
        operator needs on a re-read — *which* answer is. ``new-only`` and ``all`` are both a green
        gate 5, and they publish different runs.
        """
        caption = (
            f"Step {gate.step} · {self._answer_label(gate)}"
            if answered
            else (f"Step {gate.step} · optional")
        )
        classes = "gate gate-folded w-full" + (" gate-done" if answered else "")
        with (
            ui.expansion(gate.title, caption=caption)
            .classes(classes)
            .props(f"id=gate-{gate.id} dense")
        ):
            with ui.element("div").classes("head-row"):
                ui.label(gate.summary).classes("gate-lede")
                theme.explanation(gate.purpose, about=gate.title, rich=True)
            self._gate_body(gate)

    def _answer_label(self, gate: Gate) -> str:
        """What was chosen, in the words the gate offered — or the raw answer for a gate with no
        options, which is the languages filter and nothing else."""
        chosen = self.session.chosen(gate.id)
        return chosen.label if chosen else self.session.answers.get(gate.id, "answered")

    def _gate_body(self, gate: Gate) -> None:
        """The controls, and nothing above them but one line.

        ``gate.purpose`` used to render here in full — seven of the nine are several sentences —
        so every gate opened with a slab of prose and the question underneath it. Eight of those
        at once is the screen the operator called over-complete, and prose nobody reads is worse
        than prose behind a press: it trains the eye to skip the region the warnings live in.

        It is still one press away, on the ⓘ beside the title, and rendered as Markdown there:
        the emphasis in those sentences is load-bearing — on *permanent*, on *how many products
        this run could touch*.
        """
        getattr(self, f"_gate_{gate.id}", self._gate_default)(gate)

    # -- per-gate bodies ------------------------------------------------------

    def _gate_default(self, gate: Gate) -> None:
        self._options(gate)

    def _gate_intent(self, gate: Gate) -> None:
        """Gate 0 is the mode and Confirm / Change / Cancel — nothing else.

        **The mode is chosen on the Data screen now** (operator, 2026-10-09), because it decides
        what a batch needs before anything is counted — a links-only batch skips Content and most
        of the preflight. So this gate *shows* the batch's mode and asks for it to be confirmed;
        changing it means going back to Data, where the selection is judged for the new mode. The
        confirmation stays: a mode remembered with a batch is not a run approved.

        Operator, 2026-10-07: "remove all info but the buttons from step 0". It carried the scope
        figures, the export path and age, the environment and three notes, and the one decision it
        asks for — which mode — was the thing that got missed: two dry runs went out in ``pages``
        after ``both`` was meant. The facts it dropped are still said where they act: the
        permanence band heads this screen whenever the mode writes to GS1, the rail names the
        environment, Preflight checked the scope and the export, gate 5 shows the rows, and gate 8
        asks about production on its own.
        """

        if self.mode_from_batch:
            mode = self.session.mode
            ui.label(f"This batch publishes {mode.value} — {mode.summary}").classes(
                "gate-title mb-3"
            )
            with ui.row().classes("gap-3 flex-wrap"):
                for option in gate.shell_options:
                    if option.value == "change-mode":
                        theme.quiet_action(
                            "Change on the Data screen", lambda: ui.navigate.to("/data")
                        ).tooltip("The mode is saved with the batch; choose again on Data")
                        continue
                    place = theme.action if option.proceeds else theme.quiet_action
                    place(
                        option.label,
                        lambda o=option: self._answer(gate.id, o.value),  # type: ignore[misc]
                    ).tooltip(option.consequence)
            chosen = self.session.chosen(gate.id)
            if chosen is not None:
                ui.label(f"Answered: {chosen.label}").classes("note mt-2")
            return

        def pick(value: str) -> None:
            self.session.mode = Mode(value)
            self._redraw()

        ui.toggle(
            {mode.value: f"{mode.value} — {mode.summary}" for mode in Mode},
            value=self.session.mode.value,
            on_change=lambda event: pick(event.value),
        ).props("no-caps").classes("mb-4")
        self._options(gate)

    def _gate_languages(self, gate: Gate) -> None:
        def choose(values: list[str]) -> None:
            # Clearing every language means "all", not "none". A run scoped to no language would
            # confirm nothing, publish nothing and report success — and an empty multi-select is
            # far more often a mis-click than a decision.
            self.session.languages = values or list(self.cfg.wordpress.languages)
            self.session.answers["languages"] = ",".join(self.session.languages)

        ui.select(
            list(self.cfg.wordpress.languages),
            value=list(self.session.languages),
            multiple=True,
            label="Languages this run covers",
            on_change=lambda event: choose(list(event.value)),
        ).props("outlined use-chips").classes("max-w-md")
        ui.label(
            "All of them unless you narrow it. Every other gate stays as it is when this changes."
        ).classes("note mt-2")

    def _gate_content_review(self, gate: Gate) -> None:
        """The same three figures the Content screen shows, from the same read of the same check.

        The first one was labelled **units in scope** and is not that: ``total`` counts the units
        this run needs copy *for* — NEW or CHANGED, minus what the plan will hold — which on a
        27-product scope is routinely 0. So the gate read "0 units in scope" at the moment the
        operator is forming their picture of the run, which is the defect gate 0 was fixed for
        once already: a number standing under a label describing something else.

        Worded identically to the Content screen on purpose. Two surfaces reading one payload and
        naming it differently is how an operator comes to believe they are two different numbers.
        """
        entry = context.doctor_check(self.doctor, "generation_results")
        data = (entry or {}).get("data") or {}
        summary = context.copy_summary(entry)
        if entry is not None and data.get("total") == 0:
            # Nothing to approve. Three zeroes and a "Copy is good" button is a required gate
            # asking a question with no content — the operator reads it as broken, or worse
            # answers it and learns that answering this gate means nothing. Say the state instead,
            # and say why: the sentence below names what is finished and what is stuck.
            theme.band(
                "Nothing to review — this run publishes no pages, so no tagline or Eigenschappen "
                "text was written for it. Confirm to carry on; there is nothing here to read."
            )
            if summary:
                ui.label(summary).classes("note mb-3")
            self._options(gate)
            return
        if entry is not None:
            with theme.figures():
                theme.figure(
                    str(data.get("total", "—")),
                    "pages to publish",
                    "pages this run creates or rewrites",
                )
                theme.figure(
                    str(data.get("covered", "—")),
                    "have text",
                    "of those, how many have a tagline and Eigenschappen for this export",
                )
                theme.figure(
                    str(data.get("pending", "—")),
                    "pending",
                    "no text yet — these are dropped from the run",
                )
            if entry["status"] != "ok":
                theme.band(summary or str(entry["detail"]), "danger")
            elif summary:
                theme.band(summary)
        ui.link("Read the tagline and Eigenschappen on the Content screen →", "/content").classes(
            "mono mb-3"
        )
        self._options(gate)

    def _gate_missing_field(self, gate: Gate) -> None:
        """Name every unit the plan dropped for a missing ``product_name``.

        The gate exists at all only because there is something to name — ``lib.gates`` does not
        return it otherwise — so this renderer never has to describe an empty case. That is the
        fix: it used to render on every run, offering "Skip this unit" beside no unit, where the
        only answer with any effect was the one that stops the run.

        Capped and counted rather than truncated silently, so a list longer than the cap reads as
        one. ``detail`` is printed verbatim: it is the sentence ``run_plan`` logged and wrote into
        ``plan.json``, and a second wording here would give the operator two records to reconcile.
        """
        units = self.session.units_missing_product_name
        theme.band(
            f"{len(units)} unit(s) were dropped before the plan was classified: the product "
            "carries no product_name in that language, so there is no title to publish under. "
            "They are not in the plan's counts, and confirming will not publish them.",
            "warn",
        )
        for unit in units[:_MAX_MISSING_LISTED]:
            ui.label(f"{unit.gtin} · {unit.language} — {unit.detail}").classes("mono")
        if len(units) > _MAX_MISSING_LISTED:
            ui.label(
                f"…and {len(units) - _MAX_MISSING_LISTED} more, all for the same reason."
            ).classes("note")
        ui.label(
            "Nothing in this tool can supply the name. Fixing it means filling product_name for "
            "that language in MyGS1, re-exporting, and building the plan again."
        ).classes("note my-3")
        self._options(gate)

    def _gate_plan_review(self, gate: Gate) -> None:
        plan = context.load_plan(self.cid)
        summary = context.load_plan_summary(self.cid)

        # Async, and the subprocess runs off the event loop, for the reason
        # `runner.run_off_the_loop` gives: a blocking call in a sync handler holds the loop until
        # the command has already finished, so the button never gets to show it is working.
        async def build_plan() -> None:
            result = await runner.run_off_the_loop(
                runner.run_plan_argv(
                    self.cid,
                    include_published=self.include_published,
                    links_only=self.session.mode is Mode.LINKS,
                )
            )
            # These rows are not the rows those decisions were made about. Carrying an *apply*
            # across a rebuild is consent to publish a row in a form the operator may never have
            # seen — the same remembered consent the production gate is enforced per run to avoid.
            self.session.clear_row_decisions()
            said = result.stderr.strip().splitlines()[-1] if result.stderr else "run_plan finished"
            if result.ok:
                theme.notify_ok(said)
            else:
                theme.notify_problem(said)
            self._redraw()
            # A gate that materialises *above* the one your hands are on is fine if it is
            # announced and bad if it is not: everything below it has just shifted down,
            # including the buttons this gate only now grew. Step 4 is not required, so nothing
            # else forces a look at it — which is exactly why it is said out loud.
            dropped = self.session.units_missing_product_name
            if dropped:
                theme.notify_warning(
                    f"{len(dropped)} unit(s) have no product_name in their language. The "
                    "missing-field gate (step 4) is now above this one and names them."
                )

        def toggle(event: events.ValueChangeEventArguments) -> None:
            self.include_published = bool(event.value)
            # Redraw so the command line above the button shows what will actually run. A
            # displayed command that does not match the one the button sends is worse than no
            # command at all.
            self._redraw()

        ui.label(
            "By default a product that is already published and resolvable is treated as "
            "finished and left out of the plan. Tick this when its source data changed after it "
            "went live — otherwise the plan comes back empty and the run writes nothing while "
            "reporting success."
        ).classes("note")
        ui.checkbox(
            "Re-plan products that are already published (rewrites live pages)",
            value=self.include_published,
            on_change=toggle,
        )
        theme.command(
            runner.run_plan_argv(
                self.cid,
                include_published=self.include_published,
                links_only=self.session.mode is Mode.LINKS,
            )
        )
        theme.action("Build the plan", build_plan)

        if summary is None or plan is None:
            ui.label("No plan yet. Build one to see the counts.").classes("note mt-3")
            return

        # E19 leads, above the counts. The counts alone read as a routine first run, and
        # confirming past them would rewrite every live page.
        if summary.state_reset_from_corrupt:
            theme.band(
                "Prior state was corrupt and has been reset"
                + (
                    f" (backup: {summary.state_corrupt_backup})"
                    if summary.state_corrupt_backup
                    else ""
                )
                + ". Every row therefore re-plans as NEW. Re-running them is idempotent — pages "
                "are matched by slug and updated in place, not duplicated — but this will rewrite "
                "live pages and resolver targets rather than skip them.",
                "danger",
            )

        # Beneath E19 and above the counts, for the same reason: it changes what a CHANGED row
        # means. Read from the summary rather than from ``self.include_published`` so it describes
        # the plan on screen — the checkbox may have been re-ticked since it was built.
        if summary.included_published:
            theme.band(
                "This plan re-admits products that are already published and resolvable. A "
                "CHANGED row here rewrites a LIVE page. Pages are matched by slug and meta.gtin "
                "and updated in place, not duplicated, and an untouched product still classifies "
                "UNCHANGED and is never executed.",
                "danger",
            )

        # The plan and the mode can part company: the mode is chosen at gate 0 and can be changed
        # after the plan was built. Said here because the run would be refused for it at the end.
        if plan.links_only and self.session.mode is not Mode.LINKS:
            theme.band(
                "This plan was built for a links-only run, so it keeps products with no generated "
                f"copy. It cannot run in {self.session.mode.value} mode — build the plan again.",
                "warn",
            )

        with theme.figures():
            for classification in PlanClassification:
                theme.figure(
                    str(plan.counts.get(classification, 0)),
                    classification.value,
                    _COUNT_MEANING.get(classification, ""),
                )

        # An empty plan is the failure this project keeps designing against: executing it would
        # report success having published nothing. Said here, permanently, rather than left to a
        # toast at the moment the run is refused.
        if not plan.rows:
            theme.band(
                "This plan has no rows. A run against it would write nothing and still report "
                "success — so nothing here will run until the plan has something in it. The "
                "reasons are above and on the Preflight screen.",
                "danger",
            )

        if plan.skipped:
            reasons: dict[str, int] = {}
            for unit in plan.skipped:
                reasons[unit.reason.value] = reasons.get(unit.reason.value, 0) + 1
            theme.band(
                f"{len(plan.skipped)} unit(s) never became rows and are NOT in the counts above: "
                + ", ".join(f"{n} {reason}" for reason, n in reasons.items())
                + ". Confirming will not publish them."
                # The overlap with step 4 is deliberate. This band's job is completeness against
                # the counts directly above it, so dropping the E18 units from its total would
                # make it disagree with `plan.skipped`, with `plan.summary.json` and with
                # run_plan's own stderr line, with nothing here to explain the gap. Step 4's job
                # is the decision. A pointer turns the duplication into navigation.
                + (
                    " The missing_product_name ones are named individually at step 4 above."
                    if self.session.units_missing_product_name
                    else ""
                ),
                "warn",
            )
        if summary.excluded:
            ui.label(
                "Excluded by the gates: "
                + ", ".join(
                    f"{n} {reason.replace('_', ' ')}" for reason, n in summary.excluded.items()
                )
            ).classes("note")

        self._options(gate)

    def _gate_row_diff(self, gate: Gate) -> None:
        """Every CHANGED row, each with its own Apply and Skip.

        **Keyed on the classification, never on ``row.diff``.** State records the prior ``title``
        and ``wp_url`` and nothing else, so a row whose change is in the product body carries no
        diff at all — and this gate used to select ``[row for row in plan.rows if row.diff]``. On
        the real 24-row pilot plan that displayed **one** row while confirming twenty. A per-row
        walk keyed the same way would have reproduced the blindness one control at a time, which
        is the trap worth naming: the fix is not the buttons, it is what they are asked about.

        Narrowed to the languages chosen at gate 2 as well, because a decision about a row the
        language subset will drop is a decision with no effect.
        """
        if self.session.answers.get("plan_review") != "changed-review":
            ui.label("Only shown when the plan review asks to walk the changed rows.").classes(
                "note"
            )
            return
        plan = context.load_plan(self.cid)
        languages = set(self.session.languages)
        changed = [
            row
            for row in (plan.rows if plan else ())
            if row.classification is PlanClassification.CHANGED
            and (not languages or row.language in languages)
        ]
        if not changed:
            ui.label("This plan has no changed rows in the chosen languages.").classes("note")
            return

        decisions = [self.session.row_applied(row.gtin, row.language) for row in changed]
        applied = decisions.count(True)
        skipped = decisions.count(False)
        theme.band(
            f"{len(changed)} changed row(s) · {applied} applied · {skipped} skipped · "
            f"{decisions.count(None)} not decided. Only the applied ones are published — a row "
            "left undecided is not. Every NEW row is confirmed either way."
        )

        # `show-full-diff` is the gate's own option, and here it lifts the row cap. The cap is not
        # a summary — every field of every row shown is already printed — so the only thing left
        # for "show me everything" to mean is the rows past it.
        full = self.session.answers.get(gate.id) == "show-full-diff"
        shown = changed if full else changed[:_MAX_DIFFS]
        options = {option.value: option for option in gate.shell_options}
        self._apply_all(shown, changed)
        for row in shown:
            self._row_decision(row, options)
        if len(changed) > len(shown):
            # Said plainly rather than counted quietly. With a control on every row, a capped list
            # drops rows out of the *decision* and not merely out of the display — which is the
            # same class of defect as the one this gate is being rebuilt to fix.
            theme.band(
                f"{len(changed) - len(shown)} further changed row(s) are not shown here, so they "
                "are not decided, so they will NOT be published. Choose “Show full diff” to bring "
                "them onto the screen.",
                "warn",
            )
        self._options(gate, exclude=_PER_ROW_OPTIONS)

    def _apply_all(self, shown: list[PlanRow], changed: list[PlanRow]) -> None:
        """One button for "apply every row below", when there is still one left to apply.

        It applies the rows on the screen and no others. Past the cap a row is not shown, and a
        bulk button deciding rows nobody saw is the defect the per-row walk exists to prevent —
        so with rows hidden the label says "shown", and "Show full diff" brings the rest on.
        """
        pending = [r for r in shown if self.session.row_applied(r.gtin, r.language) is not True]
        if not pending:
            return
        scope = "changed" if len(shown) == len(changed) else "shown"

        def apply_all() -> None:
            self.session.apply_rows(shown)
            self._redraw()

        theme.action(f"Apply all {len(shown)} {scope} row(s)", apply_all).tooltip(
            "Each of them is published, as if you had pressed Apply on every row below. "
            "Skip a row afterwards to take it out again."
        )

    def _row_decision(self, row: PlanRow, options: dict[str, GateOption]) -> None:
        """One CHANGED row: what changed, what was decided, and the two buttons that decide it.

        The labels and the tooltips come from the gate's own options, so the words an operator
        reads here are the words ``SKILL.md`` offers on the other surface. Apply is filled and
        Skip outlined by position rather than by ``proceeds``: both answers advance the walk, so
        the flag cannot tell them apart, and the one that puts a row into the run is the one that
        should look like it does.
        """
        decision = self.session.row_applied(row.gtin, row.language)
        with ui.element("div").classes("mb-4"):
            ui.label(f"{row.gtin} ({row.language}) — {row.title}").classes("mono")
            for line in _changes(row):
                ui.label(line).classes("note mono scroll-x")
            with ui.row().classes("gap-3 items-center mt-2"):
                theme.action(
                    options["apply"].label, lambda: self._decide(row, applied=True)
                ).tooltip(options["apply"].consequence)
                theme.quiet_action(
                    options["skip"].label, lambda: self._decide(row, applied=False)
                ).tooltip(options["skip"].consequence)
                ui.label(_DECISION_WORD[decision]).classes("note")

    def _decide(self, row: PlanRow, *, applied: bool) -> None:
        self.session.apply_row(row.gtin, row.language, applied=applied)
        self._redraw()

    def _gate_production(self, gate: Gate) -> None:
        theme.band(
            f"About to execute against PRODUCTION. This will make live changes to "
            f"{self.cfg.wordpress.site_url} and register permanent GS1 records.",
            "danger",
        )
        typed = (
            ui.input(label="Type the client id to confirm", placeholder=self.cid)
            .props("outlined")
            .classes("max-w-sm my-3")
        )

        def confirm() -> None:
            if typed.value.strip() != self.cid:
                theme.notify_problem("That is not the client id.")
                return
            self.session.answer("production", "confirm")
            self._redraw()

        with ui.row().classes("gap-3"):
            theme.action("Confirm production", confirm, danger=True)
            theme.quiet_action(
                "Switch to test", lambda: self._answer("production", "switch-to-test")
            )
            theme.quiet_action("Cancel", lambda: self._answer("production", "cancel"))

    def _gate_dry_run(self, gate: Gate) -> None:

        async def go() -> None:
            confirmed = self._write_confirmed()
            if confirmed is None:
                return
            try:
                argv = self.session.execute_argv(confirmed, dry_run=True)
            except GateNotAnsweredError as exc:
                theme.notify_warning(str(exc))
                return
            log.style("display:block")
            log.clear()
            self.dry_log = [" ".join(["python", *argv])]
            log.push(self.dry_log[0])

            def keep(line: str) -> None:
                self.dry_log.append(line)
                log.push(line)

            result = await runner.stream(argv, keep)
            if result.ok:
                theme.notify_ok("Dry run finished — now read it, then Proceed or Cancel")
            else:
                theme.notify_warning(f"Dry run exited {result.returncode}")
            # Running it is not answering it. The output is the thing to be approved, so the
            # answer comes from the operator below — this used to set "proceed" here, which made
            # the gate self-answering and left Cancel unreachable at the one gate whose whole
            # purpose is to be read before the real write.
            self.has_run_dry = True
            self._redraw()

        ui.label(
            "It builds no clients, needs no credentials and writes nothing. It cannot verify that "
            "resolver targets serve, and it cannot prove the ACF fields will land — so read it "
            "for what it is."
        ).classes("note mb-3")
        theme.action("Run the dry run", go)
        log = ui.log().classes("console mt-4").style("display:none")
        if self.dry_log:
            log.style("display:block")
            for line in self.dry_log:
                log.push(line)
        if self.has_run_dry:
            self._options(gate)
        else:
            ui.label("Run it, read the output, then answer.").classes("note mt-3")

    def _gate_post_run(self, gate: Gate) -> None:
        ui.link("Every run, with its per-row outcomes →", "/runs").classes("mono")
        ui.label(
            "That screen also compares the site against state.json, which is the one question a "
            "run log cannot answer: a row logged as an error may still have left a live page."
        ).classes("note")
        self._options(gate)

    # -- shared ---------------------------------------------------------------

    def _options(self, gate: Gate, *, exclude: frozenset[str] = frozenset()) -> None:
        # `shell_options`, not `options`: an option only the conversational surface can honour —
        # one that needs a model to read the run log — would otherwise become a button that does
        # not do what it says.
        #
        # `exclude` is for the options this screen renders *elsewhere* rather than not at all.
        # Gate 6's apply/skip are per-row, so putting them here too would offer one gate-wide
        # answer to a question asked twenty times.
        offered = [option for option in gate.shell_options if option.value not in exclude]
        if not offered:
            return
        with ui.row().classes("gap-3 flex-wrap"):
            for option in offered:
                # Filled for the answer that carries the flow on, outlined for the ones that do
                # not. Red is never used here: it belongs to the buttons that write.
                place = theme.action if option.proceeds else theme.quiet_action
                place(
                    option.label,
                    lambda o=option: self._answer(gate.id, o.value),  # type: ignore[misc]
                ).tooltip(option.consequence)
        chosen = self.session.chosen(gate.id)
        if chosen is not None:
            ui.label(f"Answered: {chosen.label}").classes("note mt-2")

    def _answer(self, gate_id: str, value: str) -> None:
        self.session.answer(gate_id, value)
        self._redraw()

    def _execute_panel(self) -> None:
        with ui.element("div").classes("card").props("id=execute"):
            ui.label("EXECUTE").classes("gate-step")
            ui.label("Write it").classes("gate-title")

            outstanding = self.session.outstanding
            if outstanding:
                theme.band(
                    "Still to answer: "
                    + ", ".join(f"{gate.title} (step {gate.step})" for gate in outstanding),
                    "warn",
                )
                return
            if self.session.cancelled:
                # Named rather than described. "A gate was answered with cancel" is a claim about
                # an unnamed gate, and it was reachable at gates the operator had not cancelled at
                # all — including one whose only button said "Show full diff".
                refused = [
                    f"{gate.title} (step {gate.step}) was answered “{option.label}”"
                    for gate in self.session.gates
                    if (option := self.session.chosen(gate.id)) is not None and option.refuses
                ]
                theme.band(
                    "Nothing will run: "
                    + "; ".join(refused)
                    + ". Answer it differently above to make the run available again.",
                    "quiet",
                )
                return
            plan = context.load_plan(self.cid)
            if plan is None or not plan.rows:
                theme.band(
                    "Every gate is answered, but the plan has no rows — so there is nothing to "
                    "run. Publishing an empty plan is the one outcome indistinguishable from "
                    "success, which is why it is refused rather than attempted.",
                    "danger",
                )
                return

            if self.real_log is not None:
                # This walk has run for real. Its output stays; the button does not come back.
                theme.band(
                    "This walk has run for real — every row is on the Runs screen. To publish "
                    "again, open Publish from the rail and start a new walk.",
                    "quiet",
                )
                done = ui.log().classes("console mt-4")
                for line in self.real_log:
                    done.push(line)
                return

            async def go() -> None:
                confirmed = self._write_confirmed()
                if confirmed is None:
                    return
                try:
                    argv = self.session.execute_argv(confirmed, dry_run=False)
                except GateNotAnsweredError as exc:
                    theme.notify_problem(str(exc))
                    return
                # Off before it starts, and it stays off: a second press during or after a real run
                # is a second real run.
                button.disable()
                started = time.time()
                log.style("display:block")
                log.clear()
                lines = [" ".join(["python", *argv])]
                log.push(lines[0])

                def keep(line: str) -> None:
                    lines.append(line)
                    log.push(line)

                result = await runner.stream(argv, keep)
                self.real_log = lines
                # No second subprocess for the result sheet. `run_execute` writes it itself, on
                # failure too. The dialog is built from this run's own log, not from the exit code
                # alone: the log is what each row actually did.
                newest = newest_log(self.cid)
                outcomes = (
                    load_outcomes(newest)[0]
                    if newest is not None and newest.stat().st_mtime >= started
                    else []
                )
                headline, body, kind = publish_outcome.verdict(
                    outcomes,
                    returncode=result.returncode,
                    permanent=self.session.mode.is_permanent,
                )
                # Redrawn rather than disabled: the button's own wrapper re-enables it when this
                # handler returns (found in rehearsal). The redraw replaces it with the "has run"
                # note and keeps the output. The dialog opens after, under the body, so the redraw
                # cannot take it with it.
                self._redraw()
                with self.body:
                    theme.announce(
                        headline,
                        body,
                        kind=kind,
                        detail="" if kind == "quiet" else "\n".join(lines[-_FAILURE_LINES:]),
                    )

            log = ui.log().classes("console mt-4").style("display:none")
            button = theme.action(
                f"Run {self.session.mode.value} for real",
                go,
                danger=self.session.mode.is_permanent,
            )

    def _write_confirmed(self) -> str | None:
        """Serialise the plan and the confirmed subset, and return the path.

        Which rows those are is :meth:`PublishSession.confirmed_pairs`, not a rule re-derived
        here: the screen used to hold half of it — the language intersection — and a module-level
        helper the other half, and the half on the screen was the half no test could reach.
        """
        plan = context.load_plan(self.cid)
        if plan is None:
            theme.notify_warning("No plan to confirm. Build one at the plan review gate.")
            return None

        pairs = self.session.confirmed_pairs(plan)
        if not pairs:
            theme.notify_warning(
                "Nothing is confirmed — every row was filtered out by the plan choice, by the "
                "language selection, or (under Review changed) by being skipped or never "
                "decided. Not running."
            )
            return None

        path = DATA_ROOT / "output" / self.cid / "plan.confirmed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"plan": plan.model_dump(mode="json"), "confirmed_gtins_by_lang": pairs},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(Path("output") / self.cid / "plan.confirmed.json")


_MAX_DIFFS = 50
#: Its own constant rather than a reuse of `_MAX_DIFFS`: the row-diff cap can be lifted by that
#: gate's `show-full-diff` option, and this one cannot, so sharing a number would imply a
#: symmetry that does not exist.
_MAX_MISSING_LISTED = 50

#: Gate 6's options that belong on a row rather than on the gate.
_PER_ROW_OPTIONS: Final = frozenset({"apply", "skip"})

#: A row's decision, in a word. `None` is its own state and not a quieter "skipped": the operator
#: has to be able to see which rows they have not looked at yet.
_DECISION_WORD: Final = {True: "Applied", False: "Skipped", None: "Not decided"}


def _changes(row: PlanRow) -> list[str]:
    """What changed on one row, worded as `SKILL.md` §10.6.2 words it.

    Verbatim from the skill so both surfaces say the same thing about the same row, and because
    each of the three cases means something different that a bare `Changes:` header would flatten:

    * fields present — the only two state records, so the only two with a real before and after;
    * **no diff at all** — the change is in the product body, which state does not retain. This is
      the common case, not the exotic one: 19 of 20 CHANGED rows on the pilot plan;
    * a `gs1_link` key — the page is published and its resolver link was never written. Nothing
      about the page is changing, which is why it does not read as a content change.
    """
    return _change_lines(row) + (
        # Said on the row it applies to: Apply on this one rewrites the title and the data, not
        # the tagline or the description — no copy was written for it this run.
        ["Text: keeps the tagline and description already on the page"]
        if row.keeps_live_copy
        else []
    )


def _change_lines(row: PlanRow) -> list[str]:
    diff = row.diff or {}
    if not diff:
        return ["Changes: product content (no title or URL change)"]
    if "gs1_link" in diff:
        return ["Changes: resolver link not written yet"]
    return ["Changes:"] + [
        f"  {field}: {before} → {after}" for field, (before, after) in diff.items()
    ]
