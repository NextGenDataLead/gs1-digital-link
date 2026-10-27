"""Client configuration loader (IMPLEMENTATION_SPEC §2.4, §4.2).

Loads and validates ``clients.yml`` into typed, frozen config models, applying the
``defaults`` block and enforcing the column-mapping contract (edge E6). Secrets are
handled lazily: this module never reads environment variables and never returns or
logs secret values — only the *names* of the env vars that hold them.

The Phase-2 GS1 client (``lib/gs1_dl_client.py``) defines a *resolved*,
single-environment ``GS1Config``; here :class:`GS1Config` models the full multi-
environment ``clients.yml`` block and bridges to the resolved shape via
:meth:`GS1Config.resolve`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lib.data_dir import CODE_ROOT, data_root
from lib.errors import ConfigError, ExportParseError
from lib.gdsn import GdsnSource
from lib.generator import DEFAULT_PROMPT_VERSION
from lib.gs1_dl_client import GS1Config as ResolvedGS1Config
from lib.gs1_dl_client import ResolverSettings
from lib.records import ProductRecord, is_valid_target_path

#: Default locations. ``clients.yml`` belongs to the installation, so it lives in the data folder;
#: the schema ships with the code.
DEFAULT_CLIENTS_PATH: Final = data_root() / "clients.yml"
_SCHEMA_PATH: Final = CODE_ROOT / "schema" / "clients.schema.json"

#: Config sub-blocks that inherit from the top-level ``defaults`` block.
_INHERITED_BLOCKS: Final = ("gs1", "wordpress", "qr", "flow")

#: ProductRecord field names a ``gdsn_map`` may target (``extras`` uses ``gdsn_extras``).
_PRODUCT_FIELDS: Final[frozenset[str]] = frozenset(ProductRecord.model_fields) - {"extras"}

Environment = Literal["test", "production"]


# --- Config models (§2.4) ----------------------------------------------------


class GS1Config(BaseModel):
    """Full multi-environment GS1 config for one client (§2.4).

    Mirrors the ``gs1`` block of ``clients.yml``. Use :meth:`resolve` to obtain the
    single-environment shape the Digital Link client consumes.
    """

    model_config = ConfigDict(frozen=True)

    account_number_test: str
    account_number_production: str | None = None
    client_id_env_test: str
    client_secret_env_test: str
    client_id_env_production: str | None = None
    client_secret_env_production: str | None = None
    environment: Environment = "test"
    api_version: Literal["v2"] = "v2"
    identification_key_type: Literal["Gtin"] = "Gtin"
    digital_link_url_pattern: str = "https://id.gs1.org/01/{gtin14}"
    resolver_settings: ResolverSettings = Field(default_factory=ResolverSettings)
    default_media_type: str = "text/html"
    batch_size: int = 50

    def resolve(self, environment: Environment | None = None) -> ResolvedGS1Config:
        """Produce the resolved, single-environment config for the GS1 client.

        Args:
            environment: Environment to resolve for; defaults to ``self.environment``.

        Returns:
            The ``lib.gs1_dl_client.GS1Config`` for the chosen environment.

        Raises:
            ConfigError: If the chosen environment's account/credentials are absent.
        """
        env = environment or self.environment
        account: str | None
        client_id_env: str | None
        client_secret_env: str | None
        if env == "test":
            account = self.account_number_test
            client_id_env = self.client_id_env_test
            client_secret_env = self.client_secret_env_test
        else:
            account = self.account_number_production
            client_id_env = self.client_id_env_production
            client_secret_env = self.client_secret_env_production
        if not (account and client_id_env and client_secret_env):
            raise ConfigError(f"gs1 config is missing {env} account/credentials")
        return ResolvedGS1Config(
            account_number=account,
            client_id_env=client_id_env,
            client_secret_env=client_secret_env,
            environment=env,
            resolver_settings=self.resolver_settings,
            batch_size=self.batch_size,
        )


class ExportConfig(BaseModel):
    """How a client's product export is located and mapped (§2.4, §3)."""

    model_config = ConfigDict(frozen=True)

    format: Literal["flat", "gdsn"] = "flat"
    path: str
    # Flat single-sheet path (§3.2).
    column_map: dict[str, str] = Field(default_factory=dict)
    extras_columns: list[str] = Field(default_factory=list)
    # GDSN datapool path (§3 extension).
    #: Market codes in the order to consult them (e.g. ``["528", "056", "276", "442"]``).
    #: The first market with a non-blank value wins, per product/field/language; the same
    #: order picks scalars. Replaces the old ``market_language`` 1:1 map — every market row
    #: carries every language, so which market *has* a value varies by product, and a static
    #: map both mis-resolves and undercounts coverage. The site's languages come from
    #: ``wordpress.languages`` instead.
    market_priority: list[str] = Field(default_factory=list)
    gdsn_map: dict[str, GdsnSource] = Field(default_factory=dict)
    gdsn_extras: dict[str, GdsnSource] = Field(default_factory=dict)

    @property
    def all_sources(self) -> dict[str, GdsnSource]:
        """Every declared source, mapped fields first then extras, under one name.

        The split between the two maps is about *where the value lands* — a `ProductRecord`
        field or the pass-through `extras` bag — and nothing else. Questions like "is this
        mandatory?" (E23) and "does it get a matrix column?" are properties of the source, so
        every consumer asking one of those reads this instead of picking a map.

        That is not tidiness. `missing_mandatory` walked `gdsn_map` alone, so `required` on an
        extra was silently ignored — the shape `multivalue` had in #96, where a flag meant
        something on one resolution path and nothing on the others. Worse, E23 decides whether a
        SKU may publish at all: a consumer reading one map while another read both would hold a
        product on one surface and publish it from the other.

        Safe to merge because :func:`_validate_unique_field_names` refuses a config that
        declares one name in both maps, so nothing here can shadow anything.
        """
        return {**self.gdsn_map, **self.gdsn_extras}


