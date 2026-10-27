"""The operator gates, as data — one source for the skill's prose and any UI that renders them.

The gates are the safety mechanism of this project. ``CLAUDE.md`` says it outright: they live in
``.claude/skills/flow-orchestrator/SKILL.md``, and calling ``scripts/run_execute.py`` directly
bypasses every one of them. Only two guards are in code — the production refusal and the
``--only links`` target-serves check — and both are there *because prose can be skipped*.

That is fine while a language model reading the prose is the only thing driving the pipeline. The
moment a second consumer exists — a form-rendering UI, a test, a checklist — there are two
implementations of one safety contract, and two implementations drift. Silently, because a gate
that quietly stops being shown raises nothing.

So the contract lives here, and both consumers read it. ``SKILL.md`` keeps the verbatim prompt text
a model needs; this module keeps the *structure*: which gates exist, at which step, which are
non-negotiable, and which apply in which mode. ``SKILL.md`` also carries a **Gate index** table
listing every id, and ``tests/lib/test_gates.py`` asserts the two agree in **both directions** — so
adding a gate here without documenting it there, or the reverse, fails CI rather than diverging
quietly.

This module also owns :func:`run_execute_argv`. The command is part of the contract: ``--only``
comes from the intent gate (in the shell, the mode the batch was saved with on the Data screen,
which gate 0 shows and confirms) and ``--i-understand-production`` from the production gate, and
getting either wrong turns a reviewed decision into an unreviewed write. A UI that builds its own
argv is a second chance to get it wrong.

Nothing here executes anything, prompts anything, or reads a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NamedTuple


class Mode(StrEnum):
    """Which leg of the publish a run performs.

    Mirrors ``scripts.run_execute._Mode``, and deliberately not imported from it: ``lib`` does not
    depend on ``scripts``, and this is the *operator-facing* vocabulary — the thing gate 0 asks
    about — which happens to share its spelling with the flag.
    """

    PAGES = "pages"
    LINKS = "links"
    BOTH = "both"

    @property
    def is_permanent(self) -> bool:
        """Whether this mode writes a GS1 record, which can never be deleted."""
        return self is not Mode.PAGES

    @property
    def slash_command(self) -> str:
        """The skill that pins this mode."""
        return {Mode.PAGES: "/gs1-pages", Mode.LINKS: "/gs1-links", Mode.BOTH: "/gs1-publish"}[self]

    @property
    def summary(self) -> str:
        """One line naming what this mode writes, in the operator's words."""
        return {
            Mode.PAGES: "WordPress pages only — no GS1 record, no QR",
            Mode.LINKS: (
                "Digital Links and QR only, pointing at pages that already exist — "
                "no page is touched"
            ),
            Mode.BOTH: "WordPress pages first, then Digital Links pointing at them",
        }[self]


#: Shown at gate 0 for any mode that writes a GS1 record. Verbatim from ``SKILL.md``: this is the
#: single fact that makes the flow's caution proportionate, and it must not be paraphrased shorter.
PERMANENCE_WARNING: Final = (
    "A GS1 Digital Link record can never be deleted. Retraction only disables it; the "
    "record stays on the account permanently."
)

#: Its counterpart for ``pages`` — said out loud rather than left implied, because the difference
#: between the two modes is the entire reason there are three of them.
REVERSIBLE_NOTE: Final = "Pages only — no GS1 record is written, so this run is reversible."


class GateOutcome(StrEnum):
    """What answering with one option does to the run.

    Three states rather than two, because the boolean this replaces was carrying two questions at
    once — *does this advance the flow* and *does this stop the run* — which coincide everywhere
    except on a detour. :attr:`REDISPLAYS` is the detour.
    """

    #: Carries the flow on to the next gate.
    ADVANCES = "advances"
    #: Refuses: this run must not happen. The one outcome that must never be inferred wrong.
    STOPS = "stops"
    #: Re-presents this same gate, showing more or asking again. The run stays exactly as available
    #: as it was — an answer of this kind changes what the operator is *looking at*, nothing else.
    REDISPLAYS = "redisplays"


