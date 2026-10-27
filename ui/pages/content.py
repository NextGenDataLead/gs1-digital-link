"""Screen 3 — the generated copy: written here when there is a key, imported when there is not.

Copy has two producers and one results file. A Claude Code session running the
``content-generator`` skill writes it on the maintainer's machine and hands over
``generation_results.json``; the Anthropic Messages API writes the same file headlessly. This
screen offers whichever is available, then says how much of the current export the file covers and
shows the text per language.

**Which one is offered is decided by the key, not by a setting.** With the client's
``generator.api_key_env`` unset, this machine holds no credential and reaches Anthropic not at
all — the documented arrangement, and still the default. Setting it turns generation on here, so
the shell becomes one access point from dataset to pages rather than a surface with a hole in the
middle of it. Either way the key stays out of this process: generating runs
``scripts.run_generate`` as a subprocess, which loads ``.env`` in its own ``__main__`` block.

**Coverage is the load-bearing part.** Copy is written fresh for each run and never stored, so the
question is not how much has piled up but whether *this* file answers every unit the run will
publish — and whether it still describes this export. Not every in-scope unit: copy is written for
the rows a run creates or changes, so an already-live unchanged unit needs none, and neither does
a product the plan will hold for a missing video or missing mandatory data. Both are excluded from
the count rather than reported as a shortfall, and the check's detail line names each separately —
one of them is finished and the other is waiting on the client. Its fingerprint covers
``{inputs, language, prompt_version}``, so editing one product in the feed, or bumping the prompt
version, leaves that unit uncovered. An uncovered unit with no producer on this machine is an E21
omission: it leaves the plan without a row. Before ``Plan.skipped`` existed it left without a trace
at all, and an empty plan looked exactly like a plan with nothing to do.

So the count is shown before the copy is, and a shortfall is stated as a shortfall.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from nicegui import ui

from lib.config import ClientConfig, GeneratorConfig
from lib.gates import Mode
from ui import DATA_ROOT, content_session, context, env_edit, progress, runner, theme


def render() -> None:
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Content",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Content"),
            "Content",
            "The two things on a product page a machine writes, and who is without them.",
        )
        if cfg is None or cid is None:
            theme.blocked(
                "clients.yml did not load, so this screen has nothing to work from.",
                link_label="Open Setup →",
                route="/setup",
            )
            return
        # Filled in once Next exists, below the steps it waits on.
        unlock: list[Callable[[bool], None]] = []
        resume: Callable[[], None] = lambda: None  # noqa: E731 — nothing to resume without a generator
        links_only = context.batch_mode(cfg) is Mode.LINKS
        if links_only:
            # Passed over by Next on Data; reachable from the rail, so it says why it is empty.
            theme.band(
                "This batch publishes GS1 links only. No page is written, so there is no tagline "
                "or Eigenschappen text to write for it — go on to the preflight."
            )
        elif cfg.generator is None:
            ui.label(
                "This client has no `generator` block, so pages are published from feed copy only "
                "and there is nothing to import."
            ).classes("note")
        else:
            results_path = DATA_ROOT / "output" / cid / "data" / "generation_results.json"
            resume = _live_screen(
                cid, cfg, cfg.generator, results_path, list(cfg.wordpress.languages), unlock
            )

        unlock.append(_onward(cid, waits=cfg.generator is not None and not links_only))
        resume()


def _onward(cid: str, *, waits: bool) -> Callable[[bool], None]:
    """Next: the only way on to Preflight — see :mod:`ui.progress`. Returns its caption's switch.

    **Always pressable: writing text is optional** (operator, 2026-10-22). A page that is already
    live keeps the text it carries when nothing new is written for it
    (``lib.state.keeps_live_copy``), so a batch whose only change is, say, the title goes on
    without generating anything. What skipping costs is said in the caption instead of enforced by
    a disabled button: a product that is not live yet and has no text is left out of the run
    (E21), and Preflight names it.
    """

    def go() -> None:
        progress.of(cid).advance("/content")
        ui.navigate.to("/preflight")

    button, caption = theme.onward("Next", go)

    def switch(written: bool) -> None:
        caption.text = (
            "Next goes on to the preflight, once you have read the text above."
            if written
            else "Writing text is optional. Without it, pages that are already live keep the text "
            "they have; a product not yet published and without text is left out of the run."
        )

    switch(not waits)
    return switch


def _live_screen(  # noqa: PLR0913, PLR0915 — the three steps share one set of containers
    cid: str,
    cfg: ClientConfig,
    generator: GeneratorConfig,
    results_path: Path,
    languages: list[str],
    unlock: list[Callable[[bool], None]],
) -> Callable[[], None]:
    """The screen, driven by what the **site** carries rather than by the ledger. Returns the call
    that restores this session's earlier check — see :mod:`ui.content_session`.

    **Each step opens when the one before it succeeds** (operator feedback, 2026-10-07): step 2
    once the site has been read, step 3 — and Next, through ``unlock`` — once every product the
    site says needs text has it in every language, written now or by an earlier Generate.

    Everything here used to be derived from ``state.json``: which units a run would write, which
    of those had copy, how much was outstanding. That ledger records what *this machine* wrote and
    does not travel between machines, so on a second operator's copy it can be confidently wrong
    about a page that is live and correct. The one question this screen exists to answer — *does
    this product have a tagline and an Eigenschappen block?* — has an authoritative source, and it
    is the site.

    **Three numbered steps, in the order they are done** (operator feedback, 2026-10-07): check the
    live site, generate the content, review it. One button asks the site, and three numbers come
    back, each labelled with what happens to it: **generate** (no live text and the export can
    supply it), **skip** (no live text and it cannot — a MyGS1 worklist, not a button), and **skip**
    (live text already).

    The third is skipped *by default*, and that is the only part a person has to decide: whether
    the inputs moved since the live text was written. Nothing here can tell — the site reports
    that a tagline exists, never which export values produced it, and the fingerprint that would
    say is in the ledger this screen exists to stop depending on. So it is a tick box, and the
    ticks join the automatic set in **one** Generate button: two buttons made the run two runs, and
    an operator who pressed only the obvious one wrote half of what they meant to.

    The read is slow (a listing per language, then one request per page, because a language-scoped
    read answers ``acf: []`` on this site) and needs credentials this process does not hold. So it
    is a subprocess, and it runs only when pressed.
    """
    payload: Any = None
    selection: set[str] = set()
    scope = context.batch_scope(cid, cfg)
    batch = scope.gtins if scope is not None else frozenset()

    def show(fetched: tuple[Any, runner.CommandResult | None]) -> None:
        nonlocal payload
        payload, result = fetched
        selection.clear()
        status.clear()
        picker.clear()
        everyone.clear()
        action.clear()
        with status:
            _live_figures(payload, result)
        checked = context.live_counts(payload) is not None
        step2.set_visibility(checked)
        ready = context.live_gtins(payload, "needs_text")
        done = checked and context.text_written_for(ready, _written(results_path), languages)
        step3.set_visibility(done)
        for switch in unlock:
            switch(done)
        # The button is built before the list that feeds it, so the list's tick boxes have
        # something to update — the containers were created in reading order above, so building
        # them out of order does not move anything on screen.
        with action:
            if done and ready:
                theme.band(
                    "This batch's text is already written — read it in step 3. Generating again "
                    "replaces it.",
                    "quiet",
                )
            sync = _generate_panel(cid, generator, ready, selection, refresh)
        with picker:
            boxes = _override(payload, selection, sync)
        with everyone:
            _include_published(boxes)

    def draw_review() -> None:
        # Redrawn after every Generate, which ends in ``refresh``: drawn once at page load, the
        # copy just written stayed off screen until the operator left and came back.
        review.clear()
        with review:
            _review(context.batch_scope(cid, cfg), results_path, languages)

    async def refresh() -> None:
        fetched = await runner.run_json_off_the_loop(runner.report_live_copy_argv(cid))
        if context.live_counts(fetched[0]) is not None:
            content_session.CHECKS.keep(cid, batch, fetched[0])
        show(fetched)
        draw_review()

    with theme.section("Check the live site", step=1):
        ui.label(
            "Two things on every product page are written by a machine: the tagline at the top "
            "and the Eigenschappen bullet list. Everything else — brand, size, material, barcode "
            "— comes from the GS1 export and is never invented. This asks the live site which "
            "products are missing those two."
        ).classes("note")
        theme.action("Check the live site", refresh)
        ui.label("One request per page, so it takes a few seconds. Nothing is written.").classes(
            "note mt-2"
        )
        status = ui.column().classes("w-full mt-4")
        with status:
            theme.band("Not checked yet — press the button to ask the site.")

    step2 = ui.column().classes("w-full gap-0")
    step2.set_visibility(False)
    with step2, theme.section("Generate content", step=2):
        ui.label(
            "Writes the tagline and the Eigenschappen list for the products step 1 found without "
            "them, in every language."
        ).classes("note")
        everyone = ui.column().classes("w-full mt-2")
        # Folded: it is the exception, and a step that opens on a list of tick boxes reads as
        # though they are the step.
        with (
            ui.expansion("Override: also regenerate products that already have live text")
            .classes("w-full mt-2 fold-tight")
            .props("dense")
        ):
            ui.label(
                "Those are skipped by default. Tick a product here to include it anyway — for "
                "text that is live but whose GS1 data has moved since it was written. Nothing on "
                "this machine can detect that for you: the site can say a tagline exists, never "
                "which export values produced it. So it is your call, and nothing is ticked."
            ).classes("note")
            picker = ui.column().classes("w-full mt-3")
            with picker:
                ui.label("Check the live site first.").classes("note")
        action = ui.column().classes("w-full mt-3")
        with action:
            ui.label("Check the live site first.").classes("note")

    step3 = ui.column().classes("w-full gap-0")
    step3.set_visibility(False)
    with step3, theme.section("Review the text", step=3):
        ui.label(
            "The last place this is read as text rather than as a count. Check it against the "
            "real product: this pipeline fails silently, and a 'validated N' figure proves only "
            "that N things were shaped correctly."
        ).classes("note")
        review = ui.column().classes("w-full gap-0")
    draw_review()

    def resume() -> None:
        # Back on this screen in the same session, for the same batch: pick up where it was left,
        # steps 2 and 3 and Next included. The figures carry their own "checked … ago". Called by
        # ``render`` once Next exists, or the restored state could not switch it on.
        if (kept := content_session.CHECKS.find(cid, batch)) is not None:
            show((kept, None))

    return resume


def _written(results_path: Path) -> dict[str, dict[str, Any]]:
    """The results file, per product and language — empty when it is absent or unreadable."""
    import json  # noqa: PLC0415 — as in ``_review``

    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        return context.group_results(data.get("results", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _live_figures(payload: Any, result: Any) -> None:
    """The three counts, ordered and labelled by what happens to their products.

    "have text / need text / cannot be written" described three states and left the operator to
    work out which one the button acted on — and two of the three are skipped here for completely
    different reasons, one fixable on this machine and one only in MyGS1. The verb is the label,
    and the one that gets written comes first.
    """
    counts = context.live_counts(payload)
    if counts is None:
        theme.band(getattr(result, "stderr", "") or "The site could not be read.", "danger")
        return
    with theme.figures():
        theme.figure(
            str(counts["needs_text"]),
            "no live text · generate",
            "the export can supply it, so these are written",
        )
        theme.figure(
            str(counts["no_inputs"]),
            "no live text · skip",
            "no attr 1083 or 1067 in the export — fix in MyGS1",
        )
        theme.figure(
            str(counts["has_text"]),
            "live text already · skip",
            "already on the site — override by ticking one below",
        )
    ui.label(f"The site was checked {context.live_checked_at(payload)}.").classes("note mb-3")

    held = context.live_products(payload, "held")
    if held:
        # Said, not hidden: the saved selection keeps every not-eligible row so a run can name it,
        # and without this line the figures above would not add up to what the batch card lists.
        theme.band(
            f"{len(held)} product(s) on the saved list are not eligible, so nothing is written for "
            "them here — the Data screen says why. They stay on the list so the run can name them.",
        )
        ui.label(
            ", ".join(f"{p.get('gtin', '')} {p.get('name') or ''}".strip() for p in held)
        ).classes("mono scroll-x note")

    blocked = context.live_gtins(payload, "no_inputs")
    if blocked:
        theme.band(
            f"{len(blocked)} product(s) are skipped because nothing can be written for them: the "
            "export carries neither a marketing message (attr 1083) nor a feature/benefit (attr "
            "1067). Those are fixed in MyGS1 and re-exported, never here.",
            "warn",
        )
        ui.label(", ".join(blocked)).classes("mono scroll-x note")


def _override(payload: Any, selection: set[str], sync: Callable[[], None]) -> list[Any]:
    """Every product the site already has text for, each with a tick box, none ticked.

    Unticked by default, and never remembered across a check. Including one rewrites text that is
    live and, as far as anything here can tell, correct — so the default has to be "do nothing".
    A screen that arrives with rows ticked is a screen that rewrites a batch because somebody
    pressed the obvious button.
    """
    products = context.live_products(payload, "has_text")
    if not products:
        ui.label("Nothing has live text yet, so there is nothing to override.").classes("note")
        return []

    def toggle(gtin: str, on: bool) -> None:
        if on:
            selection.add(gtin)
        else:
            selection.discard(gtin)
        sync()

    boxes = []
    for product in products:
        with ui.row().classes("items-center gap-3 w-full"):
            boxes.append(
                ui.checkbox(
                    value=False,
                    on_change=lambda event, gtin=str(product["gtin"]): toggle(
                        gtin, bool(event.value)
                    ),
                )
            )
            ui.label(str(product["gtin"])).classes("mono")
            ui.label(str(product.get("name") or "")).classes("note")
    return boxes


def _include_published(boxes: list[Any]) -> None:
    """One box for every product already live with text — unticked, like each of the boxes it sets.

    The per-product boxes stay below, folded; this is the same choice made for all of them at
    once (operator, 2026-10-22). Unticked, a live page that changes keeps its text — a new title
    goes out without rewriting a word of copy. Ticking it replaces the live text of every one.
    """
    if not boxes:
        return

    def set_all(on: bool) -> None:
        for box in boxes:
            box.set_value(on)  # each box's own handler updates the selection and the caption

    ui.checkbox(
        f"Include already-published pages ({len(boxes)}) — replaces the text they have live",
        value=False,
        on_change=lambda event: set_all(bool(event.value)),
    )


def _generate_panel(
    cid: str,
    generator: GeneratorConfig,
    ready: list[str],
    selection: set[str],
    refresh: Callable[[], Awaitable[None]],
) -> Callable[[], None]:
    """One button for both halves of the run, and the sentence saying what it will do.

    There were two — write the missing, and regenerate the ticked — and that made one intention
    into two runs. The failure is not hypothetical in this codebase's history: given two buttons
    where one is obviously primary, the second gets pressed some of the time, and a run that
    writes half of what the operator meant reports success either way.

    So the set is the **union**, recomputed at click time rather than captured when the button was
    built, and the caption keeps the two halves visible so a total of 13 is never mistaken for 13
    missing pages. Returns the updater the tick boxes call.
    """
    caption = ui.label("").classes("note mb-2")
    secret = env_edit.describe([generator.api_key_env])[generator.api_key_env]
    if not secret.present:
        caption.text = ""
        theme.band(
            f"{generator.api_key_env} is not set, so this machine cannot write text at all. Set "
            "it on the Setup screen.",
            "warn",
        )
        return lambda: None

    def chosen() -> list[str]:
        """The union, read when the button is pressed — the ticks keep moving until then."""
        return sorted({*ready, *selection})

    async def go() -> None:
        picked = chosen()
        if not picked:
            theme.announce(
                "Nothing to write", "Nothing needs text, and nothing is ticked.", kind="warn"
            )
            return
        output: list[str] = []
        result = await runner.stream(runner.run_generate_argv(cid, picked), output.append)
        await refresh()
        if result.ok:
            theme.announce(
                "Text written",
                f"{len(picked)} product(s) were written. Read them below before publishing.",
            )
        else:
            theme.announce(
                "Generation failed",
                f"The command exited {result.returncode} and nothing was written. The output "
                "below is what it said.",
                kind="danger",
                detail="\n".join(output[-_FAILURE_LINES:]) or "(no output)",
            )

    button = theme.action("Generate content", go)

    def sync() -> None:
        total = len(chosen())
        picked = len(selection)
        caption.text = f"{total} product(s) will be written: {len(ready)} with no live text" + (
            f", plus {picked} ticked under Override." if picked else "."
        )
        button.set_text(f"Generate content for {total} product(s)")
        button.set_enabled(bool(total))

    sync()
    return sync


def _review(scope: context.Scope | None, results_path: Path, languages: list[str]) -> None:
    """The copy for *this run*, with anything written for another scope called out.

    Scope is not recomputed here. It arrives from the doctor's ``scope`` check, whose GTINs are
    ``ProductRecord.gtin`` — the same field the results are keyed by — so the filter is a set
    membership test and not a second opinion about what a run covers.

    A GTIN outside that set used to be ordinary: the cache accumulated every unit ever generated
    on this machine. The file is per-run now, so copy for a GTIN this run will not touch means it
    was written against a different scope — worth saying, not worth hiding.
    """
    import json  # noqa: PLC0415 — only this section needs it

    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        entries = context.group_results(data.get("results", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        ui.label("No readable copy to review yet.").classes("note")
        return

    if not entries:
        ui.label("No text written for this batch yet.").classes("note")
        return

    if scope is None:
        ui.label("The saved batch could not be read, so there is nothing to review.").classes(
            "note"
        )
        return
    # This batch's products and nothing else. Text left in the file from an earlier batch is not
    # this batch's to review, and nothing publishes it.
    split = context.split_results(entries, scope)
    if not split.in_scope:
        ui.label("No text written for this batch yet.").classes("note")
        return
    _entries(split.in_scope, languages, results_path)

    if split.missing:
        ui.label(
            f"{len(split.missing)} product(s) in this batch have no text yet: "
            + ", ".join(split.missing[:_MAX_NAMED])
            + (
                f" …and {len(split.missing) - _MAX_NAMED} more"
                if len(split.missing) > _MAX_NAMED
                else ""
            )
        ).classes("note mono scroll-x mt-3")


def _entries(entries: dict[str, Any], languages: list[str], results_path: Path) -> None:
    """Render the copy for each GTIN, one card per product, capped and counted.

    The tagline is ``usps[0]`` and the Eigenschappen bullets are the rest, which is the same
    reading ``_assemble_description`` uses. There is no product name here: the copy contract
    never carried one, and the field this used to render was silently absent on every entry — a
    column of em-dashes that looked like missing data rather than like a bug.
    """
    for gtin, per_language in list(entries.items())[:_MAX_SHOWN]:
        with ui.element("div").classes("card mb-3"):
            ui.label(gtin).classes("mono gate-step")
            with ui.row().classes("gap-8 items-start w-full flex-wrap"):
                for language in languages:
                    entry = per_language.get(language)
                    with ui.column().classes("gap-1 min-w-64 flex-1"):
                        ui.label(language.upper()).classes("figure-label")
                        if entry is None:
                            ui.label("no copy").classes("tag tag-fail")
                            continue
                        usps = entry.get("usps", [])
                        if usps:
                            ui.label(usps[0]).classes("font-medium")
                        for usp in usps[1:]:
                            ui.label(f"• {usp}").classes("note")
    if len(entries) > _MAX_SHOWN:
        ui.label(f"Showing the first {_MAX_SHOWN}. The rest are in {results_path.name}.").classes(
            "note"
        )


#: How much of a failed command's output to put in the dialog. Enough for a traceback's tail,
#: short enough that the Close button stays on screen without scrolling to it.
_FAILURE_LINES = 40

_MAX_SHOWN = 25
#: How many GTINs to name in a one-line list before summarising the remainder.
_MAX_NAMED = 20
