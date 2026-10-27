"""Screen 1 — what this machine is configured to do, to whom, and with which credentials.

The two most expensive mistakes in this pipeline are both *config* mistakes that nothing
downstream notices: pointing at the wrong export, and pointing at production. Until this screen
existed, both were made in a text editor, in a file whose rules are not visible from inside it,
and discovered at the first API call — or, in the worst case, after a permanent record was written.

Four things about the form are deliberate.

**It writes only what changed.** The values shown are the *resolved* config, ``defaults`` merged
in. Writing them all back would freeze every inherited default into this client's block. See
:mod:`ui.form`.

**The client id is not editable.** It is the path to ``output/{client_id}/state.json``, which
records every GTIN already published. Renaming it does not move that file — it orphans it, and
every published GTIN would classify as new on the next run against a live site.

**Switching to production asks for the client id, typed in full.** That flag decides whether a run
writes permanent, undeletable records. It is the same decision the production gate asks about, and
it was previously one word in a text file changed with no confirmation at all.

**Credentials are write-only.** The fields set values in ``.env`` and never show one back; whether
a name resolves is answered by the Test buttons, which ask WordPress and GS1 rather than reading
the file. See :mod:`ui.env_edit`. The Anthropic key is the one optional field: left empty, this
machine never reaches Anthropic and copy is imported on Content; set, Content can generate it.

``gdsn_map``, ``acf_map``, ``brick_category_map`` and ``generator`` stay read-only: the first three
were each settled by a field walk against the live site, and ``generator`` is the E21 switch rather
than a preference.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

from nicegui import ui

from lib.config import DEFAULT_CLIENTS_PATH, ClientConfig
from lib.errors import ConfigError
from ui import DATA_ROOT, config_edit, context, env_edit, handover_panel, runner, theme
from ui.form import FieldSet, Parsed, split_list

#: WordPress multilingual plugins the pipeline has an adapter for. ``none`` is not "unknown": it
#: selects the no-op adapter, which links no translations and raises nothing when it does not.
_PLUGINS: Final = ("none", "polylang", "wpml")

_ENVIRONMENTS: Final = ("test", "production")


def render() -> None:
    cid = context.client_id()
    cfg = context.client_config(cid)

    with theme.page(
        "Setup",
        client_id=cid,
        environment=cfg.gs1.environment if cfg else None,
        facts=context.rail_facts(cid, cfg),
        locked=context.locked_steps(cid, cfg),
    ):
        theme.heading(
            theme.eyebrow("Setup"),
            "Setup",
            "What this machine is configured to publish, where, and with which credentials.",
        )

        if cfg is None or cid is None:
            _no_config()
            # Receiving a pack is how a broken or placeholder config gets replaced, so it is
            # offered even when nothing else on this screen can be.
            handover_panel.render_receive_only()
            return

        if context.is_production(cfg):
            theme.band(
                "This client points at the GS1 PRODUCTION resolver. Records written there can "
                "never be deleted — retraction only clears their links and disables them.",
                "danger",
            )

        # Eight sections and about four screens of scroll, on the one screen a new operator
        # opens first. The jumps are the cheapest way to make its shape visible without
        # rearranging what is on it.
        theme.jumps(
            [
                ("Client", "client"),
                ("WordPress", "wordpress"),
                ("GS1", "gs1"),
                ("Files", "files"),
                ("Credentials", "credentials"),
                ("Test", "tests"),
                ("Handover", "handover"),
            ]
        )

        editor = _Editor(cid)
        _client(editor, cfg)
        _wordpress(editor, cfg)
        _gs1(editor, cfg)
        _files(editor, cfg)
        _save(editor, cfg)

        _credentials(cfg)
        _tests(cid)
        handover_panel.render(cid)
        _read_only(cfg)


def _no_config() -> None:
    theme.band("clients.yml did not load, so nothing else on this screen can be trusted.", "danger")
    ui.label(
        "The form is not shown over a file that will not parse — it would offer to edit fields "
        "it could not read. Run the preflight for the full list of what is wrong: it reports "
        "every offending field at once rather than stopping at the first."
    ).classes("note")
    theme.command(["-m", "scripts.doctor", "--offline"])
    ui.link("Go to Preflight →", "/preflight").classes("mono")


# --- The editable fields ------------------------------------------------------


class _Editor:
    """The screen's editable fields, and whatever needs to react when one of them changes.

    A thin layer over :class:`ui.form.FieldSet`: that class owns the change bookkeeping and knows
    nothing about widgets, this one owns the widgets and knows nothing about YAML. The Save
    section registers a watcher so the unsaved-changes count and the production confirmation
    appear as the operator types, rather than only once they have pressed the button.
    """

    def __init__(self, client_id: str) -> None:
        self.fields = FieldSet(prefix=("clients", client_id))
        self._watchers: list[Callable[[], None]] = []

    def watch(self, callback: Callable[[], None]) -> None:
        self._watchers.append(callback)

    def text(self, path: Sequence[str], label: str, value: str, hint: str = "") -> None:
        self._row(label, hint, lambda: self._wire(path, ui.input(value=value), value))

    def choice(
        self, path: Sequence[str], label: str, value: str, options: Sequence[str], hint: str = ""
    ) -> None:
        self._row(
            label, hint, lambda: self._wire(path, ui.select(list(options), value=value), value)
        )

    def items(self, path: Sequence[str], label: str, value: Sequence[str], hint: str = "") -> None:
        """A YAML sequence typed as ``nl, fr`` — quicker to read and correct than a chip widget."""
        self._row(
            label,
            hint,
            # Built inside the callback, not before it: NiceGUI parents an element to whichever
            # container is open when it is *constructed*, so one made ahead of `_row` lands
            # beside the row instead of in it.
            lambda: self._wire(path, ui.input(value=", ".join(value)), list(value), split_list),
        )

    def _wire(
        self,
        path: Sequence[str],
        element: Any,
        initial: Parsed,
        parse: Any = None,
    ) -> Any:
        element.props("outlined dense").classes("w-full")
        element.on_value_change(lambda _: self._touched())
        if parse is None:
            return self.fields.add(path, element, initial)
        return self.fields.add(path, element, initial, parse)

    def _touched(self) -> None:
        for callback in self._watchers:
            callback()

    @staticmethod
    def _row(label: str, hint: str, build: Callable[[], Any]) -> None:
        """A form row: label, control, and the consequence of getting it wrong.

        The reason sits in the row rather than behind a tooltip. Every field here has a wrong
        value that costs a live mistake, and a control with no stated consequence is one an
        operator changes to see what happens.
        """
        with ui.element("div").classes("field"):
            ui.label(label).classes("field-label")
            with ui.column().classes("gap-1 w-full"):
                build()
                if hint:
                    ui.label(hint).classes("field-hint")


# --- Sections -----------------------------------------------------------------


def _client(editor: _Editor, cfg: ClientConfig) -> None:
    with theme.section("Client", anchor="client"):
        with ui.element("div").classes("field"):
            ui.label("Client id").classes("field-label")
            with ui.column().classes("gap-1"):
                ui.label(cfg.client_id).classes("mono")
                ui.label(
                    f"Not editable. It is the path to output/{cfg.client_id}/state.json, which "
                    "records every GTIN already published — renaming it orphans that file rather "
                    "than moving it, and every published GTIN would classify as new on the next "
                    "run."
                ).classes("field-hint")
        editor.text(
            ("display_name",),
            "Display name",
            cfg.display_name,
            "Appears in reports and logs. Nothing resolves by it.",
        )


def _wordpress(editor: _Editor, cfg: ClientConfig) -> None:
    wp = cfg.wordpress
    with theme.section("WordPress", anchor="wordpress"):
        editor.text(
            ("wordpress", "site_url"),
            "Site",
            wp.site_url,
            "No trailing slash. Every target URL is built from this, and a Digital Link whose "
            "target does not serve is refused before it is written.",
        )
        editor.text(
            ("wordpress", "username"),
            "User",
            wp.username,
            "The account the application password belongs to. It needs the editor or "
            "administrator role: one that authenticates but cannot publish fails mid-run, after "
            "some rows are already live.",
        )
        editor.text(
            ("wordpress", "post_type"),
            "Post type",
            wp.post_type,
            "The REST slug of the post type, not its display name.",
        )
        editor.choice(
            ("wordpress", "multilingual_plugin"),
            "Multilingual plugin",
            wp.multilingual_plugin,
            _PLUGINS,
            "`none` selects the no-op adapter: pages publish, translations are never linked, and "
            "nothing says so. Test WordPress below reports what the site actually answers.",
        )
        editor.text(
            ("wordpress", "wpml_helper_path"),
            "WPML helper path",
            wp.wpml_helper_path,
            "Used only when the plugin is wpml. WPML has no core REST route for language "
            "assignment, so each site hosts a small helper and its namespace is per-site.",
        )
        editor.items(
            ("wordpress", "languages"),
            "Languages",
            wp.languages,
            "Comma-separated. One page and one resolver link per language — and GS1's write "
            "replaces the whole links array, so a language removed here is a language deleted "
            "from the resolver on the next run.",
        )
        editor.text(
            ("wordpress", "default_language"),
            "Default language",
            wp.default_language,
            "Must be one of the languages above. It carries the default resolver link, so every "
            "QR that is not language-specific lands there.",
        )


def _gs1(editor: _Editor, cfg: ClientConfig) -> None:
    gs1 = cfg.gs1
    with theme.section("GS1", anchor="gs1"):
        editor.choice(
            ("gs1", "environment"),
            "Environment",
            gs1.environment,
            _ENVIRONMENTS,
            "`production` writes records that can never be deleted. Changing it to production "
            "asks for a typed confirmation below — the same decision the production gate asks "
            "about, made once here instead of once per run.",
        )
        editor.text(
            ("gs1", "account_number_test"),
            "Account (test)",
            gs1.account_number_test,
            "From the minted token's accountNumber claim. It is not derivable from the GTIN "
            "prefix or the GLN, and it differs per environment.",
        )
        editor.text(
            ("gs1", "account_number_production"),
            "Account (production)",
            gs1.account_number_production or "",
            "Confirm this against a live GET before trusting it — a write against an account "
            "that is not yours can still return 200.",
        )
        ui.label(
            "The four fields below hold the *names* of environment variables, never the "
            "credentials themselves. The values are set under Credentials."
        ).classes("note mt-4 mb-2")
        for path, label in (
            ("client_id_env_test", "Client id var (test)"),
            ("client_secret_env_test", "Client secret var (test)"),
            ("client_id_env_production", "Client id var (production)"),
            ("client_secret_env_production", "Client secret var (production)"),
        ):
            editor.text(("gs1", path), label, getattr(gs1, path) or "")


def _files(editor: _Editor, cfg: ClientConfig) -> None:
    with theme.section("Files", anchor="files"):
        editor.text(
            ("export", "path"),
            "Product export",
            cfg.export.path,
            "Authoritative, with no command-line override — a workbook saved anywhere else is "
            "invisible to the tool. To use a new export, upload it on the Data screen rather "
            "than pointing this at it.",
        )
        _file_row("…on disk", cfg.export.path)

        if cfg.process_list is None:
            ui.label("No `process_list` block, so every product in the export is planned.").classes(
                "note mt-4"
            )
        else:
            editor.text(("process_list", "path"), "Process list", cfg.process_list.path)
            editor.text(
                ("process_list", "gtin_column"),
                "GTIN column",
                cfg.process_list.gtin_column,
                "The header of the barcode column. Every other column is ignored by the tool "
                "and preserved by the editor on the Data screen.",
            )
            _file_row("…on disk", cfg.process_list.path)

        _file_row("Parsed products", f"output/{cfg.client_id}/data/products.json")
        if cfg.generator is not None:
            _file_row("Generated copy", f"output/{cfg.client_id}/data/generation_results.json")


def _file_row(label: str, path: str) -> None:
    """A path with how long ago it changed — gate 0's export cross-check, asked early."""
    fact = context.file_fact(path)
    with (
        ui.element("div")
        .classes("grid gap-x-8 gap-y-1 items-baseline")
        .style("grid-template-columns:12rem 1fr auto")
    ):
        ui.label(label).classes("note")
        try:
            shown = str(fact.path.relative_to(DATA_ROOT))
        except ValueError:
            shown = str(fact.path)
        ui.label(shown).classes("mono scroll-x")
        ui.label(fact.age).classes(f"tag {'tag-na' if fact.exists else 'tag-fail'}")