class TaxonomyConfig(BaseModel):
    """How a WordPress taxonomy's terms are sourced."""

    model_config = ConfigDict(frozen=True)

    map_from_column: str


class WordPressConfig(BaseModel):
    """WordPress target configuration for one client (§2.4)."""

    model_config = ConfigDict(frozen=True)

    site_url: str
    username: str
    app_password_env: str
    post_type: str = "page"
    post_status: str = "publish"
    multilingual_plugin: Literal["none", "polylang", "wpml"] = "none"
    #: Path to the site-side WPML helper route (``multilingual_plugin: wpml`` only). WPML has
    #: no core REST route for language assignment or translation linking, so each site hosts a
    #: small helper; the namespace is per-site, hence config rather than a constant. See
    #: ``lib.multilingual.WPMLAdapter`` and ``docs/clients/democlient-page-adapter.md`` §7.
    wpml_helper_path: str = "/wp-json/gs1dl/v1/translations"
    default_language: str = "nl"
    languages: list[str] = Field(default_factory=lambda: ["nl"])
    image_handling: Literal["url_in_export", "local_folder", "manual"] = "url_in_export"
    taxonomies: dict[str, TaxonomyConfig] = Field(default_factory=dict)
    #: ``{acf_field_name: product_record_field}`` for themes that render from ACF rather than
    #: ``post_content`` (Oxygen, and similar page builders). Empty means the client renders
    #: from the body template, as in Phase 5. See :mod:`lib.acf`.
    acf_map: dict[str, str] = Field(default_factory=dict)
    slug_pattern: str | None = None
    target_url_pattern: str | None = None


def _default_qr_formats() -> list[Literal["svg", "png", "eps"]]:
    return ["svg", "png"]


class QRConfig(BaseModel):
    """QR rendering configuration (§2.4)."""

    model_config = ConfigDict(frozen=True)

    formats: list[Literal["svg", "png", "eps"]] = Field(default_factory=_default_qr_formats)
    size_mm: int = 20
    error_correction: Literal["L", "M", "Q", "H"] = "M"
    dpi: int = 300


class TemplateConfig(BaseModel):
    """Template override location and per-language filenames (§2.4)."""

    model_config = ConfigDict(frozen=True)

    override_dir: str | None = None
    files: dict[str, str] = Field(default_factory=dict)


class GS1LinkConfig(BaseModel):
    """One GS1 resolver link definition (§2.4)."""

    model_config = ConfigDict(frozen=True)

    link_type: str
    #: Whether this link type is the resolver's default — applied to the **default
    #: language only**, however many languages the record carries.
    default: bool = False
    public: bool = True
    title_pattern: str | None = None


class FlowConfig(BaseModel):
    """Interactive-flow behaviour toggles (§2.4)."""

    model_config = ConfigDict(frozen=True)

    on_change: str = "prompt"
    on_missing_field: str = "prompt"
    batch_size: int = 50


