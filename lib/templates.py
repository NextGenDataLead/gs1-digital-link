"""Product-page template rendering (IMPLEMENTATION_SPEC §4.6, §3.4).

Renders one :class:`~lib.records.ProductRecord` into a localised HTML fragment using
Mustache templates (``pystache``). Templates are resolved with a client-override-first,
default-fallback strategy so every client gets a working page out of the box while still
being able to ship a bespoke layout.

Resolution order (§4.6), for language ``L`` and client ``C``::

    input/C/templates/<files[L] or "product.L.html">                        # client, data folder
    <base>/<override_dir or "templates/C">/<files[L] or "product.L.html">   # client, release
    <base>/templates/_default/product.L.html                                # fallback
    -> TemplateError                                                        # none

The first is relative to the working directory, which is the data folder (:mod:`lib.client_files`):
a client's templates travel in its handover pack there, and a release that ships none of them
still renders that client's pages.

The variable vocabulary (§3.4) is the contract between this engine and template authors;
it is documented for clients in ``docs/template-variables.md`` (a later phase). Values are
HTML-escaped by ``pystache`` (edge E13); a template that references an ``extras`` key the
product lacks renders empty and warns once (edge E12).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Final

import pystache

from lib import client_files
from lib.config import TemplateConfig
from lib.errors import TemplateError
from lib.records import ProductRecord
from lib.units import decode_net_content

_log = logging.getLogger(__name__)

#: Repo root — the base against which the repo-root-relative template paths resolve.
_ROOT: Final = Path(__file__).resolve().parent.parent

#: Directory holding the built-in fallback templates.
_DEFAULT_DIRNAME: Final = "_default"
_TEMPLATES_DIRNAME: Final = "templates"

#: Matches ``{{extras.<name>}}`` references so the engine can warn on missing keys (E12).
_EXTRAS_TAG_RE: Final = re.compile(r"\{\{\s*extras\.([A-Za-z0-9_-]+)\s*\}\}")


class TemplateEngine:
    """Resolve and render product templates for one client (§4.6).

    Args:
        client_id: The client whose templates take precedence over the defaults.
        template_config: The client's ``template`` config block (``override_dir`` and
            per-language ``files``); ``None`` when the client ships no override — the
            engine then falls back to ``templates/<client_id>/`` and the defaults.
        base_dir: Root the template paths resolve against. Defaults to the repo root;
            overridable so tests can point at a temporary template tree.
        data_root: The data folder, tried first. Defaults to the working directory.
    """

    def __init__(
        self,
        client_id: str,
        template_config: TemplateConfig | None,
        *,
        base_dir: Path = _ROOT,
        data_root: Path = Path(),
    ) -> None:
        self._client_id = client_id
        self._config = template_config
        self._base_dir = base_dir
        self._data_root = data_root
        self._renderer = pystache.Renderer(missing_tags="ignore")
        # Deduplicates the E12 warning to once per (template path, missing key) per engine.
        self._warned_extras: set[tuple[str, str]] = set()

    def render(
        self,
        product: ProductRecord,
        language: str,
        client_meta: dict[str, str],
    ) -> str:
        """Render ``product`` for ``language`` into an HTML fragment.

        Args:
            product: The product to render.
            language: ISO 639-1 language code for this page.
            client_meta: Client-level template context — ``display_name``, ``id`` and,
                optionally, ``default_language`` used to resolve localised text when the
                requested language is absent.

        Returns:
            The rendered HTML string.

        Raises:
            TemplateError: If neither the client nor the default template exists for
                ``language``.
        """
        template_path = self._resolve(language)
        source = template_path.read_text(encoding="utf-8")
        context = self._build_context(product, language, client_meta)
        self._warn_missing_extras(source, product, template_path)
        rendered = self._renderer.render(source, context)
        return str(rendered)

    def _resolve(self, language: str) -> Path:
        """Return the template path for ``language`` (client, then default) or raise."""
        candidates = [
            self._data_template(language),
            self._client_template(language),
            self._default_template(language),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        tried = ", ".join(str(c) for c in candidates)
        raise TemplateError(
            f"no template for client {self._client_id!r} language {language!r}; tried: {tried}"
        )

    def _filename(self, language: str) -> str:
        filename = self._config.files.get(language) if self._config else None
        return filename or f"product.{language}.html"

    def _data_template(self, language: str) -> Path:
        """The data folder's template for ``language`` (may not exist)."""
        folder = client_files.data_dir(self._client_id, client_files.TEMPLATES, self._data_root)
        return folder / self._filename(language)

    def _client_template(self, language: str) -> Path:
        """The release's client-override template path for ``language`` (may not exist)."""
        if self._config and self._config.override_dir:
            client_dir = self._base_dir / self._config.override_dir
        else:
            client_dir = self._base_dir / _TEMPLATES_DIRNAME / self._client_id
        return client_dir / self._filename(language)

    def _default_template(self, language: str) -> Path:
        """The built-in fallback template path for ``language`` (may not exist)."""
        return self._base_dir / _TEMPLATES_DIRNAME / _DEFAULT_DIRNAME / f"product.{language}.html"

    def _build_context(
        self,
        product: ProductRecord,
        language: str,
        client_meta: dict[str, str],
    ) -> dict[str, Any]:
        """Build the Mustache context from ``product`` + ``client_meta`` (§3.4).

        ``None`` scalars become empty strings so a bare ``{{field}}`` renders nothing
        (not the literal ``"None"``) and a ``{{#field}}`` section stays falsy.
        """
        fallback = client_meta.get("default_language")
        return {
            "gtin": product.gtin,
            "gtin14": product.gtin14,
            "brand": product.brand,
            "gpc_brick_code": _s(product.gpc_brick_code),
            "net_content": _s(
                decode_net_content(product.net_content, language, fallback_language=fallback)
            ),
            "image_url": _s(product.image_url),
            "category": _s(product.category),
            "product_name": _s(product.product_name.get(language, fallback)),
            "description_short": _s(_localised(product.description_short, language, fallback)),
            "description_long": _s(_localised(product.description_long, language, fallback)),
            # Resolved for this page's language, with the default-language fallback every other
            # localised field here already uses: a template renders one blob, and a hole in it
            # reads as a broken page rather than as an omitted field.
            "extras": product.extras_for(language, fallback),
            "language": language,
            "client": {
                "display_name": _s(client_meta.get("display_name")),
                "id": client_meta.get("id", self._client_id),
            },
        }

    def _warn_missing_extras(
        self,
        source: str,
        product: ProductRecord,
        template_path: Path,
    ) -> None:
        """Warn once per missing ``{{extras.<name>}}`` reference (edge E12)."""
        for match in _EXTRAS_TAG_RE.finditer(source):
            name = match.group(1)
            if name in product.extras or name in product.extras_localised:
                continue
            key = (str(template_path), name)
            if key in self._warned_extras:
                continue
            self._warned_extras.add(key)
            _log.warning(
                "template %s references unknown extra %r (rendering empty)",
                template_path,
                name,
            )


def _localised(text: Any, language: str, fallback: str | None) -> str | None:
    """Resolve an optional :class:`~lib.records.LocalisedText` to a plain string."""
    if text is None:
        return None
    resolved: str | None = text.get(language, fallback)
    return resolved


def _s(value: str | None) -> str:
    """Coerce ``None`` to an empty string; leave other strings unchanged."""
    return "" if value is None else value
