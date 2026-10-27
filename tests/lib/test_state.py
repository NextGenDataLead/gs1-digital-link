"""Unit tests for lib/state.py (IMPLEMENTATION_SPEC §4.8, §12 Phase 6/7).

Covers the round-trip, the atomic-write / kill-mid-write no-corruption guarantee
(the Phase 6 DoD atomicity item), content-hash determinism, E19 corrupt-file recovery
(quarantine + reset, vs. the raise an unreadable file still gets), and the change
classification `run_plan` builds its plan from.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.config import WordPressConfig
from lib.errors import ConfigError, StateError
from lib.gdsn import GdsnSource
from lib.media_video import VideoGate, VideoMap, VideoMapEntry
from lib.records import (
    LocalisedText,
    PlanClassification,
    ProductRecord,
    SkipReason,
    State,
    StateEntry,
)
from lib.state import (
    classify_units,
    compute_content_hash,
    diff_against_state,
    load_state,
    peek_state,
    save_state,
    state_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GTIN = "02987653007359"
_HASH_LEN = 64


def _product(**overrides: object) -> ProductRecord:
    base: dict[str, object] = {
        "gtin": "02987653007359",
        "brand": "Democlient",
        "product_name": LocalisedText(values={"nl": "Rugsteun", "fr": "Support arrière"}),
    }
    base.update(overrides)
    return ProductRecord(**base)


def _entry(page_id: int = 1) -> StateEntry:
    return StateEntry(
        wp_page_id=page_id,
        wp_url=f"https://democlient.test/p/{page_id}",
        wp_featured_media_id=None,
        content_hash="c" * _HASH_LEN,
        gs1_link_set_hash="g" * _HASH_LEN,
        last_run=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        title="Rugsteun",
    )


# --- load_state / save_state round-trip --------------------------------------


def test_load_state_absent_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    state = load_state("democlient")
    assert state.client_id == "democlient"
    assert state.entries == {}


def test_save_then_load_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    original = State(client_id="democlient", entries={"02987653007359": {"nl": _entry(7)}})

    save_state(original)
    reloaded = load_state("democlient")

    assert reloaded == original
    assert state_path("democlient").is_file()


def test_load_state_without_title_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state file written before titles were persisted still loads (title -> None)."""
    monkeypatch.chdir(tmp_path)
    legacy = _entry(7).model_dump(mode="json")
    del legacy["title"]
    path = state_path("democlient")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"client_id": "democlient", "entries": {"02987653007359": {"nl": legacy}}}),
        encoding="utf-8",
    )

    entry = load_state("democlient").entries["02987653007359"]["nl"]

    assert entry.title is None
    assert entry.wp_page_id == 7


def test_load_state_corrupt_file_is_quarantined_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """E19: a corrupt file is moved aside and the run starts fresh, rather than aborting."""
    monkeypatch.chdir(tmp_path)
    path = state_path("democlient")
    path.parent.mkdir(parents=True)
    path.write_text("{ not valid json", encoding="utf-8")

    with caplog.at_level("ERROR", logger="lib.state"):
        state = load_state("democlient")

    assert state.entries == {}
    assert state.reset_from_corrupt is True  # the caller must surface this (§8.2 summary)
    assert not path.exists()
    # The bad file is preserved, never deleted — it is the only evidence of what went wrong.
    backups = list(path.parent.glob("state.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ not valid json"
    assert "starting fresh" in caplog.text


def test_load_state_schema_violation_is_also_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid JSON that is not a valid State is corrupt too (e.g. an entry missing wp_url)."""
    monkeypatch.chdir(tmp_path)
    path = state_path("democlient")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"client_id": "democlient", "entries": "not-a-dict"}), "utf-8")

    state = load_state("democlient")

    assert state.reset_from_corrupt is True
    assert len(list(path.parent.glob("state.json.corrupt.*"))) == 1


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows chmod cannot make a file unreadable to its owner"
)
def test_load_state_unreadable_file_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable file is an environmental fault, not corruption — continuing would be wrong."""
    monkeypatch.chdir(tmp_path)
    path = state_path("democlient")
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o000)

    try:
        with pytest.raises(StateError, match="cannot read state"):
            load_state("democlient")
    finally:
        path.chmod(0o600)


def test_reset_flag_is_not_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset_from_corrupt`` describes a load, not the state — it must never be written."""
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="democlient", entries={}, reset_from_corrupt=True))

    written = json.loads(state_path("democlient").read_text(encoding="utf-8"))

    assert "reset_from_corrupt" not in written
    assert load_state("democlient").reset_from_corrupt is False


# --- peek_state: the read that must not change anything ----------------------


def test_peek_state_reads_what_load_state_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same parsing. If the two disagreed, a reconciliation would compare the wrong ledger."""
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="democlient", entries={"02987653007359": {"nl": _entry(1)}}))

    assert peek_state("democlient") == load_state("democlient")


def test_peek_state_of_a_missing_file_is_an_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert peek_state("democlient").entries == {}


def test_peek_state_does_not_quarantine_a_corrupt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason this function exists.

    ``load_state`` moves a corrupt file aside (E19) so a *run* can continue, which turns an idle
    read into a change to what the next run does — every published row would re-plan as NEW.
    ``scripts/doctor.py`` avoids state entirely for this reason; a reconciliation cannot, so it
    reads through here instead.
    """
    monkeypatch.chdir(tmp_path)
    path = state_path("democlient")
    path.parent.mkdir(parents=True)
    path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(StateError) as caught:
        peek_state("democlient")

    assert path.exists(), "the file was quarantined by a read that promised not to"
    assert path.read_text(encoding="utf-8") == "{ not valid json"
    assert not list(path.parent.glob("state.json.corrupt.*"))
    assert "left exactly as it is" in str(caught.value)


# --- Atomicity / kill-mid-write (§12 Phase 6 DoD) ----------------------------


def test_save_state_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at the replace step must leave the prior file intact, no stray temp."""
    monkeypatch.chdir(tmp_path)
    save_state(State(client_id="democlient", entries={"02987653007359": {"nl": _entry(1)}}))
    path = state_path("democlient")
    before = path.read_bytes()

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr("lib.state.os.replace", _boom)
    with pytest.raises(StateError):
        save_state(State(client_id="democlient", entries={"99999999": {"nl": _entry(2)}}))

    assert path.read_bytes() == before  # original untouched, not truncated
    assert load_state("democlient").entries.keys() == {"02987653007359"}
    assert not list(path.parent.glob("*.tmp"))  # temp cleaned up


