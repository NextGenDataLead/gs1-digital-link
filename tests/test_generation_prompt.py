"""Guard the generation voice template against the active prompt version.

The producer (the ``content-generator`` skill now, ``lib.llm`` later) loads
``prompts/{client}/generation.{prompt_version}.md`` — its few-shot examples *are* the frozen voice
for that ``prompt_version``. This test ensures the Democlient template for the active
``DEFAULT_PROMPT_VERSION`` exists and is stamped with a matching version, so a future version bump
cannot silently ship without its voice file.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib.generator import DEFAULT_PROMPT_VERSION

_ROOT = Path(__file__).resolve().parent.parent
_PROMPT = _ROOT / "prompts" / "democlient" / f"generation.{DEFAULT_PROMPT_VERSION}.md"


def test_democlient_voice_template_exists_for_active_version() -> None:
    assert _PROMPT.is_file(), f"no voice template for prompt_version {DEFAULT_PROMPT_VERSION!r}"
    assert _PROMPT.read_text(encoding="utf-8").strip(), "voice template is empty"


def test_voice_template_version_stamp_matches() -> None:
    text = _PROMPT.read_text(encoding="utf-8")
    match = re.search(r"prompt_version:\s*(\S+)", text)
    assert match is not None, "voice template carries no `prompt_version:` stamp"
    assert match.group(1) == DEFAULT_PROMPT_VERSION