# --- Saving -------------------------------------------------------------------


def _save(editor: _Editor, cfg: ClientConfig) -> None:
    with theme.section("Save"):
        summary = ui.label("").classes("note")
        with ui.row().classes("gap-3 items-center") as confirm_row:
            ui.label("Type the client id to confirm production:").classes("note")
            confirm = ui.input(placeholder=cfg.client_id).props("outlined dense")
        outcome = ui.column().classes("w-full mt-3")

        def to_production() -> bool:
            return (
                editor.fields.text("gs1", "environment") == "production"
                and editor.fields.initial("gs1", "environment") != "production"
            )

        def refresh() -> None:
            count = len(editor.fields.changes())
            summary.text = "No changes." if not count else f"{count} field(s) changed, not saved."
            switching = to_production()
            confirm_row.set_visibility(switching)
            if not switching:
                # Cleared rather than merely hidden. A confirmation is for the decision in front
                # of the operator; one typed earlier in the session, for a choice since undone,
                # must not still be sitting there when production is selected a second time.
                confirm.value = ""

        editor.watch(refresh)
        refresh()

        def save() -> None:
            outcome.clear()
            with outcome:
                changes = editor.fields.changes()
                if not changes:
                    theme.band("Nothing to save.", "quiet")
                    return
                problem = _inconsistency(editor.fields)
                if problem:
                    theme.band(problem, "danger")
                    return
                if to_production() and str(confirm.value or "").strip() != cfg.client_id:
                    theme.band(
                        "Switching to the production resolver needs the client id typed in full "
                        f"({cfg.client_id}). Nothing was written.",
                        "danger",
                    )
                    return
                if _write(changes):
                    editor.fields.commit()
            refresh()

        with ui.row().classes("gap-3 mt-3 items-center"):
            theme.action("Save clients.yml", save, danger=True)
            theme.quiet_action("Reload from the file", ui.navigate.reload)
        ui.label(
            "Only changed fields are written. The file's comments, and every block this form "
            "does not show, are preserved byte for byte. The result is validated before it "
            "replaces the file, and the previous version is kept as clients.yml.bak."
        ).classes("note mt-2")