#: Generous: this only has to be longer than a cold interpreter takes to start, import pydantic
#: via lib.records, and write. It is never waited out on a healthy run.
_CHILD_DEADLINE_SECONDS = 60.0
_POLL_SECONDS = 0.01

#: One 3000-entry state is ~1 MB; the child's first write holds a single entry, a few hundred
#: bytes. Anything past this is the large-write loop, whenever we happen to start looking.
_BIG_WRITE_BYTES = 100_000


def _size(path: Path) -> int:
    """Size of ``path``, or 0 while it does not exist yet."""
    return path.stat().st_size if path.is_file() else 0


def _wait_for(proc: subprocess.Popen[bytes], predicate: Callable[[], bool], *, what: str) -> None:
    """Poll ``predicate`` until it holds — failing fast if the child dies first.

    A fixed sleep here would be a wall-clock race against interpreter startup, which is what
    made this test flaky: the same commit passed on ``push`` and failed on ``pull_request``.
    """
    deadline = time.monotonic() + _CHILD_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        assert proc.poll() is None, f"the child exited (rc={proc.returncode}) before {what}"
        time.sleep(_POLL_SECONDS)
    raise AssertionError(f"timed out after {_CHILD_DEADLINE_SECONDS:.0f}s waiting for {what}")


def test_save_state_survives_sigkill_mid_write(tmp_path: Path) -> None:
    """SIGKILL a process hammering save_state; the file must never be corrupt.

    Because save_state writes to a temp file then ``os.replace``s it, the target is
    always either the old or a fully-written new state — never a partial one.

    The kill lands once the child is demonstrably *inside* the loop of large writes, rather
    than after a fixed wait. That is both the point of the test — a kill mid-write — and what
    stops it racing a cold interpreter on a loaded runner.
    """
    child = tmp_path / "hammer.py"
    child.write_text(
        "from datetime import UTC, datetime\n"
        "from lib.records import State, StateEntry\n"
        "from lib.state import save_state\n"
        "def e(i):\n"
        "    return StateEntry(wp_page_id=i, wp_url=f'https://x/{i}', wp_featured_media_id=None,\n"
        "        content_hash='c'*64, gs1_link_set_hash='g'*64, last_run=datetime.now(UTC))\n"
        "save_state(State(client_id='k', entries={'1': {'nl': e(1)}}))\n"
        "big = {str(g): {'nl': e(g)} for g in range(3000)}\n"
        "while True:\n"
        "    save_state(State(client_id='k', entries=big))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    path = tmp_path / "output" / "k" / "state.json"
    proc = subprocess.Popen([sys.executable, str(child)], cwd=tmp_path, env=env)  # noqa: S603
    try:
        _wait_for(proc, path.is_file, what="the child's first write")
        _wait_for(proc, lambda: _size(path) > _BIG_WRITE_BYTES, what="the large writes to begin")
    finally:
        proc.kill()
        proc.wait()

    assert path.is_file()
    parsed = State.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert parsed.client_id == "k"  # loads cleanly: old or new, never corrupt


# --- compute_content_hash ----------------------------------------------------


def test_content_hash_is_deterministic() -> None:
    a = compute_content_hash(_product(), "nl", "https://democlient.test/p/1")
    b = compute_content_hash(_product(), "nl", "https://democlient.test/p/1")
    assert a == b
    assert len(a) == _HASH_LEN
    assert all(c in "0123456789abcdef" for c in a)


@pytest.mark.parametrize(
    ("language", "target_url", "product"),
    [
        ("fr", "https://democlient.test/p/1", _product()),
        ("nl", "https://democlient.test/p/2", _product()),
        ("nl", "https://democlient.test/p/1", _product(brand="Other")),
        # Generated content lives on the record, so filling it reclassifies the row (the
        # merge step relies on this: newly generated copy must not read as UNCHANGED).
        (
            "nl",
            "https://democlient.test/p/1",
            _product(generated_tagline=LocalisedText(values={"nl": "Slogan"})),
        ),
    ],
)
def test_content_hash_sensitive_to_each_input(
    language: str, target_url: str, product: ProductRecord
) -> None:
    baseline = compute_content_hash(_product(), "nl", "https://democlient.test/p/1")
    assert compute_content_hash(product, language, target_url) != baseline


# --- diff_against_state (§4.8, §8.2, Phase 7) --------------------------------


def _gate(
    confirmed: frozenset[str] = frozenset(),
    *,
    clashing: frozenset[str] = frozenset(),
    publish_without_video: bool = False,
    files: dict[str, list[tuple[str, str]]] | None = None,
) -> VideoGate:
    """A gate built by hand, so each test names the one property it is about."""
    by_language = {
        language: [VideoMapEntry(file=file, gtin=gtin) for file, gtin in rows]
        for language, rows in (files or {}).items()
    }
    return VideoGate(confirmed, clashing, publish_without_video, VideoMap(by_language=by_language))


def _wp(**overrides: object) -> WordPressConfig:
    base: dict[str, object] = {
        "site_url": "https://democlient.test",
        "username": "bot",
        "app_password_env": "DEMOCLIENT_WP_APP_PASS",
        "post_type": "democlient",
        "languages": ["nl", "fr"],
        "default_language": "nl",
        "slug_pattern": "p-{gtin}",
        "target_url_pattern": "{site_url}/{lang_segment}{post_type}/{slug}/",
    }
    base.update(overrides)
    return WordPressConfig(**base)


def _row_for(rows: list[object], language: str) -> object:
    return next(r for r in rows if getattr(r, "language") == language)  # noqa: B009


def _state_with(
    gtin: str,
    language: str,
    *,
    content_hash: str,
    wp_url: str,
    title: str | None = "Rugsteun",
) -> State:
    entry = StateEntry(
        wp_page_id=1,
        wp_url=wp_url,
        wp_featured_media_id=None,
        content_hash=content_hash,
        gs1_link_set_hash="g" * _HASH_LEN,
        last_run=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        title=title,
    )
    return State(client_id="democlient", entries={gtin: {language: entry}})


def test_diff_new_when_no_state_entry() -> None:
    rows, _ = diff_against_state(
        [_product()], State(client_id="democlient", entries={}), ["nl"], _wp()
    )

    assert len(rows) == 1
    assert rows[0].classification is PlanClassification.NEW
    assert rows[0].diff is None


def test_diff_slug_and_target_url_built_from_patterns() -> None:
    product = _product(gtin="02987653007359")
    rows, _ = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl", "fr"], _wp()
    )

    nl, fr = _row_for(rows, "nl"), _row_for(rows, "fr")
    assert nl.slug == "p-02987653007359"
    # Default language has no language path segment; a non-default one does.
    assert nl.target_url == "https://democlient.test/democlient/p-02987653007359/"
    assert fr.target_url == "https://democlient.test/fr/democlient/p-02987653007359/"
    assert nl.title == "Rugsteun"
    assert fr.title == "Support arrière"