class GateOption(NamedTuple):
    """One choice a gate offers.

    ``value`` is the token ``SKILL.md`` prints between the brackets, kept identical so an operator
    who has used one surface recognises the other.

    ``outcome`` says what this answer does. It is data rather than something a consumer infers
    from the word, because the inference is not obvious in any direction: ``changed-review``
    advances and ``switch-to-test`` does not, and both read like the opposite at a glance. A UI
    that guesses wrong here either blocks a legitimate run or — far worse — treats ``cancel`` as
    consent.

    It was a ``proceeds`` boolean until that boolean was found to be answering two questions with
    one bit. ``show-full-diff`` is where they part: in the chat flow it prints the rest of the diff
    and re-prompts, so *does not advance* is true of it, and the flag was set accordingly. On a
    form surface it is the **terminal** answer to its gate, so the same flag read as a refusal —
    and since it is the only option the shell can render at that gate, the single button on the
    card cancelled the run irrecoverably, on the path taken by the operator doing the most careful
    thing on offer. Two states made a screen guess; three say which question is being asked.

    ``chat_only`` marks an option only the conversational surface can honour, and it is data for
    the same reason ``outcome`` is: a UI cannot infer it. It is a **separate axis** — *which
    surface can honour this*, not a fourth value of *what this does*. Two gates carry such options
    — one needs a model to read the run log and explain, the others need a row-by-row walk the
    shell does not do. Deleting them instead would have been the tail wagging the dog: the shell
    cannot do a thing, so the surface that can loses it, and ``SKILL.md`` has to be edited to
    match. This keeps both surfaces honest about what they offer, and lets the contract test derive
    what a screen must render rather than carrying a hand-maintained list of exceptions.
    """

    value: str
    label: str
    consequence: str
    outcome: GateOutcome = GateOutcome.ADVANCES
    chat_only: bool = False

    @property
    def proceeds(self) -> bool:
        """Whether this answer carries the flow on to the next gate."""
        return self.outcome is GateOutcome.ADVANCES

    @property
    def refuses(self) -> bool:
        """Whether this answer stops the run.

        Not the negation of :attr:`proceeds`, and that is the whole point: an option that
        re-presents its gate does neither.
        """
        return self.outcome is GateOutcome.STOPS

    @property
    def in_shell(self) -> bool:
        """Whether a form-rendering UI can offer this option."""
        return not self.chat_only


@dataclass(frozen=True)
class Gate:
    """One operator touchpoint in the publish flow.

    ``step`` is a string because the sequence contains ``"8.5"``. The numbering is load-bearing:
    step 0 was added after the rest and the others kept their numbers, so every cross-reference to
    "step 8" — in ``SKILL.md``, in ``IMPLEMENTATION_SPEC.md`` §8.3, in ``docs/setup.md`` — still
    points at the same gate. Renumbering here would break all of them.

    ``required`` marks a gate that must not be skipped, defaulted through, or remembered from a
    previous run. ``purpose`` is the *why*, in one sentence: a UI that shows only the question
    trains an operator to answer it without reading, and the why is what stops that.
    """

    id: str
    step: str
    title: str
    purpose: str
    #: One sentence saying what this gate is for, in the words an operator uses. ``purpose`` is
    #: the full *why* — several sentences, each carrying an incident — and it earns its length on
    #: a surface that is read once, carefully. On a form it is a slab above the controls, and a
    #: slab above the controls is scrolled past: the screen showed eight of them at once, and the
    #: question the operator had to answer was underneath. So the screen leads with this and keeps
    #: ``purpose`` one press away. Never a substitute for it — a gate that shows only the short
    #: line is a gate answered without the reason being available at all.
    summary: str
    options: tuple[GateOption, ...]
    required: bool
    modes: frozenset[Mode]
    #: Only applicable when the client has a ``generator`` block configured.
    needs_generator: bool = False
    #: Only applicable when the resolved GS1 environment is ``production``.
    needs_production: bool = False
    #: Only applicable when this run's plan actually dropped a unit for a missing
    #: ``product_name`` (E18). The first applicability input that is a fact about the *plan*
    #: rather than about configuration, and the difference is operational: the other two are
    #: settled before the walk begins, this one is not. The plan is built at step 5 — in the
    #: middle of the walk — so a consumer that reads this once when the run starts reads it
    #: from before there was a plan, and the gate it decides then never appears at all.
    needs_missing_product_name: bool = False
    # When a fourth applicability input arrives, fold these into a frozen `RunFacts` with no
    # defaults rather than adding a fifth keyword: one bundle to keep exhaustive beats four
    # parameters to keep in sync. Three still read at a glance; five would not.

    @property
    def shell_options(self) -> tuple[GateOption, ...]:
        """The options a form-rendering UI can offer — everything not ``chat_only``.

        A screen renders from this rather than from :attr:`options`, so an option that only the
        conversational surface can honour is absent by construction instead of by omission. The
        difference matters: an omission is invisible, and one gate's options quietly stopped being
        rendered at all before anything checked.
        """
        return tuple(option for option in self.options if option.in_shell)

    def applies(
        self,
        *,
        mode: Mode,
        has_generator: bool,
        is_production: bool,
        has_missing_product_name: bool,
    ) -> bool:
        """Whether this gate fires for a given run.

        Every input is keyword-only and **none has a default**, deliberately. A defaulted
        applicability input is precisely the failure this module exists to prevent: a caller
        that forgets one gets a walk quietly missing a gate, and a gate that stops being shown
        raises nothing at all. A handful of call sites is a cheap price for a ``TypeError``
        instead of a silence.

        ``has_missing_product_name`` is a plain ``bool`` rather than the plan itself because
        this module reads no file and imports nothing from ``lib``: the caller does the
        derivation, and only the conclusion crosses the boundary.
        """
        if mode not in self.modes:
            return False
        if self.needs_generator and not has_generator:
            return False
        if self.needs_production and not is_production:
            return False
        return not (self.needs_missing_product_name and not has_missing_product_name)