class ProcessListConfig(BaseModel):
    """Operator-maintained list of exactly which GTINs a run may touch.

    Not part of the datasource export and not in the original spec: a deliberate,
    per-client extension. **Every GTIN in the file is processed** — the tool reads no
    status columns and interprets no cell values, so the file's only meaning is
    membership. The operator prepares it by deleting the rows that should not be
    processed, applying whatever rule their business uses.

    Only the GTIN column is needed, and it is named here so a client can label it
    however they like (``Barcode``, ``EAN``, ``GTIN``…) without a code change. Any
    other columns in the file are ignored, so an operator can keep their own working
    notes alongside the barcodes.

    This replaced a version that read "already on website" and "already in GS1"
    columns using presence-semantics (any non-blank cell meant ``True``). That was
    right only for files marking rows with ``X``; a file saying ``no`` silently meant
    the opposite. See ``lib/process_list.py`` for the full account.

    ``target_url_column`` is the one exception, and it reads a *value*, not a status: the
    address of a page this tool did not make. A product that already has a page on the site
    under its own slug (``/democlient/notenkraker-2/``) cannot be found by ``slug_pattern``, so
    a ``--only links`` run would otherwise point its permanent GS1 record at a URL that does
    not exist and be refused. The cell is the default language's page and the record's default
    link; another language is linked when that page names a translation for it (its ``hreflang``),
    and left out when it names none. A blank cell means
    "no page named" and changes nothing. Unset (the default), no column is read at all.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    gtin_column: str = "Barcode"
    target_url_column: str | None = None


class CategoryConfig(BaseModel):
    """GPC brick → client category-term mapping (Phase 7.5).

    Client-owned, signed-off data that is *not* derivable from the feed: GPC bricks span
    marketing categories and a client's own scheme is not purely semantic (see
    ``docs/clients/democlient-page-adapter.md`` §5.7). ``terms`` is the closed set of allowed
    category terms; ``brick_category_map`` maps a GPC brick code to one of them; ``overrides``
    resolves bricks that span categories, per GTIN, and win over the brick map.

    ``on_unmapped`` is fixed at ``"warn"`` — an unmapped brick leaves the category unset and
    is reported; the tool never guesses. ``require_terms_exist`` records the resolved open
    decision (require the WordPress term to pre-exist rather than auto-creating it), and
    ``run_execute`` enforces it where it sets the page's term: a category the site has no term
    for, in the page's language, refuses the row (or, off, only warns).
    """

    model_config = ConfigDict(frozen=True)

    terms: list[str]
    brick_category_map: dict[str, str] = Field(default_factory=dict)
    overrides: dict[str, str] = Field(default_factory=dict)
    on_unmapped: Literal["warn"] = "warn"
    require_terms_exist: bool = True

    @model_validator(mode="after")
    def _check_terms(self) -> CategoryConfig:
        """Reject an empty/duplicate ``terms`` list or a map/override value outside it."""
        allowed = set(self.terms)
        if not self.terms or len(allowed) != len(self.terms):
            raise ValueError("categories.terms must be non-empty and unique")
        for brick, term in self.brick_category_map.items():
            if term not in allowed:
                raise ValueError(f"brick_category_map[{brick!r}] = {term!r} is not in terms")
        for gtin, term in self.overrides.items():
            if term not in allowed:
                raise ValueError(f"overrides[{gtin!r}] = {term!r} is not in terms")
        return self


class GeneratorConfig(BaseModel):
    """Content-generator (LLM copy) settings for one client (generator SPEC §"Config additions").

    Governs the copy generator that writes the tagline + Eigenschappen. The headless API backend
    (``scripts/run_generate.py --backend api`` via :class:`lib.llm.AnthropicClient`) is opt-in via
    ``enabled``; the in-session producer needs no key. Secrets are handled as elsewhere —
    ``api_key_env`` names the env var holding the API key, never the key itself. ``prompt_version``
    selects the frozen voice template (``prompts/{client}/generation.{prompt_version}.md``) and is
    part of every input fingerprint, so copy written under the old one reads as stale.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    model: str = "claude-sonnet-5"
    prompt_version: str = DEFAULT_PROMPT_VERSION
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = Field(default=1024, ge=1)