def _write(changes: dict[tuple[str, ...], Parsed]) -> bool:
    """Apply the edits, validate, and say exactly what landed. ``True`` when the file changed."""
    path = DEFAULT_CLIENTS_PATH
    try:
        edited = config_edit.apply_edits(path.read_text(encoding="utf-8"), changes)
        backup = config_edit.write_validated(path, edited)
    except (ConfigError, OSError) as exc:
        theme.band(f"Not written — {exc}", "danger")
        ui.label("clients.yml is unchanged. Fix the field named above and save again.").classes(
            "remedy"
        )
        return False

    theme.band(f"Saved. The previous version is kept as {backup.name}.", "quiet")
    for parts, value in changes.items():
        shown = ", ".join(value) if isinstance(value, list) else value
        ui.label(f"{'.'.join(parts[2:])} → {shown or '(blank)'}").classes("mono")
    ui.label(
        "Run the preflight before publishing: a field can be valid and still wrong, and only "
        "the checks ask the site rather than the file."
    ).classes("note mt-3")
    return True


def _inconsistency(fields: FieldSet) -> str:
    """The inconsistencies the schema cannot express, caught before the file is touched.

    Both would otherwise validate and fail later — the first at execute time, with the default
    resolver link pointing at a language that has no page; the second at the first GS1 call,
    after parse, plan and a clean dry run have all passed.
    """
    languages = fields.items("wordpress", "languages")
    default = fields.text("wordpress", "default_language")
    if not languages:
        return "At least one language is required."
    if default not in languages:
        return (
            f"The default language {default!r} is not in the language list "
            f"({', '.join(languages)}). The default resolver link would point at a language "
            "with no page."
        )
    if fields.text("gs1", "environment") == "production":
        missing = [
            label
            for label, key in (
                ("account number", "account_number_production"),
                ("client id variable", "client_id_env_production"),
                ("client secret variable", "client_secret_env_production"),
            )
            if not fields.text("gs1", key)
        ]
        if missing:
            return (
                "The production environment needs its production "
                + ", ".join(missing)
                + ". Without them, every GS1 call fails at the first request."
            )
    return ""