_ALL_MODES: Final = frozenset(Mode)
_PERMANENT_MODES: Final = frozenset({Mode.LINKS, Mode.BOTH})


GATES: Final[tuple[Gate, ...]] = (
    Gate(
        id="intent",
        step="0",
        title="Intent confirmation",
        summary="What this run will do, to which file, and in which environment.",
        purpose=(
            "States the mode, cross-checks the configured export file against the one the "
            "operator has in mind, gives **how many products this run could touch** and the "
            "environment, and — for anything that writes to GS1 — warns that the records are "
            "permanent. The export cross-check catches the likeliest real error: a fresh export "
            "dropped somewhere new while `export.path` still points at the old one, which "
            "nothing downstream notices. The scope figure leads and the catalogue total follows "
            "it: this gate used to give only the catalogue size, which read 127 on a run scoped "
            "to one product — and gate 0 is where the operator forms their picture of what they "
            "are about to do. Neither number is the row count; that arrives at step 5."
        ),
        options=(
            GateOption("confirm", "Confirm", "Proceed with this mode"),
            GateOption(
                "change-mode",
                "Change mode",
                "Re-present with a different mode",
                GateOutcome.REDISPLAYS,
            ),
            GateOption("cancel", "Cancel", "Abort; nothing runs", GateOutcome.STOPS),
        ),
        required=True,
        modes=_ALL_MODES,
    ),
    Gate(
        id="languages",
        step="2",
        title="Language selection",
        summary="Which languages this run covers. All of them unless you narrow it.",
        purpose=(
            "Which of the client's configured languages this run covers. Intersected with the "
            "confirmed rows at step 6."
        ),
        options=(),  # built from wordpress.languages at render time
        required=False,
        modes=_ALL_MODES,
    ),
    Gate(
        id="content_review",
        step="3",
        title="Generated copy review (gate 1 of 2)",
        summary=(
            "Read the machine-written tagline and Eigenschappen text before it reaches a live page."
        ),
        purpose=(
            "The tagline and Eigenschappen are LLM-written, so they are read before they can "
            "reach a page. Review the copy against the real product, not the 'validated N' count "
            "— this pipeline fails silently. Show the coverage counts here too: a NEW or CHANGED "
            "unit with no copy for this version of the export is dropped from the plan entirely "
            "(E21), so a missing or stale results file yields an empty plan and a run that "
            "reports success having published nothing. The batch is this run's rows, not every "
            "unit in scope — an already-live, unchanged unit is not generated for."
        ),
        options=(
            GateOption("confirm", "Copy is good", "Proceed to planning"),
            GateOption(
                "regenerate",
                "Regenerate",
                "Write the copy again before planning",
                GateOutcome.REDISPLAYS,
            ),
            GateOption("cancel", "Cancel", "Abort; nothing runs", GateOutcome.STOPS),
        ),
        required=True,
        # Not links: that mode plans with `run_plan --links-only`, which does not apply E21, so
        # there is no copy to review and none whose absence could empty the plan.
        modes=frozenset({Mode.PAGES, Mode.BOTH}),
        needs_generator=True,
    ),
    Gate(
        id="missing_field",
        step="4",
        title="Missing-field prompt",
        summary=(
            "Products with no name in one language. They cannot be published — decide what happens."
        ),
        purpose=(
            "The units `run_plan` dropped because the product carries no `product_name` in that "
            "language (E18), named one by one. **This gate appears only when a unit was actually "
            "dropped.** It used to appear on every run, asking whether to skip a unit it could "
            "not name, on runs where nothing had been skipped — and of its answers only *stop "
            "the run* did anything, so the one live control on a question about nothing was the "
            "destructive one. A gate that asks about nothing teaches answering without reading, "
            "which is the habit this flow cannot afford. Nothing here can supply the missing "
            "name: it is filled in MyGS1 and re-exported."
        ),
        options=(
            GateOption("skip-row", "Skip this unit", "Other languages proceed"),
            GateOption("ask-me-later", "Ask me later", "Batch the prompts, present at the end"),
            GateOption("fail-run", "Stop the run", "Abort before execute", GateOutcome.STOPS),
        ),
        required=False,
        modes=_ALL_MODES,
        needs_missing_product_name=True,
    ),
    Gate(
        id="plan_review",
        step="5",
        title="Plan review (gate 2 of 2)",
        summary=(
            "The run sheet: which pages get created, rewritten or left alone. Choose how much "
            "of it runs."
        ),
        purpose=(
            "The last look before anything is written. Show the counts, the gate exclusions, and "
            "the units dropped before classification — an operator reading 'New: 0' alone "
            "concludes there is nothing to do, when in fact there is copy to generate. When "
            "prior state was reset from a corrupt file (E19), that warning goes **above** the "
            "counts: the counts alone read as a routine first run, and confirming would rewrite "
            "every live page."
        ),
        options=(
            GateOption("all", "All", "Confirm every NEW and CHANGED row"),
            GateOption("new-only", "New only", "Confirm NEW rows; skip CHANGED"),
            GateOption("changed-review", "Review changed", "Walk each CHANGED row's diff"),
            GateOption("cancel", "Cancel", "Abort; nothing is written", GateOutcome.STOPS),
        ),
        required=True,
        modes=_ALL_MODES,
    ),
    Gate(
        id="row_diff",
        step="6",
        title="Per-row diff",
        summary="Decide each live page this run would rewrite, one at a time.",
        purpose=(
            "Only on `changed-review`. **Every CHANGED row is walked, not only the ones carrying "
            "a diff.** State records the prior `title` and `wp_url` and nothing else, so a row "
            "whose change is in the product body has no diff at all — on the pilot plan that was "
            "19 of 20 CHANGED rows, and keying the walk on the diff showed one row while "
            "confirming twenty. Show the fields the diff actually has and never invent an old "
            "value; an empty diff reads `Changes: product content (no title or URL change)`, and "
            "a `gs1_link` key reads `Changes: resolver link not written yet` — the page is "
            "published, its resolver link was never written, and nothing about the page is "
            "changing. `apply`/`skip` are per-row on **both** surfaces: the chat flow prompts row "
            "by row, the shell puts the pair on every row and holds the answers for the run. A "
            "row left undecided is not confirmed."
        ),
        options=(
            GateOption("apply", "Apply", "Include this row in the run"),
            GateOption("skip", "Skip", "Leave this row unchanged"),
            GateOption(
                "show-full-diff",
                "Show full diff",
                "Show every changed row",
                GateOutcome.REDISPLAYS,
            ),
        ),
        required=False,
        modes=_ALL_MODES,
    ),
    Gate(
        id="production",
        step="8",
        title="Production environment confirmation",
        summary="This run writes to the real GS1 register, and those records are permanent.",
        purpose=(
            "Mandatory, non-overridable, and enforced per run rather than per session. Skipped in "
            "`pages` mode only because gate 0 has already named the environment and nothing "
            "irreversible follows — a second production prompt for a page you can delete trains "
            "the operator to click through them."
        ),
        options=(
            GateOption("confirm", "Confirm", "Execute against production"),
            GateOption(
                "switch-to-test",
                "Switch to test",
                "Re-resolve to the test environment",
                # STOPS, not REDISPLAYS, though it reads like a detour: it refuses *this* run,
                # whose whole subject is the environment it was resolved against.
                GateOutcome.STOPS,
            ),
            GateOption("cancel", "Cancel", "Abort; nothing is written", GateOutcome.STOPS),
        ),
        required=True,
        modes=_PERMANENT_MODES,
        needs_production=True,
    ),
    Gate(
        id="dry_run",
        step="8.5",
        title="Dry run",
        summary="The same run with nothing written. Read what it says it would do, then answer.",
        purpose=(
            "The same command with `--dry-run` and every other flag identical. Catches a plan "
            "pointing at the wrong rows, the wrong leg or the wrong URLs while it still costs "
            "nothing. Two things it cannot catch, so do not read a clean dry run as more than it "
            "is: in `links` mode it does not verify that targets serve, and it never proves the "
            "ACF fields will land."
        ),
        options=(
            GateOption("proceed", "Proceed", "Run it for real"),
            GateOption("cancel", "Cancel", "Abort; nothing is written", GateOutcome.STOPS),
        ),
        required=True,
        modes=_ALL_MODES,
    ),
    Gate(
        id="post_run",
        step="11",
        title="Post-run summary",
        summary="What actually happened, row by row.",
        purpose=(
            "What actually ran, per row, with each error named. In `links` mode a refused GTIN "
            "means its target URL did not serve — read that as 'the page is not where the plan "
            "says it is', not as a GS1 fault. `detail` needs a model to read the log and explain "
            "it, so it exists in the chat flow only; the shell links to the Runs screen, where "
            "the same rows are rendered and the site can be reconciled against the ledger."
        ),
        options=(
            GateOption("yes", "Retry the failures", "Re-run execute filtered to the failed GTINs"),
            GateOption("no", "Done", "Finish"),
            GateOption(
                "detail",
                "Explain each error",
                "Read the run log and explain",
                GateOutcome.REDISPLAYS,
                chat_only=True,
            ),
        ),
        required=False,
        modes=_ALL_MODES,
    ),
)