def test_diff_unchanged_when_hash_matches() -> None:
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(
        product.gtin, "nl", content_hash=baseline.content_hash, wp_url=baseline.target_url
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.UNCHANGED
    assert rows[0].diff is None


def _held_state(product: ProductRecord, baseline_hash: str, url: str, **down: object) -> State:
    """State for a product whose content still matches but which was taken down."""
    state = _state_with(product.gtin, "nl", content_hash=baseline_hash, wp_url=url)
    entry = state.entries[product.gtin]["nl"]
    state.entries[product.gtin]["nl"] = entry.model_copy(update=down)
    return state


@pytest.mark.parametrize(
    ("down", "why"),
    [
        ({"wp_status": "draft"}, "pages drafted"),
        # An interrupted run_unpublish: the resolver is retracted but the pages are still
        # up. Held too — the next run must finish taking it down, not put it back.
        ({"retracted": True}, "resolver retracted, pages still published"),
        ({"wp_status": "draft", "retracted": True}, "fully unpublished"),
    ],
)
def test_diff_held_when_product_was_unpublished(down: dict[str, object], why: str) -> None:
    # The whole point: a held product's content hash still MATCHES, so without the held
    # check this classifies UNCHANGED and the next confirmed run republishes it.
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _held_state(product, baseline.content_hash, baseline.target_url, **down)

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.HELD, why


def test_diff_held_outranks_changed() -> None:
    # Editing an unpublished product's content must not un-hold it: the operator's
    # decision to take it down is about the product, not about that revision of it.
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _held_state(product, "stale", baseline.target_url, wp_status="draft")

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.HELD


def test_a_legacy_gs1_enabled_false_entry_is_still_held() -> None:
    """The migration, and the one place getting this wrong puts a product back on the site.

    ``gs1_enabled`` was renamed to ``retracted`` and inverted. ``StateEntry`` sets no
    ``extra`` policy, so pydantic's default is to **ignore** an unknown key: without a
    translating validator this legacy entry loads with ``retracted`` defaulting to
    ``False``, classifies UNCHANGED, and the next confirmed run republishes something an
    operator deliberately took down — silently, since nothing in the file looks wrong.

    Written against a raw dict rather than ``_entry()``, because the point is a payload no
    current version of the model would ever produce.
    """
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    legacy = _entry().model_dump(mode="json")
    del legacy["retracted"]
    legacy["gs1_enabled"] = False  # the old spelling of retracted=True
    legacy["content_hash"] = baseline.content_hash
    legacy["wp_url"] = baseline.target_url
    entry = StateEntry.model_validate(legacy)
    assert entry.retracted is True

    rows, _ = diff_against_state(
        [product],
        State(client_id="democlient", entries={product.gtin: {"nl": entry}}),
        ["nl"],
        _wp(),
    )

    assert rows[0].classification is PlanClassification.HELD


def test_legacy_state_without_status_fields_is_not_held() -> None:
    # Back-compat: entries written before wp_status/retracted existed default to the
    # published condition. If they defaulted the other way, every pre-existing product
    # in every client's state would silently classify HELD and stop being updated.
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    legacy = _entry().model_dump(mode="json")
    del legacy["wp_status"]
    del legacy["retracted"]
    legacy["content_hash"] = baseline.content_hash
    legacy["wp_url"] = baseline.target_url
    state = State(
        client_id="democlient",
        entries={product.gtin: {"nl": StateEntry.model_validate(legacy)}},
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.UNCHANGED


def test_diff_changed_when_page_published_without_a_resolver_link() -> None:
    # What `run_execute --only pages` leaves behind. Its content hash MATCHES — that is
    # exactly what makes the gap invisible — so without this rule the row classifies
    # UNCHANGED, and a follow-up `/gs1-links` finds nothing to publish and says so cheerfully.
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(
        product.gtin, "nl", content_hash=baseline.content_hash, wp_url=baseline.target_url
    )
    entry = state.entries[product.gtin]["nl"]
    state.entries[product.gtin]["nl"] = entry.model_copy(update={"gs1_link_set_hash": ""})

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.CHANGED
    # A named diff row, so the §10.6.2 prompt says what is missing rather than printing a
    # bare "Changes:" header the operator has to guess at.
    assert rows[0].diff == {"gs1_link": ("not written", "will be written")}


def test_a_pages_only_revive_leaves_a_row_that_still_needs_its_resolver_link() -> None:
    """The defect this rename came with: the revived row read as fully published.

    ``_finish_pages`` carries the **prior** ``gs1_link_set_hash`` forward, and
    ``run_unpublish`` used to leave a real one there — so a ``--revive --only pages`` run
    produced an entry that looked completely linked. Its content hash matches, so the row
    classified UNCHANGED and the resolver record stayed retracted for good, with no gate,
    plan or report saying so. Now the retraction blanks the hash, and the existing
    ``_has_no_resolver_link`` rule does the rest.
    """
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    # After run_unpublish: pages drafted, resolver retracted, link-set hash blanked.
    state = _held_state(
        product,
        baseline.content_hash,
        baseline.target_url,
        wp_status="draft",
        retracted=True,
        gs1_link_set_hash="",
    )
    taken_down = state.entries[product.gtin]["nl"]
    # `--revive --only pages` writes a fresh entry at the published defaults, carrying the
    # prior link-set hash forward — exactly what `_finish_pages` builds.
    state.entries[product.gtin]["nl"] = _entry().model_copy(
        update={
            "content_hash": baseline.content_hash,
            "wp_url": baseline.target_url,
            "gs1_link_set_hash": taken_down.gs1_link_set_hash,
        }
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.CHANGED
    assert rows[0].diff == {"gs1_link": ("not written", "will be written")}


def test_a_links_only_revive_releases_a_product_whose_pages_never_came_down() -> None:
    """``run_unpublish`` retracts before it drafts, so an interrupted one leaves this.

    The product is fully live — page published, resolver record written back and enabled —
    and it used to stay HELD forever, because ``_commit_state`` updated the hash and left
    the retraction flag alone. Every later run dropped it as held, and only ``--revive``
    got it through, each time.
    """
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _held_state(
        product,
        baseline.content_hash,
        baseline.target_url,
        retracted=True,
        gs1_link_set_hash="",
    )
    assert diff_against_state([product], state, ["nl"], _wp()).rows[0].classification is (
        PlanClassification.HELD
    ), "precondition: retracted with its pages still up is held"

    # `--revive --only links`: _commit_state writes the new hash and clears the retraction.
    entry = state.entries[product.gtin]["nl"]
    state.entries[product.gtin]["nl"] = entry.model_copy(
        update={"gs1_link_set_hash": "g" * _HASH_LEN, "retracted": False}
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.UNCHANGED


def test_a_links_only_revive_does_not_release_a_product_whose_pages_are_drafts() -> None:
    """The other half of the OR, and the reason clearing the flag is safe.

    Writing the resolver record back says nothing about the pages. A fully unpublished
    product revived one leg at a time must stay held until the pages are published too,
    or a partial revive would quietly re-enter the ordinary update flow with its pages
    still drafted.
    """
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _held_state(
        product,
        baseline.content_hash,
        baseline.target_url,
        wp_status="draft",
        retracted=True,
        gs1_link_set_hash="",
    )
    entry = state.entries[product.gtin]["nl"]
    state.entries[product.gtin]["nl"] = entry.model_copy(
        update={"gs1_link_set_hash": "g" * _HASH_LEN, "retracted": False}
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.HELD


def test_diff_held_outranks_a_missing_resolver_link() -> None:
    # An unpublished product whose resolver link was never written is still held: intent
    # about the product outranks the fact that half of it was never finished.
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _held_state(
        product, baseline.content_hash, baseline.target_url, wp_status="draft", gs1_link_set_hash=""
    )

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.HELD
    # And it carries no diff. This row is the one place the two facts can contradict each other:
    # the resolver link *is* missing, so the field-level diff would happily say "will be written"
    # — on a row nothing will be written for. The operator reads that diff at the plan gate.
    assert rows[0].diff is None


def test_diff_changed_in_body_only_has_no_diff() -> None:
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    # Title and URL both unmoved, stale content hash -> the change is in the product
    # body, which state does not retain. CHANGED, but no field-level diff to show.
    state = _state_with(product.gtin, "nl", content_hash="stale", wp_url=baseline.target_url)

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.CHANGED
    assert rows[0].diff is None


def test_diff_changed_surfaces_title_when_renamed() -> None:
    # The Phase 7 exit-gate scenario (PROJECT_HANDOVER §8.2): rename a product, re-run,
    # and the CHANGED prompt must say what changed. The slug is GTIN-derived, so the URL
    # does not move and the title is the only thing to show.
    renamed = _product(product_name=LocalisedText(values={"nl": "Rugsteun Pro"}))
    baseline = diff_against_state(
        [renamed], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(
        renamed.gtin, "nl", content_hash="stale", wp_url=baseline.target_url, title="Rugsteun"
    )

    rows, _ = diff_against_state([renamed], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.CHANGED
    assert rows[0].diff == {"title": ("Rugsteun", "Rugsteun Pro")}


def test_diff_changed_surfaces_target_url_when_moved() -> None:
    product = _product()
    baseline = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(product.gtin, "nl", content_hash="stale", wp_url="https://old.test/x/")

    rows, _ = diff_against_state([product], state, ["nl"], _wp())

    assert rows[0].classification is PlanClassification.CHANGED
    assert rows[0].diff == {"target_url": ("https://old.test/x/", baseline.target_url)}


def test_diff_changed_surfaces_title_and_target_url_together() -> None:
    renamed = _product(product_name=LocalisedText(values={"nl": "Rugsteun Pro"}))
    baseline = diff_against_state(
        [renamed], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(
        renamed.gtin, "nl", content_hash="stale", wp_url="https://old.test/x/", title="Rugsteun"
    )

    rows, _ = diff_against_state([renamed], state, ["nl"], _wp())

    # §10.6.2 presents title before target_url.
    assert list(rows[0].diff or {}) == ["title", "target_url"]
    assert rows[0].diff == {
        "title": ("Rugsteun", "Rugsteun Pro"),
        "target_url": ("https://old.test/x/", baseline.target_url),
    }


def test_diff_state_without_recorded_title_omits_title_diff() -> None:
    # State written before titles were persisted: the title is unknown, so it is omitted
    # rather than fabricated. The URL diff still works.
    renamed = _product(product_name=LocalisedText(values={"nl": "Rugsteun Pro"}))
    baseline = diff_against_state(
        [renamed], State(client_id="democlient", entries={}), ["nl"], _wp()
    ).rows[0]
    state = _state_with(
        renamed.gtin, "nl", content_hash="stale", wp_url="https://old.test/x/", title=None
    )

    rows, _ = diff_against_state([renamed], state, ["nl"], _wp())

    assert rows[0].diff == {"target_url": ("https://old.test/x/", baseline.target_url)}


def test_diff_multilanguage_expands_rows() -> None:
    rows, _ = diff_against_state(
        [_product()], State(client_id="democlient", entries={}), ["nl", "fr"], _wp()
    )

    assert {r.language for r in rows} == {"nl", "fr"}


def test_diff_missing_product_name_for_language_is_omitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))  # no fr

    with caplog.at_level("WARNING", logger="lib.state"):
        rows, skipped = diff_against_state(
            [product], State(client_id="democlient", entries={}), ["nl", "fr"], _wp()
        )

    assert [r.language for r in rows] == ["nl"]
    assert "missing product_name.fr" in caplog.text
    # The drop is part of the answer, not just a log line somebody might read.
    assert [(s.gtin, s.language, s.reason) for s in skipped] == [
        (product.gtin, "fr", SkipReason.MISSING_PRODUCT_NAME)
    ]
    assert skipped[0].detail in caplog.text  # the record and the warning say the same thing


def test_a_links_plan_keeps_a_language_with_no_product_name() -> None:
    """Operator, 2026-10-09: a link needs an address, not a name.

    The name is only the link's label in the record, so a missing one borrows the default
    language's name instead of dropping the language — and, with no name at all, the barcode.
    """
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))  # no fr
    nameless = _product(gtin="02987653000010", product_name=LocalisedText(values={}))

    rows, skipped = diff_against_state(
        [product, nameless],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        name_required=False,
    )

    assert skipped == []
    titles = {(r.gtin, r.language): r.title for r in rows}
    assert titles[(product.gtin, "fr")] == "Rugsteun"
    assert titles[(nameless.gtin, "nl")] == nameless.gtin


def test_diff_skips_row_without_generated_copy_when_required(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # E21: generator enabled but this product has no generated tagline → held, not published.
    product = _product()  # no generated_tagline

    with caplog.at_level("WARNING", logger="lib.state"):
        rows, skipped = diff_against_state(
            [product],
            State(client_id="democlient", entries={}),
            ["nl", "fr"],
            _wp(),
            require_generated_copy=True,
        )

    assert rows == []
    assert "no generated copy" in caplog.text
    # An empty plan and a plan with nothing to do are the same document without this.
    assert [(s.language, s.reason) for s in skipped] == [
        ("nl", SkipReason.NO_GENERATED_COPY),
        ("fr", SkipReason.NO_GENERATED_COPY),
    ]


def test_diff_keeps_row_without_generated_copy_when_not_required() -> None:
    # Default (no generator configured): copy-less rows are planned as before.
    product = _product()

    rows, _ = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    )

    assert [r.language for r in rows] == ["nl"]


def test_diff_keeps_row_with_generated_copy_when_required() -> None:
    product = _product(generated_tagline=LocalisedText(values={"nl": "Slogan"}))

    rows, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        require_generated_copy=True,
    )

    # nl has copy → kept; fr lacks copy → skipped.
    assert [r.language for r in rows] == ["nl"]
    assert [(s.language, s.reason) for s in skipped] == [("fr", SkipReason.NO_GENERATED_COPY)]


# --- hash_source: what the classification is allowed to notice ----------------
#
# The content hash covers the product as the feed defined it, plus categories. Values a language
# model produced — generated copy, and a language gap filled by translating the sibling — are
# excluded, because they are not stable across runs: regenerate and the wording moves, so every
# page would reclassify CHANGED and be rewritten with nothing actually changed. Passing the
# pre-generator record as ``hash_source`` is how the caller says which record defines the content.


def _plan_one(
    product: ProductRecord,
    state: State,
    *,
    hash_source: dict[str, ProductRecord] | None = None,
) -> object:
    """One nl row for ``product``, classified against ``state``."""
    rows, _ = diff_against_state([product], state, ["nl"], _wp(), hash_source=hash_source)
    return rows[0]


def _published(row: object, product: ProductRecord) -> State:
    """State recording ``row`` as the live page for ``product``."""
    return _state_with(
        product.gtin,
        "nl",
        content_hash=getattr(row, "content_hash"),  # noqa: B009
        wp_url=getattr(row, "target_url"),  # noqa: B009
    )


def test_regenerated_copy_with_different_wording_stays_unchanged() -> None:
    """The whole point: a fresh generation over unchanged feed data must not republish."""
    feed = _product()
    source = {feed.gtin: feed}
    first = _product(generated_tagline=LocalisedText(values={"nl": "Steunt uw rug"}))
    second = _product(generated_tagline=LocalisedText(values={"nl": "Comfort voor onderweg"}))

    live = _published(
        _plan_one(first, State(client_id="democlient", entries={}), hash_source=source), feed
    )
    row = _plan_one(second, live, hash_source=source)

    assert row.classification is PlanClassification.UNCHANGED
    # And the row still carries the new copy — only the *classification* ignores it.
    assert row.product.generated_tagline is not None
    assert row.product.generated_tagline.values["nl"] == "Comfort voor onderweg"


def test_a_feed_edit_still_reclassifies_changed() -> None:
    before, after = _product(), _product(brand="Ander Merk")
    live = _published(
        _plan_one(
            before, State(client_id="democlient", entries={}), hash_source={before.gtin: before}
        ),
        before,
    )

    row = _plan_one(after, live, hash_source={after.gtin: after})

    assert row.classification is PlanClassification.CHANGED


def test_a_category_change_still_reclassifies_changed() -> None:
    """Categories are assigned before the generator and stay inside the hash."""
    before, after = _product(category="Schoonmaak"), _product(category="Tuin")
    live = _published(
        _plan_one(
            before, State(client_id="democlient", entries={}), hash_source={before.gtin: before}
        ),
        before,
    )

    row = _plan_one(after, live, hash_source={after.gtin: after})

    assert row.classification is PlanClassification.CHANGED


def test_a_translated_language_fill_does_not_move_the_hash() -> None:
    """A filled gap lands on a *feed* field, so only the pre-merge record can exclude it."""
    feed = _product(extras_localised={"material": LocalisedText(values={"nl": "kunststof"})})
    source = {feed.gtin: feed}
    filled = _product(
        extras_localised={"material": LocalisedText(values={"nl": "kunststof", "fr": "plastique"})}
    )

    live = _published(
        _plan_one(feed, State(client_id="democlient", entries={}), hash_source=source), feed
    )
    row = _plan_one(filled, live, hash_source=source)

    assert row.classification is PlanClassification.UNCHANGED


def test_the_row_hash_is_the_hash_of_the_source_record() -> None:
    """What lands in the row — and so in ``state.json`` — is the feed view, not the merged one."""
    feed = _product()
    merged = _product(generated_tagline=LocalisedText(values={"nl": "Slogan"}))

    row = _plan_one(
        merged, State(client_id="democlient", entries={}), hash_source={feed.gtin: feed}
    )

    assert row.content_hash == compute_content_hash(feed, "nl", row.target_url)


def test_without_a_hash_source_the_whole_record_is_hashed() -> None:
    """Back-compat pin: every caller that passes nothing keeps today's behaviour exactly."""
    plain = _product()
    with_copy = _product(generated_tagline=LocalisedText(values={"nl": "Slogan"}))
    empty = State(client_id="democlient", entries={})

    bare, generated = _plan_one(plain, empty), _plan_one(with_copy, empty)

    assert bare.content_hash == compute_content_hash(plain, "nl", bare.target_url)
    assert generated.content_hash != bare.content_hash


def test_a_partial_hash_source_raises_rather_than_hashing_the_enriched_record() -> None:
    """Failing loud is the decision, and it is not free — so it is pinned.

    The lenient spelling (``.get(gtin, product)``) passes every other test in this file, because
    the only caller builds the mapping from the very list it plans. What it would do on the day
    that stops being true is hash the *enriched* record for the one forgotten GTIN — reclassifying
    that row alone and rewriting one live page, silently. A ``KeyError`` names the bug instead.
    """
    planned, forgotten = _product(), _product(gtin="02987653000527")

    with pytest.raises(KeyError):
        diff_against_state(
            [planned, forgotten],
            State(client_id="democlient", entries={}),
            ["nl"],
            _wp(),
            hash_source={planned.gtin: planned},
        )


def test_e21_still_fires_when_a_hash_source_is_given() -> None:
    """Excluding copy from the hash must not disarm the hold that keeps blank pages offline."""
    product = _product()  # no generated_tagline

    rows, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl"],
        _wp(),
        require_generated_copy=True,
        hash_source={product.gtin: product},
    )

    assert rows == []
    assert [(s.language, s.reason) for s in skipped] == [("nl", SkipReason.NO_GENERATED_COPY)]


def test_diff_holds_gtin_with_blank_image_when_required(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # E22: require_hero_image set + blank source image → the whole GTIN is held, not published.
    product = _product(image_url=None)

    with caplog.at_level("WARNING", logger="lib.state"):
        rows, skipped = diff_against_state(
            [product],
            State(client_id="democlient", entries={}),
            ["nl", "fr"],
            _wp(),
            require_hero_image=True,
        )

    assert rows == []
    assert "blank source image" in caplog.text
    # The check is per product, but the record is per language: the plan counts rows in
    # (GTIN, language) units, and a skip counted in any other unit cannot be set beside them.
    assert [(s.language, s.reason) for s in skipped] == [
        ("nl", SkipReason.BLANK_HERO_IMAGE),
        ("fr", SkipReason.BLANK_HERO_IMAGE),
    ]


def test_diff_keeps_gtin_with_blank_image_when_not_required() -> None:
    # Default: a blank image degrades gracefully at execute (E7), so the plan still includes it.
    product = _product(image_url=None)

    rows, _ = diff_against_state(
        [product], State(client_id="democlient", entries={}), ["nl"], _wp()
    )

    assert [r.language for r in rows] == ["nl"]


def test_diff_keeps_gtin_with_hero_image_when_required() -> None:
    product = _product(image_url="https://example.test/hero.jpg")

    rows, _ = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl"],
        _wp(),
        require_hero_image=True,
    )

    assert [r.language for r in rows] == ["nl"]


def _required(**sources: object) -> dict[str, GdsnSource]:
    return {name: source for name, source in sources.items() if isinstance(source, GdsnSource)}


def test_diff_holds_the_whole_sku_when_a_mandatory_field_is_missing() -> None:
    """E23 is per product, in every language: a half-published SKU reads as success."""
    gdsn_map = _required(
        product_name=GdsnSource(sheet="S", attribute="3301", localised=True, required=True)
    )
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))  # no fr

    rows, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        mandatory_sources=gdsn_map,
    )

    assert rows == []  # nl is held too, though nl itself is complete
    assert [(s.language, s.reason) for s in skipped] == [
        ("nl", SkipReason.MISSING_MANDATORY_FIELD),
        ("fr", SkipReason.MISSING_MANDATORY_FIELD),
    ]
    assert "product_name.fr (attr 3301)" in skipped[0].detail


