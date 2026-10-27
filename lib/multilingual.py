"""Multilingual plugin adapters for the WordPress client (§4.5).

WordPress sites localise content through one of several plugins. The orchestrator
publishes one page per (GTIN, language) and then links those pages as translations of
one another; *how* that link is expressed is plugin-specific. This module isolates that
difference behind :class:`MultilingualAdapter` so ``lib.wp_client.WordPressClient`` stays
plugin-agnostic.

v0.1.0 supports Polylang, WPML, and a no-op for single-language sites.

WPML exposes no core REST route for language assignment or translation linking, so
:class:`WPMLAdapter` calls a small site-side helper (a Code Snippet / mu-plugin) that wraps
WPML's PHP API. That route is deliberately shaped like Polylang's ``/pll/v1/translations``
so both adapters stay symmetric. See ``docs/clients/democlient-page-adapter.md`` §7 for the
helper's source and the live verification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, Literal

from lib.errors import ConfigError, WordPressAPIError

if TYPE_CHECKING:
    from lib.wp_client import WordPressClient

#: Polylang REST endpoint that groups per-language posts into one translation set.
_PLL_LINK_PATH: Final = "/wp-json/pll/v1/translations"


class MultilingualAdapter(ABC):
    """Strategy for linking per-language pages as translations (§4.5)."""

    @abstractmethod
    def link_translations(self, wp: WordPressClient, translations: dict[str, int]) -> None:
        """Link the given ``{language: page_id}`` pages as translations of each other.

        Args:
            wp: The WordPress client whose authenticated transport to reuse.
            translations: Mapping of language code to the page id in that language.
        """


class NoOpAdapter(MultilingualAdapter):
    """Adapter for sites with no multilingual plugin — linking is a no-op (§4.5)."""

    def link_translations(self, wp: WordPressClient, translations: dict[str, int]) -> None:
        """Do nothing: single-language sites have no translation groups."""
        return


class PolylangAdapter(MultilingualAdapter):
    """Adapter for Polylang, using its ``/wp-json/pll/v1/`` REST endpoints (§4.5)."""

    def link_translations(self, wp: WordPressClient, translations: dict[str, int]) -> None:
        """Register ``translations`` as one Polylang translation group.

        Args:
            wp: The WordPress client whose authenticated transport to reuse.
            translations: Mapping of language code to the page id in that language.
        """
        if len(translations) < 2:  # noqa: PLR2004 — nothing to link with under two languages
            return
        wp._request(  # noqa: SLF001 — adapters intentionally reuse the client's transport
            "POST",
            _PLL_LINK_PATH,
            json_body={"translations": translations},
            label=f"pll link {sorted(translations)}",
        )


class WPMLAdapter(MultilingualAdapter):
    """Adapter for WPML, via a site-side helper route (§4.5).

    WPML publishes no core REST route for assigning a post's language or linking a set of
    posts as one translation group — both need its PHP API. So the site hosts a small helper
    (a Code Snippet / mu-plugin; source and live verification in
    ``docs/clients/democlient-page-adapter.md`` §7) exposing one route shaped like Polylang's::

        POST {helper_path}
        {"translations": {"nl": 123, "fr": 456}, "source_language": "nl"}
        -> {"ok": true, "trid": 42, "translations": {"nl": 123, "fr": 456}}

    The response's ``translations`` is read back from WPML's own tables rather than echoed,
    so this adapter **asserts it matches what was sent**. A silent no-op — the failure mode
    this integration is most prone to — then surfaces here rather than as a page that looks
    published but is unreachable in its own language.

    Args:
        helper_path: Path to the site's helper route (``wordpress.wpml_helper_path``).
        source_language: The language the others are translations *of*; WPML needs a source
            to hang the translation group (``trid``) off. Normally
            ``wordpress.default_language``.
    """

    def __init__(self, helper_path: str, source_language: str) -> None:
        self._helper_path = helper_path
        self._source_language = source_language

    def link_translations(self, wp: WordPressClient, translations: dict[str, int]) -> None:
        """Assign languages and link ``translations`` as one WPML translation group.

        Args:
            wp: The WordPress client whose authenticated transport to reuse.
            translations: Mapping of language code to the page id in that language.

        Raises:
            ConfigError: ``source_language`` is absent from ``translations`` — WPML cannot
                form a group without its source.
            WordPressAPIError: The helper is unreachable or errored, or the group it reports
                back differs from the one requested (WPML did not apply the link).
        """
        if len(translations) < 2:  # noqa: PLR2004 — nothing to link with under two languages
            return
        if self._source_language not in translations:
            raise ConfigError(
                f"WPML source language {self._source_language!r} is not among the linked "
                f"languages {sorted(translations)}; cannot form a translation group"
            )
        label = f"wpml link {sorted(translations)}"
        resp = wp._request(  # noqa: SLF001 — adapters intentionally reuse the client's transport
            "POST",
            self._helper_path,
            json_body={
                "translations": translations,
                "source_language": self._source_language,
            },
            label=label,
        )
        self._assert_linked(resp.json(), translations, call=f"POST {self._helper_path} ({label})")

    @staticmethod
    def _assert_linked(body: object, requested: dict[str, int], *, call: str) -> None:
        """Fail unless the helper reports back exactly the group that was requested."""
        linked: dict[str, int] = {}
        if isinstance(body, dict):
            raw = body.get("translations")
            if isinstance(raw, dict):
                linked = {str(k): int(v) for k, v in raw.items()}
        if linked != requested:
            raise WordPressAPIError(
                int(HTTPStatus.CONFLICT),
                f"WPML helper reported translation group {linked or '(none)'}, expected "
                f"{requested} — the link was not applied",
                call=call,
            )


def make_adapter(
    plugin: Literal["polylang", "wpml", "none"],
    *,
    wpml_helper_path: str | None = None,
    source_language: str | None = None,
) -> MultilingualAdapter:
    """Return the :class:`MultilingualAdapter` for a detected plugin (§4.5).

    Args:
        plugin: The multilingual plugin identifier.
        wpml_helper_path: Path to the site's WPML helper route. Required for ``wpml``.
        source_language: The source language for WPML translation groups. Required for
            ``wpml``.

    Returns:
        The matching adapter instance.

    Raises:
        ConfigError: ``plugin`` is ``wpml`` without a helper path and source language.
    """
    if plugin == "polylang":
        return PolylangAdapter()
    if plugin == "wpml":
        if not wpml_helper_path or not source_language:
            raise ConfigError(
                "wpml requires wordpress.wpml_helper_path and wordpress.default_language"
            )
        return WPMLAdapter(wpml_helper_path, source_language)
    return NoOpAdapter()