class MediaConfig(BaseModel):
    """Image convert/resize params, video folders, and media ACF field names (Phase 9.5).

    Media is written to WordPress imperatively at execute time (an attachment id is only known
    after upload), not via the static ``acf_map`` — so the ACF field *names* live here, keeping
    field names in config rather than hardcoded (page-adapter §8). ``image_write_shape`` records
    whether the image ACF fields take an attachment id or a URL string; the read-back shape is not
    the write shape (page-adapter §3 trap), so it is confirmed live and carried as config, meaning
    a wrong guess is a config edit, not a code change. ``video_folders`` are the operator's
    per-language folders; ``video_map_path`` is the client-confirmed name→GTIN mapping;
    ``video_transcode`` selects the ffmpeg H.264/MP4 prepare step (the source ``.mpg`` will not
    play in an HTML5 ``<video>``).
    """

    model_config = ConfigDict(frozen=True)

    image_max_dim: int = Field(default=1600, ge=1)
    image_quality: int = Field(default=85, ge=1, le=100)
    header_image_field: str = "product_header_image"
    regular_image_field: str = "product_regular_image"
    video_file_field: str = "product_header_video_file"
    image_write_shape: Literal["id", "url"] = "id"
    video_folders: dict[str, str] = Field(default_factory=dict)
    video_map_path: str | None = None
    video_transcode: bool = False
    ffmpeg_bin: str = "ffmpeg"
    # When true, run_plan and run_execute restrict to GTINs with a client-confirmed video in every
    # language (see lib.media_video.fully_mapped_gtins) — every other GTIN is blocked from runs.
    restrict_to_mapped_gtins: bool = False
    # Only meaningful with restrict_to_mapped_gtins. When true, a GTIN with no confirmed video in
    # some language is NOT held: it publishes with no video there, and every surface lists it as
    # missing one. A GTIN confirmed to two files in one language is still held — the page could not
    # tell which to show — and the "already published" drop restrict_to_mapped_gtins also drives is
    # unchanged. See lib.media_video.VideoGate.
    publish_without_video: bool = False
    # When true, a GTIN whose SOURCE image_url is blank is held out of the plan (E22) — a hero
    # image is treated as required, not optional. Off by default so text-only/imageless clients
    # are unaffected. This is a plan-time source-completeness gate; a runtime image fetch failure
    # (404, undecodable) still degrades gracefully and publishes the page (E7) regardless.
    require_hero_image: bool = False


class ClientConfig(BaseModel):
    """The full resolved configuration for one client (§2.4)."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    display_name: str
    enabled: bool = True
    gs1: GS1Config
    export: ExportConfig
    wordpress: WordPressConfig
    template: TemplateConfig | None = None
    gs1_links: list[GS1LinkConfig] = Field(default_factory=list)
    qr: QRConfig | None = None
    flow: FlowConfig | None = None
    process_list: ProcessListConfig | None = None
    categories: CategoryConfig | None = None
    generator: GeneratorConfig | None = None
    media: MediaConfig | None = None


# --- Loading (§4.2) ----------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base`` (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _validate_column_targets(client_id: str, export: ExportConfig) -> None:
    """Enforce the column-mapping contract at load time (edge E6).

    Raises:
        ExportParseError: If a mapping targets a field ``ProductRecord`` lacks.
    """
    if export.format == "flat":
        for column, target in export.column_map.items():
            if not is_valid_target_path(target):
                raise ExportParseError(
                    f"client {client_id!r}: column {column!r} maps to unknown field {target!r}"
                )
    else:  # gdsn
        for field in export.gdsn_map:
            if field not in _PRODUCT_FIELDS:
                raise ExportParseError(
                    f"client {client_id!r}: gdsn_map targets unknown field {field!r}"
                )


def _validate_unique_attributes(client_id: str, export: ExportConfig) -> None:
    """One GDSN attribute, one field.

    Two sources reading the same sheet and attribute carry the same value under two names, which
    nothing downstream can tell apart. Attr 3301 was mapped as ``product_name`` and declared again
    as ``gdsn_extras.functional_name`` for months, and the duplication surfaced everywhere the
    value was consumed: two §4 rows asking for one MyGS1 paste, two identical coverage-matrix
    columns, a client template printing the product name under itself, and a generator input read
    from the extra with the mapped field as a fallback for a value that could never differ.

    Checked here rather than pinned in a test over ``clients.example.yml`` because the real
    ``clients.yml`` is gitignored — this is the only place a check can see the file that had the
    bug. ``lib.preflight.check_config`` calls :func:`load_clients`, so ``scripts.doctor`` reports
    it before any run.

    The sheet is half the identity: GDSN attribute numbers are unique only within a sheet, so
    comparing numbers alone would refuse a legitimate config.

    Raises:
        ExportParseError: If two sources read the same ``(sheet, attribute)``.
    """
    if export.format != "gdsn":
        return
    seen: dict[tuple[str, str], str] = {}
    for name, src in [*export.gdsn_map.items(), *export.gdsn_extras.items()]:
        key = (src.sheet, src.attribute)
        first = seen.get(key)
        if first is not None:
            raise ExportParseError(
                f"client {client_id!r}: {first!r} and {name!r} both read "
                f"{src.sheet} attr {src.attribute} — one attribute, one field"
            )
        seen[key] = name


def _validate_unique_field_names(client_id: str, export: ExportConfig) -> None:
    """One field name, one source — across both maps.

    :attr:`ExportConfig.all_sources` merges ``gdsn_map`` and ``gdsn_extras`` so that every
    consumer asking whether a field is mandatory reads one mapping. A name declared in both
    would let the extra silently win there while the parser still wrote the mapped field, so the
    value published and the value checked would come from different sheets with nothing to say
    so. Sibling of :func:`_validate_unique_attributes`, one level up: that one refuses two names
    for one attribute, this one refuses two attributes under one name.

    Raises:
        ExportParseError: If a field name appears in both maps.
    """
    if export.format != "gdsn":
        return
    for name in export.gdsn_map:
        if name in export.gdsn_extras:
            raise ExportParseError(
                f"client {client_id!r}: {name!r} is declared in both gdsn_map and gdsn_extras — "
                f"one field name, one source"
            )


def load_clients(path: str | Path = DEFAULT_CLIENTS_PATH) -> dict[str, ClientConfig]:
    """Load, validate, and normalise ``clients.yml`` (§4.2).

    Args:
        path: Path to the config file.

    Returns:
        Mapping of ``client_id`` to its :class:`ClientConfig`, with ``defaults``
        applied.

    Raises:
        ConfigError: If the file is missing/malformed or fails schema validation.
        ExportParseError: If a client's column mapping targets an unknown field (E6), or two of
            its sources read the same GDSN sheet and attribute.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read clients config at {path}: {exc}") from exc
    data = yaml.safe_load(raw) or {}

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        # exc.message names only structural keys / env-var names — safe to surface.
        raise ConfigError(f"clients config is invalid: {exc.message}") from exc

    defaults = data.get("defaults", {})
    clients: dict[str, ClientConfig] = {}
    for client_id, block in data["clients"].items():
        merged = dict(block)
        for key in _INHERITED_BLOCKS:
            inherited = _deep_merge(defaults.get(key, {}), block.get(key, {}))
            if inherited:
                merged[key] = inherited
        merged["client_id"] = client_id
        try:
            config = ClientConfig.model_validate(merged)
        except ValueError as exc:
            raise ConfigError(f"client {client_id!r} is invalid: {exc}") from exc
        _validate_column_targets(client_id, config.export)
        _validate_unique_attributes(client_id, config.export)
        _validate_unique_field_names(client_id, config.export)
        clients[client_id] = config
    return clients