# --- Credentials --------------------------------------------------------------


def _credentials(cfg: ClientConfig) -> None:
    """Write-only fields over ``.env``. Nothing here reads a value back onto the screen."""
    wanted = [
        (cfg.wordpress.app_password_env, "WordPress application password", True),
        (cfg.gs1.client_id_env_test, "GS1 client id (test)", False),
        (cfg.gs1.client_secret_env_test, "GS1 client secret (test)", False),
        (cfg.gs1.client_id_env_production or "", "GS1 client id (production)", False),
        (cfg.gs1.client_secret_env_production or "", "GS1 client secret (production)", False),
        (
            cfg.generator.api_key_env if cfg.generator else "",
            "Anthropic API key (optional — enables generating copy here)",
            False,
        ),
    ]
    known = env_edit.describe([name for name, _, _ in wanted if name])

    with theme.section("Credentials", anchor="credentials"):
        ui.label(
            "These set values in .env, and never show one back. A field that displayed a "
            "production credential would put it in the next screenshot and support ticket, and "
            "would prove nothing anyway — Test below asks WordPress and GS1 instead. Leave a box "
            "empty to leave that credential alone."
        ).classes("note")
        theme.band(
            "The Anthropic key is the one optional field. Left empty, this machine holds no LLM "
            "credential and reaches Anthropic not at all — copy is written on the maintainer's "
            "machine and imported on the Content screen. Filled in, the Content screen can write "
            "it here instead, and the run stops depending on a file hand-off.",
            "quiet",
        )

        boxes: dict[str, Any] = {}
        for name, label, is_app_password in wanted:
            if not name or name in boxes:
                continue
            boxes[name] = _credential_row(label, known[name], is_app_password)

        result = ui.column().classes("w-full mt-3")

        def save() -> None:
            values = {name: str(box.value or "") for name, box in boxes.items()}
            written = sorted(name for name, value in values.items() if value.strip())
            result.clear()
            with result:
                if not written:
                    theme.band("Nothing to save — every box was left empty.", "quiet")
                    return
                try:
                    backup = env_edit.write_values(values)
                except OSError as exc:
                    theme.band(f"Could not write .env — {exc}", "danger")
                    return
                for box in boxes.values():
                    box.value = ""
                theme.band(
                    f"Set {', '.join(written)}. The previous .env is kept as {backup.name}, and "
                    "both files are mode 600. Press Test below to prove the new value works.",
                    "quiet",
                )

        theme.action("Save credentials to .env", save, danger=True)


