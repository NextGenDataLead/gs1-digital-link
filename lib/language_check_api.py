"""Run the cross-language check (:mod:`lib.language_check`) through the headless API backend.

Its own module because both ``scripts.check_languages`` and ``scripts.run_generate`` run it, and
:mod:`lib.language_check` cannot import :mod:`lib.llm` (which imports it) without a cycle. One
function, so the two entry points cannot judge different sets of products.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.errors import ConfigError
from lib.language_check import CheckRun, check_and_save
from lib.llm import AnthropicClient
from lib.preflight import in_scope

if TYPE_CHECKING:
    from pathlib import Path

    from lib.config import ClientConfig
    from lib.records import ProductRecord


def run_check(
    cfg: ClientConfig, products: list[ProductRecord], path: Path, *, whole_export: bool = False
) -> CheckRun:
    """Judge the in-scope ``products`` whose values are new or changed, and write the check file.

    ``whole_export`` judges every product in ``products`` instead: the complete report also covers
    live products that are no longer on the product list, and this is how they get a judgment.

    Raises:
        ConfigError: The client has no enabled ``generator`` block (it carries the model and key).
        MissingCredentialError: The configured API-key env var is unset.
        LLMAPIError: A call failed or returned no usable answer.
        GeneratorError: An existing check file is unreadable.
    """
    gen = cfg.generator
    if gen is None or not gen.enabled:
        raise ConfigError(
            f"comparing languages needs an enabled `generator` block for {cfg.client_id!r}"
        )
    with AnthropicClient(gen, voice_template="") as client:
        scope = products if whole_export else in_scope(cfg, products)
        return check_and_save(cfg, scope, path, client, f"api:{gen.model}")