def test_a_required_extra_holds_the_sku_here_too_not_only_in_the_report() -> None:
    """The plan and the coverage matrix must agree about which fields hold a SKU.

    They could not before: this took the `gdsn_map` alone, so a `gdsn_extras` entry marked
    `required` was honoured by neither — and once the matrix started reading both maps, a plan
    still reading one would have published a SKU the report was calling held. The parameter is
    named `mandatory_sources` rather than `gdsn_map` so that a caller has to notice which it is
    passing; both real callers hand over `ExportConfig.all_sources`.
    """
    sources = _required(dim_height=GdsnSource(sheet="S", attribute="3498", required=True))

    rows, skipped = diff_against_state(
        [_product()],  # carries no dim_height extra
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        mandatory_sources=sources,
    )

    assert rows == []
    assert [s.reason for s in skipped] == [SkipReason.MISSING_MANDATORY_FIELD] * 2
    assert "dim_height (attr 3498)" in skipped[0].detail


def test_diff_publishes_when_every_mandatory_field_is_present() -> None:
    gdsn_map = _required(
        product_name=GdsnSource(sheet="S", attribute="3301", localised=True, required=True)
    )

    rows, skipped = diff_against_state(
        [_product()],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        mandatory_sources=gdsn_map,
    )

    assert [r.language for r in rows] == ["nl", "fr"]
    assert skipped == []


