"""One publish run's gate answers, and the refusal to build a command without them.

This is the part of the shell that matters. Everything else here is a nicer way to look at files;
this is the safety contract, and it is enforced **in code** rather than in prose — which is the
one thing the existing skill-based flow cannot do. `CLAUDE.md` is blunt about the cost of prose:
the operator gates live only in a Markdown file, and calling `run_execute` directly bypasses every
one of them. A required checkbox that a function refuses to proceed without is a stronger gate
than a paragraph a model might paraphrase, compress, or skip when the context is long.

The rule is one sentence: :meth:`PublishSession.execute_argv` raises unless every *required*
applicable gate has been answered with an option that proceeds. Not "warns". Not "logs". A shell
that could be talked into building the command anyway would be prose again, wearing a form.

Gate applicability, ordering and the argv itself all come from :mod:`lib.gates`, which
``flow-orchestrator/SKILL.md`` is checked against. Nothing here decides which gates exist.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from lib.gates import BY_ID, Gate, GateOption, Mode, gates_for, run_execute_argv
from lib.records import Plan, PlanClassification, PlanRow, SkippedUnit


def _named(gates: list[Gate]) -> str:
    """The gates in a refusal message, each with the step number the operator saw."""
    return ", ".join(f"{gate.title} (step {gate.step})" for gate in gates)


class GateNotAnsweredError(RuntimeError):
    """A required gate has no answer, or was answered with something that does not proceed.

    A distinct type rather than a bare ``RuntimeError`` so a screen can catch exactly this and
    show the operator which gate is outstanding, instead of a traceback that says nothing.
    """


@dataclass
class PublishSession:
    """The gate answers collected so far for one publish run.

    Deliberately mutable and deliberately **not** persisted. A run's confirmations are about
    *this* run: the plan it was shown, the counts it was shown, the environment it named. Reusing
    them for the next run would be the "remembered consent" failure the production gate is
    explicitly enforced per-run to avoid.
    """

    client_id: str
    mode: Mode
    has_generator: bool
    is_production: bool
    #: The languages this run covers. Empty means "not chosen yet"; the languages gate fills it.
    languages: list[str] = field(default_factory=list)
    #: gate id → the option value the operator picked.
    answers: dict[str, str] = field(default_factory=dict)
    #: The units this run's plan dropped for a missing ``product_name`` (E18), as the plan
    #: recorded them. Not an answer and not a file read: a *fact about this run*, the same kind
    #: of thing as ``has_generator`` and ``is_production``, and it decides whether the
    #: missing-field gate is in the walk at all.
    #:
    #: The units themselves rather than a bare flag, on purpose. The screen both hides the gate
    #: and names the units it is about; if those two came from separate reads of ``plan.json``
    #: they could disagree, and the way they would disagree is the gate rendering over an empty
    #: list — the very defect this field exists to fix, with a condition on top. One value, one
    #: truth.
    #:
    #: Defaulted, unlike ``gates_for``'s parameter, because a session begins before there is a
    #: plan. That costs nothing here: the screen refreshes it on every redraw, and the drift
    #: protection lives one call down, at the contract boundary.
    units_missing_product_name: tuple[SkippedUnit, ...] = ()
    #: ``(gtin, language)`` → whether the operator applied that CHANGED row at gate 6.
    #:
    #: A pair that is **absent is undecided, and undecided is not confirmed**. That default is the
    #: whole point: the alternative — treating an untouched row as applied — is exactly the defect
    #: this replaces, where answering *Review changed* confirmed every CHANGED row without the
    #: operator having seen one of them.
    #:
    #: In the session rather than in a file, for the reason the class docstring gives about every
    #: other answer here: a decision to rewrite a live page is about *this* plan, shown at *this*
    #: moment. :meth:`clear_row_decisions` is what a rebuilt plan calls.
    applied_rows: dict[tuple[str, str], bool] = field(default_factory=dict)

    @property
    def gates(self) -> tuple[Gate, ...]:
        """The gates that fire for this run, in step order.

        Recomputed on every access rather than cached, which matters now that one of the inputs
        is a fact about the plan: the plan is built at step 5, in the middle of the walk, so the
        answer to "which gates?" legitimately changes partway through.
        """
        return gates_for(
            mode=self.mode,
            has_generator=self.has_generator,
            is_production=self.is_production,
            has_missing_product_name=bool(self.units_missing_product_name),
        )

    def answer(self, gate_id: str, value: str) -> None:
        """Record the operator's choice at one gate.

        Raises:
            KeyError: If ``gate_id`` is not a gate, or ``value`` is not one of its options. Both
                are programming errors in a screen, and both would otherwise produce a session
                that looks answered and is not.
        """
        gate = BY_ID[gate_id]
        if gate.options and value not in {option.value for option in gate.options}:
            raise KeyError(f"{value!r} is not an option at gate {gate_id!r}")
        self.answers[gate_id] = value

    def apply_row(self, gtin: str, language: str, *, applied: bool) -> None:
        """Record the operator's decision about one CHANGED row at gate 6."""
        self.applied_rows[gtin, language] = applied

    def apply_rows(self, rows: Iterable[PlanRow]) -> None:
        """Apply every row in ``rows`` — the shown ones, never the ones past the cap.

        A row the operator has not had on the screen is not decided by a button about the rows
        they have: that is the cap's whole point (see the publish screen's row-diff gate).
        """
        for row in rows:
            self.apply_row(row.gtin, row.language, applied=True)

    def row_applied(self, gtin: str, language: str) -> bool | None:
        """``True`` applied, ``False`` skipped, ``None`` not decided — and the third is not the
        second. A screen has to be able to say "you have not looked at this one yet"."""
        return self.applied_rows.get((gtin, language))

    def clear_row_decisions(self) -> None:
        """Forget every per-row decision. Called when the plan is rebuilt.

        A decision carried across a rebuild is consent to publish a row the operator reviewed in
        a form it may no longer have — the same "remembered consent" the production gate is
        enforced per run to avoid, one zoom level down.
        """
        self.applied_rows.clear()

    def chosen(self, gate_id: str) -> GateOption | None:
        """The option picked at ``gate_id``, or ``None`` when it has not been answered."""
        value = self.answers.get(gate_id)
        if value is None:
            return None
        return next((o for o in BY_ID[gate_id].options if o.value == value), None)

    def production_acknowledged(self) -> bool:
        """Whether someone confirmed, at a gate, that this run writes to production.

        In ``links`` and ``both`` the production gate asks outright, and this is its answer.

        In ``pages`` that gate is **not in the walk** — nothing irreversible follows, so making
        the operator type the client id for a reversible run would be ceremony — and gate 0
        stands in: it is required, and it states the environment and the mode before anything
        else. That substitution is what ``CLAUDE.md`` already describes, and until it was
        implemented here the flag could never be set in ``pages`` mode, so ``run_execute``
        refused every real pages run against a production client. The reversible half of a
        publish was unreachable from this shell.

        Never derived from ``gs1.environment``. The flag records that a person confirmed, not a
        fact about the config — deriving it would turn a decision into a description.
        """
        if not self.is_production:
            return False
        if any(gate.id == "production" for gate in self.gates):
            return self.proceeded("production")
        return self.proceeded("intent")

    def proceeded(self, gate_id: str) -> bool:
        """Whether ``gate_id`` was answered with an option that lets the run continue.

        A gate with no fixed options — ``languages`` — counts as proceeded once answered at all.
        """
        gate = BY_ID[gate_id]
        if not gate.options:
            return gate_id in self.answers
        option = self.chosen(gate_id)
        return option is not None and option.proceeds

    def refused(self, gate_id: str) -> bool:
        """Whether ``gate_id`` was answered with an option that **stops the run**.

        Deliberately not ``not proceeded(...)``. An option that re-presents its own gate —
        ``show-full-diff``, ``change-mode``, ``regenerate`` — does not carry the flow on and does
        not refuse either, and reading the first as the second is what made the one button the
        shell can render at gate 6 cancel the run irrecoverably. :class:`~lib.gates.GateOutcome`
        holds that distinction so this file does not have to re-derive it.
        """
        option = self.chosen(gate_id)
        return option is not None and option.refuses

    @property
    def outstanding(self) -> tuple[Gate, ...]:
        """The required gates still standing between this session and a run."""
        return tuple(gate for gate in self.gates if gate.required and not self.proceeded(gate.id))

    def folds(self, gate: Gate) -> bool:
        """Whether a screen should render this gate as one line rather than as a card.

        Presentation, decided here rather than on the screen, because it is a question about
        *answers* and this is what holds them — and because the screen imports NiceGUI, which CI's
        required job does not install, so a rule living there is a rule no required test can reach.

        Folded is not hidden: a folded gate opens on a click and keeps its **why** and its
        controls, so an answer can still be re-read or changed. What folding removes is the scroll
        between the operator and the question actually in front of them; in ``both`` mode the
        screen renders eight gates, and every one of them used to stand open at once.

        **A required gate is never folded before it is answered.** That is the line this must not
        cross: the gates are the safety mechanism, and one folded away is one answered unread.
        """
        if gate.id in self.answers:
            return True
        return not (gate.required or self._has_work(gate))

    def _has_work(self, gate: Gate) -> bool:
        """Whether an unanswered optional gate is asking for something *now*.

        Deliberately not "is it the next unanswered gate". The languages gate is answered only by
        *changing* the languages, so an operator who takes the default never answers it at all —
        under that rule it stayed :meth:`next_gate` for the whole walk, which meant the row-diff
        gate could never be next, which meant it could never open. A fold rule whose failure mode
        is a collapsed row-by-row walk is worse than the scrolling it was removing, because an
        undecided row is not published.

        So each optional gate says when it matters, and the two that never do stay folded: the
        languages filter has a default that is right every time, and the post-run summary is a
        link to a screen that holds the answer permanently.
        """
        if gate.id == "row_diff":
            return self.answers.get("plan_review") == "changed-review"
        return False

    @property
    def next_gate(self) -> Gate | None:
        """The next gate to present, required or not, or ``None`` when all have been answered."""
        return next((gate for gate in self.gates if gate.id not in self.answers), None)

    @property
    def cancelled(self) -> bool:
        """Whether any gate was answered with an option that stops the run.

        Only the answers that actually refuse. This used to count every answer that did not
        *proceed*, which swept in the detours: a screen showing the rest of a diff, or asking to
        pick a different mode, reported the run as cancelled with no way back.
        """
        return any(self.refused(gate.id) for gate in self.gates)

    def confirms(self, row: PlanRow) -> bool:
        """Whether this session confirms one plan row.

        UNCHANGED is never executed and HELD is dropped by ``run_execute`` regardless, so neither
        is ever confirmed here — offering them would be offering a choice the script overrules.

        Every NEW row is confirmed under all three plan-review answers; what differs is CHANGED:

        * ``new-only`` — never.
        * ``changed-review`` — only when the operator applied it at gate 6. **Not merely when it
          was not skipped**: a row nobody has looked at is not a row anybody approved.
        * anything else, which is ``all`` — always.

        The middle case is the fix. It used to switch on classification alone, so *Review changed*
        confirmed precisely what *All* confirmed, and the answer that reads as the careful one was
        the same click as the sweeping one.
        """
        if row.classification is PlanClassification.NEW:
            return True
        if row.classification is not PlanClassification.CHANGED:
            return False
        answer = self.answers.get("plan_review")
        if answer == "new-only":
            return False
        if answer == "changed-review":
            return self.applied_rows.get((row.gtin, row.language), False)
        return True

    def confirmed_pairs(self, plan: Plan) -> list[list[str]]:
        """The ``confirmed_gtins_by_lang`` this session authorises, in plan order.

        The whole selection rule — classification, the per-row decisions, and the language subset
        from gate 2 — lives here rather than on the screen, so there is one answer to "what will
        this run write" and a test can ask it without a browser.

        Pairs must match :attr:`~lib.records.PlanRow.gtin` **byte for byte** — not zero-padded, not
        normalised. ``run_execute`` silently ignores a pair with no matching row, so a well-meant
        reformat here would drop rows from the run without saying anything.

        An empty :attr:`languages` means "every language in the plan" rather than "none". A run
        scoped to no language would confirm nothing, publish nothing and report success, and that
        is the one outcome indistinguishable from working.
        """
        languages = set(self.languages)
        return [
            [row.gtin, row.language]
            for row in plan.rows
            if (not languages or row.language in languages) and self.confirms(row)
        ]

    def execute_argv(
        self, confirmed_path: str, *, dry_run: bool, revive: bool = False
    ) -> list[str]:
        """The command this session authorises — or a refusal.

        Two ways to be refused, and the second one used to be missing. A gate answered *stop the
        run* is not "outstanding" — it has an answer — so a refusal at a gate that is not
        ``required`` reached this function and got a command built. Gate 4's *Stop the run* is
        exactly that, and its documented "abort before execute" was enforced only by the publish
        screen returning early: display logic standing in for the function whose entire docstring
        is about refusing. It is checked first because it is the more specific truth, and it names
        the gate so the screen can say which.

        The dry run (gate 8.5) is itself a required gate, so it is exempt from needing its *own*
        answer: it is the thing being authorised at that point, and requiring an answer before
        producing the command it previews would be a loop. The exemption covers both checks — a
        dry run cancelled after being read must still be *re-runnable*, since it writes nothing.
        Every other required gate must already have proceeded, including the production one — a
        dry run of a plan whose intent was never confirmed is a preview of a decision nobody made.

        Raises:
            GateNotAnsweredError: If any required gate is outstanding, or any gate at all was
                answered with an option that stops the run.
        """

        def relevant(gates: tuple[Gate, ...]) -> list[Gate]:
            return [gate for gate in gates if not (dry_run and gate.id == "dry_run")]

        refused = relevant(tuple(gate for gate in self.gates if self.refused(gate.id)))
        if refused:
            raise GateNotAnsweredError(f"answered with a refusal: {_named(refused)}")

        outstanding = relevant(self.outstanding)
        if outstanding:
            raise GateNotAnsweredError(f"not answered yet: {_named(outstanding)}")

        return run_execute_argv(
            self.client_id,
            mode=self.mode,
            confirmed_path=confirmed_path,
            dry_run=dry_run,
            production_acknowledged=self.production_acknowledged(),
            revive=revive,
        )