def _credential_row(label: str, secret: env_edit.Secret, is_app_password: bool) -> Any:
    hint = f"Sets {secret.name} in .env."
    if is_app_password and secret.looks_truncated:
        hint += (
            f" Currently set, but only {secret.groups} group(s) long — a WordPress application "
            f"password has {env_edit.APP_PASSWORD_GROUPS}, so this one was almost certainly "
            "truncated at a space. Re-enter it."
        )
    elif is_app_password:
        hint += " WordPress issues it as six space-separated groups; paste all six."

    with ui.element("div").classes("field"):
        with ui.row().classes("gap-3 items-baseline"):
            ui.label(label).classes("field-label")
            ui.label("set" if secret.present else "not set").classes(
                f"tag {'tag-ok' if secret.present else 'tag-fail'}"
            )
        with ui.column().classes("gap-1 w-full"):
            box = ui.input(password=True, placeholder="unchanged").props("outlined dense")
            box.classes("w-full")
            ui.label(hint).classes("field-hint")
    return box


# --- Tests and the read-only blocks -------------------------------------------


def _tests(cid: str) -> None:
    """Live checks, run as the doctor rather than reimplemented — see :mod:`lib.preflight`."""
    with theme.section("Test", anchor="tests"):
        ui.label(
            "Each button runs `python -m scripts.doctor` and shows the checks that answer for "
            "that part of the form. They are the preflight's own checks, not a second opinion: "
            "a credential test that disagreed with the preflight would be worse than none."
        ).classes("note")

        # Async, and the subprocess runs off the event loop. Both halves are needed, for the
        # reason `runner.run_off_the_loop` gives: a blocking call in a sync handler holds the
        # loop until the command has already finished, so the running-state the button paints
        # arrives at the browser only once there is nothing left to report.
        async def go(names: Sequence[str], *, offline: bool) -> None:
            status.text = "running…"
            payload, result = await runner.run_json_off_the_loop(
                runner.doctor_argv(cid, offline=offline)
            )
            body.clear()
            status.text = result.display_command
            with body:
                if not isinstance(payload, list):
                    theme.band("The check did not return readable results.", "danger")
                    ui.label(result.stderr or result.stdout or "(no output)").classes("console")
                    return
                shown = [entry for entry in payload if entry.get("name") in names]
                for entry in shown or payload:
                    theme.check_row(
                        str(entry["status"]),
                        str(entry["title"]),
                        str(entry["detail"]),
                        str(entry.get("remedy") or ""),
                    )
                # A button shows two or three checks out of ten, so the run as a whole can fail
                # on a check this button did not ask about. Saying so is the difference between
                # a partial view and a misleading one.
                if result.returncode and not any(e["status"] == "fail" for e in shown):
                    theme.band(
                        "These checks passed, but something else in the preflight did not. "
                        "The Preflight screen has the full list.",
                        "warn",
                    )

        with ui.row().classes("gap-3 mt-3 flex-wrap"):
            theme.quiet_action(
                "Check the file (offline)", lambda: go(("config", "generator_block"), offline=True)
            )
            theme.quiet_action(
                "Test WordPress", lambda: go(("site_serves", "wordpress"), offline=False)
            )
            theme.quiet_action("Test GS1", lambda: go(("gs1",), offline=False))
        ui.label(
            "The two network tests authenticate and mint a GS1 token. Both are read-only: the "
            "GS1 request is a GET against a GTIN from your own catalogue, and nothing is written."
        ).classes("note mt-2")

        status = ui.label("").classes("note mt-4 mono")
        body = ui.column().classes("w-full gap-0")