def test_diff_holds_the_whole_sku_without_a_confirmed_video() -> None:
    rows, skipped = diff_against_state(
        [_product()],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        video_gate=_gate(frozenset({"02987653000000"})),  # some other GTIN
    )

    assert rows == []
    assert {s.reason for s in skipped} == {SkipReason.NO_CONFIRMED_VIDEO}
    assert {s.language for s in skipped} == {"nl", "fr"}


def test_diff_publishes_when_the_video_is_confirmed() -> None:
    rows, skipped = diff_against_state(
        [_product()],
        State(client_id="democlient", entries={}),
        ["nl"],
        _wp(),
        video_gate=_gate(frozenset({"02987653007359"})),
    )

    assert [r.language for r in rows] == ["nl"]
    assert skipped == []


def test_no_video_gate_means_no_video_hold() -> None:
    """``None`` disables E24; a closed gate would hold every product, which is a different thing."""
    rows, _ = diff_against_state(
        [_product()], State(client_id="democlient", entries={}), ["nl"], _wp(), video_gate=None
    )

    assert [r.language for r in rows] == ["nl"]


def test_missing_data_is_reported_ahead_of_a_missing_video() -> None:
    """One SKU, one reason: E23 runs first, so the operator is not sent to fix two things."""
    gdsn_map = _required(
        product_name=GdsnSource(sheet="S", attribute="3301", localised=True, required=True)
    )
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))

    _, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        mandatory_sources=gdsn_map,
        video_gate=_gate(),  # also has no video
    )

    assert {s.reason for s in skipped} == {SkipReason.MISSING_MANDATORY_FIELD}