#: Gate ids by id, for lookup.
BY_ID: Final[dict[str, Gate]] = {gate.id: gate for gate in GATES}


def gates_for(
    *, mode: Mode, has_generator: bool, is_production: bool, has_missing_product_name: bool
) -> tuple[Gate, ...]:
    """The gates that fire for one run, in step order.

    ``has_missing_product_name`` is the caller's answer to "did this run's plan drop a unit for
    a missing ``product_name``?". Required rather than defaulted for the reason
    :meth:`Gate.applies` gives — and unlike the other two it is not settled when the run begins,
    so a caller must re-ask it whenever the plan changes rather than resolving it once.
    """
    return tuple(
        gate
        for gate in GATES
        if gate.applies(
            mode=mode,
            has_generator=has_generator,
            is_production=is_production,
            has_missing_product_name=has_missing_product_name,
        )
    )


def run_execute_argv(  # noqa: PLR0913 — one parameter per gate answer; bundling them hides them
    client_id: str,
    *,
    mode: Mode,
    confirmed_path: str,
    dry_run: bool,
    production_acknowledged: bool = False,
    revive: bool = False,
) -> list[str]:
    """Build the ``run_execute`` command line the gates authorise, and only that.

    The command *is* part of the gate contract, so it is built once rather than twice.

    * ``--only`` is appended for ``pages`` and ``links`` and omitted for ``both``, because
      omitting it is what ``both`` means — every invocation written before ``--only`` existed
      keeps its behaviour.
    * ``--i-understand-production`` is appended **only** when the production gate has been
      answered, and never on a dry run, which needs no acknowledgement because it writes nothing.
      Passing it earlier would hand a real run the authorisation before anyone granted it; the
      flag exists precisely so that the live write is a deliberate act.

    ``production_acknowledged`` is a positive statement — "someone confirmed" — rather than an
    ``is_production`` fact, so the flag cannot be derived from configuration alone. Deriving it
    would make it a description of the environment instead of a record of a decision.

    Args:
        client_id: The client to run.
        mode: The leg confirmed at gate 0.
        confirmed_path: Path to the ``ConfirmedPlan`` JSON.
        dry_run: Whether this is the mandatory preview (gate 8.5) rather than the real run.
        production_acknowledged: Whether the production gate (step 8) was answered ``confirm``.
        revive: Whether to re-publish GTINs that ``run_unpublish`` took down.

    Returns:
        The full argv, starting at the module name — ``["python", "-m", …]`` is the caller's job,
        since it owns the interpreter.
    """
    argv = ["-m", "scripts.run_execute", client_id, "--confirmed", confirmed_path]
    if mode is not Mode.BOTH:
        argv += ["--only", mode.value]
    if dry_run:
        argv.append("--dry-run")
    elif production_acknowledged:
        argv.append("--i-understand-production")
    if revive:
        argv.append("--revive")
    return argv