#: The read-only block's heading. Not "Settled elsewhere": that read as "kept somewhere else", and
#: an operator preparing a handover took it to mean these settings were missing from the pack.
FIXED_TITLE: Final = "Fixed settings — not editable here"


def _read_only(cfg: ClientConfig) -> None:
    """The blocks a form must not offer to edit, each with the reason it is on this list."""
    # Folded by default. It is the longest section on the screen and the only one with nothing to
    # do on it, so open it sat between the operator and the Test buttons every single visit.
    with theme.section(
        FIXED_TITLE,
        anchor="read-only",
        collapsed=True,
        explain=(
            "These are part of the client settings and move with them in a handover pack. They "
            "were decided against the live site and the real export, so this screen does not "
            "offer to change them."
        ),
    ):
        _fixed(
            "Content generation",
            f"prompt version {cfg.generator.prompt_version}, model {cfg.generator.model}"
            if cfg.generator
            else "not set up",
            "Keep this on. It is what stops a product from going live without its description: "
            "with it removed, a product with no written text publishes with an empty one. It "
            "stays on whether or not this machine writes text itself.",
        )
        _fixed(
            "ACF field map",
            f"{len(cfg.wordpress.acf_map)} field(s)"
            if cfg.wordpress.acf_map
            else "not used — pages render from the body template",
            "Which box on the website's product page each value goes into. The names come from "
            "the site's theme and were checked against the live site; a wrong one does not show "
            "an error, it leaves that box empty on every page.",
        )
        _fixed(
            "Export column map",
            f"{cfg.export.format} — "
            f"{len(cfg.export.gdsn_map) or len(cfg.export.column_map)} mapped field(s)",
            "Which column of the GS1 export each page value is read from — for example which one "
            "holds the product name. Chosen against the real export; a change alters what every "
            "page says.",
        )
        _fixed(
            "Category map",
            f"{len(cfg.categories.brick_category_map)} brick(s) → "
            f"{len(cfg.categories.terms)} term(s)"
            if cfg.categories
            else "not set up",
            "Which website category each GS1 product group goes into. Agreed with the client; it "
            "cannot be worked out from the export.",
        )
        ui.label(
            "To change one, edit clients.yml — with the reason written beside the change — then "
            "press Check the file above."
        ).classes("note mt-4")


def _fixed(label: str, value: str, why: str) -> None:
    with ui.element("div").classes("field"):
        ui.label(label).classes("field-label")
        with ui.column().classes("gap-1"):
            ui.label(value).classes("mono scroll-x")
            ui.label(why).classes("field-hint")