def test_publish_without_video_admits_a_product_with_no_confirmed_video() -> None:
    rows, skipped = diff_against_state(
        [_product()],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        video_gate=_gate(publish_without_video=True),
    )

    assert [r.language for r in rows] == ["nl", "fr"]
    assert skipped == []


def test_two_confirmed_videos_in_one_language_hold_even_when_publishing_without_video() -> None:
    """The client must pick one: the tool will not, and a page cannot show both."""
    rows, skipped = diff_against_state(
        [_product()],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        video_gate=_gate(clashing=frozenset({_GTIN}), publish_without_video=True),
    )

    assert rows == []
    assert {s.reason for s in skipped} == {SkipReason.NO_CONFIRMED_VIDEO}
    assert all("two confirmed videos" in s.detail for s in skipped)


def _live_with_video(video_file: str | None) -> State:
    """``_GTIN``'s nl page as published from the current product, recording ``video_file``."""
    (row,), _ = diff_against_state(
        [_product()], State(client_id="democlient", entries={}), ["nl"], _wp()
    )
    entry = _entry().model_copy(
        update={
            "content_hash": row.content_hash,
            "wp_url": row.target_url,
            "video_file": video_file,
        }
    )
    return State(client_id="democlient", entries={_GTIN: {"nl": entry}})