def get_client(
    client_id: str | None = None, path: str | Path = DEFAULT_CLIENTS_PATH
) -> ClientConfig:
    """Load one client's configuration by id (§4.2).

    The normal deployment is **one client per repository**, so ``client_id`` may be omitted
    when the config defines exactly one client — see :func:`resolve_client_id`. Passing an id
    explicitly always works and is required once a second client exists.

    Args:
        client_id: Key under ``clients:`` in the config file, or ``None`` to use the only
            client defined.
        path: Path to the config file.

    Returns:
        The client's :class:`ClientConfig`.

    Raises:
        ConfigError: If the client id is unknown, or omitted when the config defines zero or
            more than one client (or the file fails to load).
    """
    clients = load_clients(path)
    resolved = client_id if client_id is not None else resolve_client_id(None, clients)
    try:
        return clients[resolved]
    except KeyError as exc:
        known = ", ".join(sorted(clients)) or "none"
        raise ConfigError(f"unknown client_id {resolved!r} (defined: {known})") from exc


def resolve_client_id(
    client_id: str | None, clients: Mapping[str, ClientConfig] | None = None
) -> str:
    """Return the client id to act on, defaulting to the only one defined.

    One client per repository is the normal case, so requiring the id on every command is
    ceremony. It stays **mandatory** the moment a second client exists: silently picking one
    of several would be a way to publish the wrong catalogue to the wrong site.

    Args:
        client_id: An explicit id, or ``None`` to infer it.
        clients: Already-loaded clients, to avoid re-reading the config file.

    Returns:
        The resolved client id.

    Raises:
        ConfigError: If ``client_id`` is ``None`` and the config defines zero, or more than
            one, client.
    """
    if client_id is not None:
        return client_id
    loaded = clients if clients is not None else load_clients()
    match sorted(loaded):
        case [only]:
            return only
        case []:
            raise ConfigError("clients config defines no clients — nothing to act on")
        case many:
            raise ConfigError(
                f"clients config defines {len(many)} clients ({', '.join(many)}) — "
                "name the one to act on explicitly"
            )