_ARRIVED = {"nl": [("Rugsteun.mp4", _GTIN)]}


def test_a_video_confirmed_after_a_page_went_live_without_one_reclassifies_it_changed() -> None:
    """The content hash does not cover the video, so without this the page never gets it."""
    gate = _gate(publish_without_video=True, files=_ARRIVED)

    (row,), _ = diff_against_state(
        [_product()], _live_with_video(""), ["nl"], _wp(), video_gate=gate
    )

    assert row.classification is PlanClassification.CHANGED
    assert row.diff == {"video": ("none", "Rugsteun.mp4")}


def test_the_same_video_as_recorded_leaves_the_page_unchanged() -> None:
    gate = _gate(publish_without_video=True, files=_ARRIVED)

    (row,), _ = diff_against_state(
        [_product()], _live_with_video("Rugsteun.mp4"), ["nl"], _wp(), video_gate=gate
    )

    assert row.classification is PlanClassification.UNCHANGED


def test_an_entry_that_never_recorded_its_video_is_not_reclassified() -> None:
    """Every page published before the field existed was published under the rule needing one."""
    gate = _gate(publish_without_video=True, files=_ARRIVED)

    (row,), _ = diff_against_state(
        [_product()], _live_with_video(None), ["nl"], _wp(), video_gate=gate
    )

    assert row.classification is PlanClassification.UNCHANGED


def test_a_video_leaving_the_mapping_does_not_reclassify_the_page() -> None:
    """The write path cannot take a video off a page, so re-running it would record a lie."""
    gate = _gate(publish_without_video=True)

    (row,), _ = diff_against_state(
        [_product()], _live_with_video("Rugsteun.mp4"), ["nl"], _wp(), video_gate=gate
    )

    assert row.classification is PlanClassification.UNCHANGED


def test_a_file_that_failed_to_prepare_is_not_retried_every_run() -> None:
    """Otherwise each run rewrites the live page and its GS1 record and fails the same way."""
    gate = _gate(publish_without_video=True, files=_ARRIVED)
    state = _live_with_video("")
    entry = state.entries[_GTIN]["nl"].model_copy(update={"video_failed": "Rugsteun.mp4"})
    state = State(client_id="democlient", entries={_GTIN: {"nl": entry}})

    (row,), _ = diff_against_state([_product()], state, ["nl"], _wp(), video_gate=gate)

    assert row.classification is PlanClassification.UNCHANGED


def _failed(retry: bool | None) -> State:
    """``_GTIN``'s nl page, published bare because ``Rugsteun.mp4`` would not prepare."""
    entry = (
        _live_with_video("")
        .entries[_GTIN]["nl"]
        .model_copy(update={"video_failed": "Rugsteun.mp4", "video_failed_retry": retry})
    )
    return State(client_id="democlient", entries={_GTIN: {"nl": entry}})


def _gate_with_file_on_disk(tmp_path: Path) -> VideoGate:
    (tmp_path / "Rugsteun.mp4").write_bytes(b"video")
    return VideoGate(
        frozenset(),
        frozenset(),
        True,
        VideoMap(by_language={"nl": [VideoMapEntry(file="Rugsteun.mp4", gtin=_GTIN)]}),
        folders=(("nl", str(tmp_path)),),
    )


@pytest.mark.parametrize("retry", [True, None])
def test_a_failed_file_now_in_its_folder_is_retried(tmp_path: Path, retry: bool | None) -> None:
    """Not in its folder (or no ffmpeg) is this machine's fault; ``None`` predates the field."""
    gate = _gate_with_file_on_disk(tmp_path)

    (row,), _ = diff_against_state([_product()], _failed(retry), ["nl"], _wp(), video_gate=gate)

    assert row.classification is PlanClassification.CHANGED
    assert row.diff == {"video": ("none", "Rugsteun.mp4")}


def test_a_file_ffmpeg_refused_is_not_retried_even_in_its_folder(tmp_path: Path) -> None:
    """It would fail the same way and rewrite the live page and its GS1 record on every run."""
    gate = _gate_with_file_on_disk(tmp_path)

    (row,), _ = diff_against_state([_product()], _failed(False), ["nl"], _wp(), video_gate=gate)

    assert row.classification is PlanClassification.UNCHANGED


def test_an_unenforced_gate_holds_nothing_but_still_sees_an_arrival() -> None:
    """A client without restrict_to_mapped_gtins still gets a later video onto the page."""
    gate = VideoGate(
        frozenset(),
        frozenset(),
        False,
        VideoMap(by_language={"nl": [VideoMapEntry(file="Rugsteun.mp4", gtin=_GTIN)]}),
        enforced=False,
    )

    (row,), skipped = diff_against_state(
        [_product()], _live_with_video(""), ["nl"], _wp(), video_gate=gate
    )

    assert skipped == []
    assert row.classification is PlanClassification.CHANGED


def test_classify_units_sees_an_arriving_video_too() -> None:
    """``run_generate`` asks this to decide which units need copy; a CHANGED row without copy
    would be dropped by E21 and the video would never land."""
    gate = _gate(publish_without_video=True, files=_ARRIVED)

    classified = classify_units([_product()], _live_with_video(""), ["nl"], _wp(), video_gate=gate)

    assert classified[(_GTIN, "nl")] is PlanClassification.CHANGED


def test_diff_empty_products_yields_no_rows() -> None:
    rows, skipped = diff_against_state([], State(client_id="democlient", entries={}), ["nl"], _wp())
    assert rows == []
    assert skipped == []  # nothing came in, so nothing was dropped — a different empty plan


def test_diff_missing_patterns_raises() -> None:
    with pytest.raises(ConfigError, match="slug_pattern"):
        diff_against_state(
            [_product()],
            State(client_id="democlient", entries={}),
            ["nl"],
            _wp(slug_pattern=None),
        )


# --- E21 after classification: copy is written for CREATE/CHANGED rows only ---
#
# Copy is generated per run for the rows a run will actually execute — NEW and CHANGED — so an
# UNCHANGED unit legitimately arrives with none. E21 asks "was this unit supposed to have copy?",
# and only a row that is going to be written was. Asked before the classification, as it used to
# be, it cannot tell the two apart and reports every already-live page as work.


def _live(product: ProductRecord, language: str = "nl") -> State:
    """State recording ``product`` as published and matching, for ``language``."""
    baseline = _plan_one(product, State(client_id="democlient", entries={}))
    return _state_with(
        product.gtin,
        language,
        content_hash=getattr(baseline, "content_hash"),  # noqa: B009
        wp_url=getattr(baseline, "target_url"),  # noqa: B009
    )


def test_an_unchanged_unit_without_copy_keeps_its_row() -> None:
    """The change PR 3 turns on: already live, nothing to publish, so no copy was written."""
    product = _product()  # no generated_tagline

    rows, skipped = diff_against_state(
        [product],
        _live(product),
        ["nl"],
        _wp(),
        require_generated_copy=True,
        hash_source={product.gtin: product},
    )

    assert [r.classification for r in rows] == [PlanClassification.UNCHANGED]
    # Not a skip: reporting a correctly-skipped page as a work item is what this replaces.
    assert skipped == []


def test_a_new_unit_without_copy_is_still_held() -> None:
    product = _product()

    rows, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl"],
        _wp(),
        require_generated_copy=True,
    )

    assert rows == []
    assert [(s.language, s.reason) for s in skipped] == [("nl", SkipReason.NO_GENERATED_COPY)]


def test_a_changed_live_page_without_copy_is_written_keeping_its_text() -> None:
    """Copy is optional for a page already live (operator, 2026-10-22).

    A new title on a published product goes out without new copy; the row says it keeps the live
    text, and the payload then carries no copy field, so the page's tagline stays as it is.
    """
    before, after = _product(), _product(brand="Ander Merk")

    rows, skipped = diff_against_state(
        [after],
        _live(before),
        ["nl"],
        _wp(),
        require_generated_copy=True,
        hash_source={after.gtin: after},
    )

    assert skipped == []
    assert [(r.classification, r.keeps_live_copy) for r in rows] == [
        (PlanClassification.CHANGED, True)
    ]


def test_a_changed_page_with_copy_does_not_claim_to_keep_its_text() -> None:
    before = _product()
    after = _product(
        brand="Ander Merk", generated_tagline=LocalisedText(values={"nl": "Nieuwe tekst"})
    )

    rows, _ = diff_against_state(
        [after],
        _live(before),
        ["nl"],
        _wp(),
        require_generated_copy=True,
        hash_source={after.gtin: _product(brand="Ander Merk")},
    )

    assert [r.keeps_live_copy for r in rows] == [False]


def test_a_held_unit_without_copy_is_held_rather_than_skipped() -> None:
    """HELD outranks E21 now that E21 is asked after the classification, and should.

    A product somebody took down is not waiting for copy — it is waiting for a decision. Reported
    as ``no_generated_copy`` it read as a generator failure and sent the operator to re-run
    generation, which would change nothing.
    """
    product = _product()
    state = _live(product)
    entry = state.entries[product.gtin]["nl"]
    state.entries[product.gtin]["nl"] = entry.model_copy(update={"wp_status": "draft"})

    rows, skipped = diff_against_state(
        [product],
        state,
        ["nl"],
        _wp(),
        require_generated_copy=True,
        hash_source={product.gtin: product},
    )

    assert [r.classification for r in rows] == [PlanClassification.HELD]
    assert skipped == []


def test_e18_still_runs_before_the_classification() -> None:
    """Unmoved: a row with no title cannot be built at all, whatever it would classify as."""
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))  # no fr

    rows, skipped = diff_against_state(
        [product],
        State(client_id="democlient", entries={}),
        ["nl", "fr"],
        _wp(),
        require_generated_copy=True,
    )

    assert rows == []
    # fr is dropped for the missing name, not for the copy it was never going to be asked for
    # (nl comes first: the skips follow the language order, not the rule order).
    assert [(s.language, s.reason) for s in skipped] == [
        ("nl", SkipReason.NO_GENERATED_COPY),
        ("fr", SkipReason.MISSING_PRODUCT_NAME),
    ]


# --- classify_units: the same classification, with no skip rules applied ------


def test_classify_units_agrees_with_diff_against_state() -> None:
    """The pin that stops the two paths drifting: generation scope and the plan must match.

    ``run_generate`` narrows to NEW/CHANGED using ``classify_units``; ``run_plan`` classifies with
    ``diff_against_state``. A disagreement generates copy for the wrong units, and the symptom —
    a plan row with no copy — looks like a producer failure rather than a classification one.
    """
    live, fresh = _product(), _product(gtin="02987653000527")
    state = _live(live)

    rows, _ = diff_against_state([live, fresh], state, ["nl"], _wp())
    classified = classify_units([live, fresh], state, ["nl"], _wp())

    assert classified == {(r.gtin, r.language): r.classification for r in rows}
    assert classified[(live.gtin, "nl")] is PlanClassification.UNCHANGED
    assert classified[(fresh.gtin, "nl")] is PlanClassification.NEW


def test_classify_units_applies_no_skip_rules() -> None:
    """It answers "what would this run publish", which every skip rule is downstream of.

    E18 in particular: the one unit whose French name exists only once the producer translates it
    would be dropped here, and then never generated for — the gap closing itself out of existence.
    """
    product = _product(product_name=LocalisedText(values={"nl": "Rugsteun"}))  # no fr, no copy

    classified = classify_units(
        [product], State(client_id="democlient", entries={}), ["nl", "fr"], _wp()
    )

    assert classified == {
        (product.gtin, "nl"): PlanClassification.NEW,
        (product.gtin, "fr"): PlanClassification.NEW,
    }


def test_classify_units_missing_patterns_raises() -> None:
    with pytest.raises(ConfigError, match="slug_pattern"):
        classify_units(
            [_product()],
            State(client_id="democlient", entries={}),
            ["nl"],
            _wp(slug_pattern=None),
        )
