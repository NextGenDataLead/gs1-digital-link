"""Video → GTIN mapping and web-prep for the Democlient pilot (Phase 9.5 media).

Videos are **not** in the GDSN feed (all 375 feed media are ``PRODUCT_IMAGE``). The operator
supplies two local folders (nl, fr) whose files are named by **English marketing name**
(``PipeSticks_NL.mpeg``, ``FlyCatch_NL_Small.mpg``) — names that mostly appear nowhere in the
feed, so the tool cannot reliably auto-match them to GTINs. The mapping is therefore
**operator-authored**: this module only

* normalizes a filename to a display token (:func:`normalize_video_name`) used for hints/keying,
* ranks fuzzy feed candidates as *hints* for the human (:func:`rank_candidates`),
* loads and validates the human-confirmed ``mapping.yml`` (:class:`VideoMap`,
  :func:`check_video_map`), and
* prepares a matched file for the web (:func:`prepare_video`) — an optional ffmpeg transcode,
  since the source ``.mpg``/``.mpeg`` (MPEG-1/2) will not play in an HTML5 ``<video>``.

Like :mod:`lib.categories`, it *reports rather than guesses*: an unconfirmed or ambiguous entry
is surfaced as a :class:`~lib.records.SourceIssue`, never uploaded.
"""

from __future__ import annotations

import logging
import re
import subprocess  # noqa: S404 — ffmpeg is invoked with a fixed, non-shell argv
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Final, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from lib.errors import VideoMapError
from lib.records import ProductRecord, SourceIssue

_log = logging.getLogger(__name__)

#: Video file extensions recognised in an operator folder.
VIDEO_EXTENSIONS = frozenset({".mpg", ".mpeg", ".mp4", ".mov", ".m4v", ".webm"})
#: Directory names to skip when scanning a folder (macOS/Windows volume cruft).
_IGNORE_DIRS = frozenset({"System Volume Information"})

#: Standalone tokens dropped during normalization (language + size/version markers).
_LANG_TOKENS = frozenset({"nl", "fr", "en", "de"})
_SIZE_TOKENS = frozenset({"small", "large", "medium", "new"})
_VERSION_RE = re.compile(r"^v\d+$")
#: Insert a boundary between a lower/digit and an upper letter, to split camelCase.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Sentinel gtin value meaning "this video intentionally maps to no product" (not a gap).
#: Public because three surfaces classify a gtin cell — the pipeline here, the shell's editor and
#: the candidate report — and a flag that means one thing on one path and something else on
#: another is the bug this repo has now shipped three times. One definition, read by all of them.
SKIP: Final = "skip"
#: The other two answers :func:`state_of` gives.
CONFIRMED: Final = "confirmed"
UNSET: Final = "unset"
#: What a video is mapped to: ``()`` unset, ``("skip",)``, or its sorted canonical GTIN-14s.
Assignment = tuple[str, ...]
#: Canonical GTIN width — GTIN-14, the zero-padded form (matches ``ProductRecord.gtin14``).
_GTIN_WIDTH = 14


def canon_gtin(gtin: str) -> str:
    """Canonicalise a GTIN to its 14-digit form for comparison.

    The mapping file is often keyed with 13-digit GTINs (no indicator digit) while the pipeline
    carries 14-digit GTINs, so a raw ``==`` silently misses. Stripping non-digits and left-padding
    to 14 makes the two forms compare equal — the same canonicalisation the ``process_list`` gate
    uses via :attr:`lib.records.ProductRecord.gtin14`.
    """
    return re.sub(r"\D", "", gtin).zfill(_GTIN_WIDTH)


def state_of(gtin: str) -> str:
    """Classify a mapping row's gtin cell: :data:`UNSET`, :data:`SKIP`, or :data:`CONFIRMED`.

    Whitespace is not a value: a cell holding only spaces is unset, not a GTIN. That distinction
    is not cosmetic — :func:`canon_gtin` turns ``" "`` into ``00000000000000``, which would then
    join the confirmed set as a product that does not exist, and hold nothing while looking like
    a mapping.
    """
    value = gtin.strip()
    if not value:
        return UNSET
    if value.lower() == SKIP:
        return SKIP
    return CONFIRMED


def normalize_video_name(filename: str) -> str:
    """Reduce a video filename to a canonical display token for matching/keying.

    Drops the extension, splits camelCase, replaces separators with spaces, and removes
    standalone language (``nl``/``fr``) and size/version (``small``/``v2``/``new``) markers,
    then lowercases and collapses whitespace. ``BonnetShine_NL_SmallV2.mpg`` → ``bonnet shine``.
    Used only to build hints and to eyeball the draft — never as an authoritative match.
    """
    stem = Path(filename).stem
    spaced = _CAMEL_RE.sub(" ", stem).replace("_", " ").replace("-", " ")
    tokens = [t for t in spaced.split() if t]
    kept = [
        t
        for t in tokens
        if t.lower() not in _LANG_TOKENS
        and t.lower() not in _SIZE_TOKENS
        and not _VERSION_RE.match(t.lower())
    ]
    return " ".join(kept).lower().strip()


def list_video_files(folder: Path) -> list[Path]:
    """Return the video files directly in ``folder``, sorted, skipping cruft.

    Ignores dotfiles and the ``System Volume Information`` directory; non-video files are
    excluded by extension. Non-recursive: the operator's folders are flat.
    """
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.parent.name not in _IGNORE_DIRS
        and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def files_by_language(folders: Mapping[str, str]) -> dict[str, list[str]]:
    """``{language: [filename]}`` for each configured video folder (``media.video_folders``).

    Spelled once. The preflight, two scripts, the report and the shell each listed the folders with
    the same comprehension, and the gap checks they feed compare these names against the mapping —
    so one listing that differed (a case fold, a recursive walk) would invent or hide gaps on one
    surface only.
    """
    return {
        language: [p.name for p in list_video_files(Path(folder))]
        for language, folder in folders.items()
    }


class VideoCandidate(NamedTuple):
    """A ranked fuzzy match of a video name against one feed field — a hint, not a decision."""

    gtin: str
    name: str
    field: str
    score: float


def _candidate_fields(product: ProductRecord) -> list[tuple[str, str]]:
    """Return ``(field_label, value)`` pairs on ``product`` worth matching a video name against.

    ``product_name`` is attr 3301 and carries every language, so it is the substance of the match.
    A ``functional_name`` extra used to be listed here too and added nothing: it was a second
    declaration of that same attribute, so it could only ever repeat what ``product_name`` says.

    The two name extras earn their place for the opposite reason: the video filenames are
    **English** marketing names, and the feed's English is in these two rather than in
    ``product_name`` — where 3301 reads "huisdierspeelgoed" / "Jouets chiens", the French
    ``marketing_name`` reads "Democlient Pet Pal". So every language of each is offered, labelled
    ``extras.{name}.{lang}`` the way ``product_name.{lang}`` is.

    A name the record carries **flat** is still read. Whether a value is per-language is the
    *record's* fact, not the config's: a ``products.json`` written before ``extras_localised``
    existed holds one flat string however the attribute was declared, and ``localised`` is a
    per-field switch in ``clients.yml`` that another client need not set. ``lib.gdsn`` gives each
    name exactly one home, so the two branches cannot double-count.

    Blank values are dropped rather than offered. Two empty strings compare **1.00**, and a video
    named for its language alone normalizes to one — so a language an extra carries blank would
    rank first at a perfect score with no name to show for it.
    """
    pairs: list[tuple[str, str]] = [
        (f"product_name.{lang}", value) for lang, value in product.product_name.values.items()
    ]
    # ``functional_name`` is 3301 where a client titles its pages with 3297
    # (democlient, 2026-10-22).
    for key in ("marketing_name", "logistics_name", "functional_name"):
        localised = product.extras_localised.get(key)
        if localised is None:
            flat = product.extras.get(key)
            if flat:
                pairs.append((f"extras.{key}", flat))
            continue
        pairs.extend(
            (f"extras.{key}.{lang}", value) for lang, value in localised.values.items() if value
        )
    return pairs


def rank_candidates(
    normalized: str, products: list[ProductRecord], *, top_n: int = 3
) -> list[VideoCandidate]:
    """Rank ``products`` against a normalized video name; return the top ``top_n`` hints.

    Scores each product by the best :class:`~difflib.SequenceMatcher` ratio across its
    ``product_name`` and both name extras, in every language each of them carries — see
    :func:`_candidate_fields`. Hints only — the operator decides the actual GTIN.
    """
    scored: list[VideoCandidate] = []
    for product in products:
        best: VideoCandidate | None = None
        for field, value in _candidate_fields(product):
            ratio = SequenceMatcher(None, normalized, normalize_value(value)).ratio()
            if best is None or ratio > best.score:
                best = VideoCandidate(product.gtin, value, field, ratio)
        if best is not None:
            scored.append(best)
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_n]


def normalize_value(value: str) -> str:
    """Normalize a feed string the same way as a video name, for comparable matching."""
    spaced = _CAMEL_RE.sub(" ", value).replace("_", " ").replace("-", " ")
    return " ".join(spaced.split()).lower().strip()


class VideoMapEntry(BaseModel):
    """One row of the confirmed mapping: a filename and the operator-filled GTIN."""

    model_config = ConfigDict(frozen=True)

    file: str
    gtin: str = ""


class VideoMap(BaseModel):
    """The client-confirmed ``{language: [{file, gtin}]}`` video mapping."""

    model_config = ConfigDict(frozen=True)

    by_language: dict[str, list[VideoMapEntry]]

    def resolve(self, gtin: str, language: str) -> str | None:
        """Return the confirmed filename for ``(gtin, language)``, or ``None``.

        ``None`` when the pair is absent, the GTIN is blank/``skip``, or the GTIN is
        confirmed to more than one file in that language (ambiguous — reported elsewhere).
        """
        target = canon_gtin(gtin)
        # Distinct files: one video listed twice under one barcode (13- and 14-digit, say) is still
        # one video, now that a shared video is written one row per barcode.
        matches = {
            e.file
            for e in self.by_language.get(language, [])
            if state_of(e.gtin) == CONFIRMED and canon_gtin(e.gtin) == target
        }
        if len(matches) == 1:
            return matches.pop()
        return None


def assignments(vmap: VideoMap, language: str) -> dict[str, Assignment]:
    """What each video in ``language`` is mapped to, in mapping order. Pure.

    One video may be listed on several rows, one per barcode — that is how a video shared by
    several products is written — and carries every confirmed barcode among them. Every reader that
    asks "which barcodes does this video have?" rather than "which video does this barcode get?"
    goes through here, so a shared video is never counted as several. ``skip`` counts only
    when no row of that video carries a barcode, so one stray ``skip`` row cannot hide a mapping.
    """
    confirmed: dict[str, set[str]] = {}
    skipped: set[str] = set()
    for entry in vmap.by_language.get(language, []):
        state = state_of(entry.gtin)
        confirmed.setdefault(entry.file, set())
        if state == CONFIRMED:
            confirmed[entry.file].add(canon_gtin(entry.gtin))
        elif state == SKIP:
            skipped.add(entry.file)
    return {
        file: tuple(sorted(gtins)) if gtins else ((SKIP,) if file in skipped else ())
        for file, gtins in confirmed.items()
    }


def _confirmed_gtins(entries: list[VideoMapEntry]) -> set[str]:
    """Canonical GTIN-14s confirmed to **exactly one** file in one language.

    Exactly one, not at least one. A GTIN confirmed to two files is ambiguous, and
    :meth:`VideoMap.resolve` attaches neither — so counting it here let a product through the gate
    that then published with no video in that language, reporting success. It was a ``set`` of
    every confirmed cell, which is how two rows collapsed into one "mapped" GTIN unnoticed.
    """
    counts = _files_per_gtin(entries)
    return {gtin for gtin, files in counts.items() if len(files) == 1}


def _files_per_gtin(entries: list[VideoMapEntry]) -> dict[str, set[str]]:
    """``{canonical GTIN: distinct files}`` over one language's confirmed rows.

    Distinct, because a row is not a video: a shared video is one row per barcode, and a hand edit
    can list the same video twice under one barcode in two spellings. Counting rows held that
    product as a two-video clash :meth:`VideoMap.resolve` would not have.
    """
    files: dict[str, set[str]] = {}
    for e in entries:
        if state_of(e.gtin) == CONFIRMED:
            files.setdefault(canon_gtin(e.gtin), set()).add(e.file)
    return files


def fully_mapped_gtins(vmap: VideoMap, languages: list[str]) -> frozenset[str]:
    """Canonical GTIN-14 set of GTINs with exactly one confirmed video in **every** language.

    This is the pilot allowlist: a product is runnable only once it has a client-confirmed video
    in each language, so the page can be published complete in all languages at once. A GTIN
    confirmed to two files in a language is held like one with none: the page could not get a
    video there, and the data-quality report (§1b) names both files so one can be marked ``skip``.
    Equivalently: :meth:`VideoMap.resolve` finds a file in every language — which is what
    ``run_execute`` attaches, so passing the gate and getting a video cannot disagree.
    """
    per_language = [_confirmed_gtins(vmap.by_language.get(lang, [])) for lang in languages]
    if not per_language:
        return frozenset()
    return frozenset(set.intersection(*per_language))


def clashing_gtins(vmap: VideoMap, languages: list[str]) -> frozenset[str]:
    """Canonical GTIN-14s confirmed to two or more files in at least one of ``languages``.

    The complement, within the confirmed cells, of what :func:`_confirmed_gtins` counts: these are
    the products :meth:`VideoMap.resolve` attaches nothing to in that language because it cannot
    tell which file is meant.
    """
    clashing: set[str] = set()
    for language in languages:
        files = _files_per_gtin(vmap.by_language.get(language, []))
        clashing.update(gtin for gtin, names in files.items() if len(names) > 1)
    return frozenset(clashing)


@dataclass(frozen=True)
class VideoGate:
    """What the video rule lets publish, decided once from the mapping for every caller.

    There is one rule and four places that apply it — the plan (E24), ``run_execute``'s hard
    allowlist, the doctor and the Data screen — so it is an object they share rather than a set
    each of them interprets. The set it replaced, :func:`fully_mapped_gtins`, could only say "these
    may run"; once a product with *no* video may run, the allowed set is "everything except the
    clashes", which no finite set of the mapping's GTINs can spell.

    Attributes:
        confirmed: GTINs with exactly one confirmed file in every language.
        clashing: GTINs confirmed to two files in some language — held either way, because a page
            cannot show two videos and the tool will not pick one for the client.
        publish_without_video: ``media.publish_without_video``. When true, a product missing a
            video in some language publishes without one there; when false it is held (the pilot
            rule, unchanged).
        vmap: The mapping itself, so a caller can ask which file a page gets
            (:meth:`file_for`) through the same :meth:`VideoMap.resolve` ``run_execute`` attaches.
        enforced: ``media.restrict_to_mapped_gtins``. When false the gate holds nothing and exists
            only for :meth:`file_for` — a page published without a video still has to get it when
            one is confirmed, whether or not the client restricts runs to mapped products.
        folders: ``media.video_folders`` as ``(language, folder)`` pairs, so :meth:`on_disk` can
            say whether a file is where a run would look for it. Empty: nothing is on disk.
    """

    confirmed: frozenset[str]
    clashing: frozenset[str]
    publish_without_video: bool
    vmap: VideoMap
    enforced: bool = True
    folders: tuple[tuple[str, str], ...] = ()

    def admits(self, gtin: str) -> bool:
        """Whether a product may publish as far as videos are concerned."""
        if not self.enforced:
            return True
        canonical = canon_gtin(gtin)
        if canonical in self.clashing:
            return False
        return self.publish_without_video or canonical in self.confirmed

    def file_for(self, gtin: str, language: str) -> str | None:
        """The file this ``(gtin, language)`` page gets, or ``None`` when it gets none."""
        return self.vmap.resolve(gtin, language)

    def on_disk(self, language: str, file: str | None) -> bool:
        """Whether ``file`` is in ``language``'s video folder, where a run would read it."""
        folder = dict(self.folders).get(language)
        return bool(file and folder) and (Path(folder or "") / (file or "")).is_file()


def video_gate(
    vmap: VideoMap,
    languages: list[str],
    *,
    publish_without_video: bool,
    enforced: bool = True,
    folders: Mapping[str, str] | None = None,
) -> VideoGate:
    """Build the :class:`VideoGate` for ``vmap`` over ``languages``."""
    return VideoGate(
        confirmed=fully_mapped_gtins(vmap, languages),
        clashing=clashing_gtins(vmap, languages),
        publish_without_video=publish_without_video,
        vmap=vmap,
        enforced=enforced,
        folders=tuple(sorted((folders or {}).items())),
    )


#: Admits nothing. What a caller that must fail safe uses when the mapping will not load.
CLOSED_GATE: Final = VideoGate(frozenset(), frozenset(), False, VideoMap(by_language={}))


def load_video_map(path: Path) -> VideoMap:
    """Load and validate the confirmed mapping YAML (``{lang: [{file, gtin}]}``).

    Args:
        path: The ``mapping.yml`` named by ``media.video_map_path``.

    Returns:
        The parsed mapping.

    Raises:
        VideoMapError: The file is missing, unreadable, not valid YAML, or not the expected
            shape. A YAML syntax error carries its line and column through, because the operator
            has to find it in a file they edited by hand.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VideoMapError(f"cannot read the video mapping at {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise VideoMapError(f"{path} is not valid YAML {_yaml_detail(exc)}") from exc

    try:
        return VideoMap.model_validate({"by_language": raw})
    except ValidationError as exc:
        raise VideoMapError(
            f"{path} is valid YAML but not a video mapping — expected "
            f"{{language: [{{file, gtin}}]}} — {_shape_detail(exc)}"
        ) from exc


def _shape_detail(exc: ValidationError) -> str:
    """The first validation failure as one line, located by key path.

    Same reason as :func:`_yaml_detail`: pydantic's own rendering is a paragraph, and this
    message has to survive being put in a table cell.
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return str(exc)
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "the document"
    return f"{where}: {first['msg']} ({len(errors)} problem(s))"


def _yaml_detail(exc: yaml.YAMLError) -> str:
    """A YAML error as one line: where it is, then what it is.

    PyYAML's own ``str()`` is a multi-line block with a caret diagram. That is fine in a terminal
    and wrong everywhere else this message goes — a preflight row, a run log, a notification —
    so the position and the problem are pulled out and the diagram left behind.
    """
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is None or problem is None:  # a YAMLError that carries no mark; keep it on one line
        return f"— {' '.join(str(exc).split())}"

    context = getattr(exc, "context", None)
    what = f"{context}, {problem}" if context else str(problem)
    return f"at line {mark.line + 1}, column {mark.column + 1} — {what}"


@dataclass(frozen=True)
class VideoMapSummary:
    """The mapping's coverage, counted once so every surface reports the same numbers.

    It exists because two of them did not. The preflight compared *every* gap — including one per
    map row when the folders are absent — against the number of files on disk, and printed
    "284 of 0 video file(s) are not yet confirmed": a ratio whose numerator is a different
    quantity from its denominator, on exactly the machine where it is read first. ``build_video_map
    --check`` counted differently again, by entries rather than GTINs.

    Attributes:
        files: Video files found on disk, across every configured language folder.
        entries: Rows in the mapping, across every language.
        unconfirmed: Rows with no GTIN filled in yet.
        ambiguous: GTINs confirmed to more than one file in one language.
        missing_from_map: Files on disk with no row in the mapping.
        file_missing: Rows naming a file that is not on disk.
        confirmed_gtins: GTINs confirmed in **every** requested language — the runnable set.
    """

    files: int
    entries: int
    unconfirmed: int
    ambiguous: int
    missing_from_map: int
    file_missing: int
    confirmed_gtins: int

    @property
    def gaps(self) -> int:
        """Every gap, of any kind."""
        return self.unconfirmed + self.ambiguous + self.missing_from_map + self.file_missing

    @property
    def no_files_found(self) -> bool:
        """Whether the mapping has rows but the folders hold nothing.

        A distinct condition, and a different fix: the mapping arrived and the (multi-gigabyte)
        video library did not. Reporting it as unconfirmed mappings sends the operator to edit a
        file that is already correct.
        """
        return self.files == 0 and self.entries > 0


def summarize_video_map(
    vmap: VideoMap, files_by_language: dict[str, list[str]], languages: list[str]
) -> VideoMapSummary:
    """Count what is mapped, what is missing, and what is runnable.

    Args:
        vmap: The confirmed mapping.
        files_by_language: Filenames found in each configured video folder.
        languages: The languages a product must be confirmed in to be runnable.

    Returns:
        The counts, per gap kind rather than as one total — so no caller has to invent a ratio.
    """
    kinds = Counter(issue.issue for issue in check_video_map(vmap, files_by_language))
    return VideoMapSummary(
        files=sum(len(names) for names in files_by_language.values()),
        entries=sum(len(entries) for entries in vmap.by_language.values()),
        unconfirmed=kinds["video_unconfirmed"],
        ambiguous=kinds["video_ambiguous"],
        missing_from_map=kinds["video_missing_from_map"],
        file_missing=kinds["video_file_missing"],
        confirmed_gtins=len(fully_mapped_gtins(vmap, languages)),
    )


def check_video_map(vmap: VideoMap, files_by_language: dict[str, list[str]]) -> list[SourceIssue]:
    """Return the coverage gaps in ``vmap`` against the actual folder contents.

    Emits one :class:`~lib.records.SourceIssue` per gap: an entry with a blank GTIN
    (``video_unconfirmed``), a GTIN confirmed to two files in one language
    (``video_ambiguous``), a file on disk absent from the map (``video_missing_from_map``),
    or a map entry naming a file not on disk (``video_file_missing``). A ``skip`` GTIN is an
    intentional non-mapping, not a gap. An empty list means the mapping is complete.
    """
    issues: list[SourceIssue] = []
    for language, disk_names in _all_languages(vmap, files_by_language):
        entries = vmap.by_language.get(language, [])
        map_files = {e.file for e in entries}
        disk_files = set(disk_names)

        for entry in entries:
            if state_of(entry.gtin) == UNSET:
                issues.append(
                    _issue(language, entry.file, "video_unconfirmed", "no GTIN filled in yet")
                )

        real = [e for e in entries if state_of(e.gtin) == CONFIRMED]
        # Counted **canonically**, because that is how :meth:`VideoMap.resolve` decides. Counting
        # the raw cells let a GTIN written 13-digit on one row and 14-digit on another map to two
        # files and go unreported: `resolve` canonicalises, finds two and returns ``None``, so the
        # product silently gets no video while the mapping looks complete. Found on the pilot when
        # an import wrote 14-digit values beside hand-typed 13-digit ones — ``Mega Snare.mp4`` and
        # ``Mega Snare.mpg``, the same GTIN in two spellings, both confirmed, neither reported.
        for gtin, names in _files_per_gtin(real).items():
            if len(names) > 1:
                issues.append(
                    SourceIssue(
                        gtin=gtin,
                        field=f"video.{language}",
                        source="operator video folder",
                        issue="video_ambiguous",
                        value=", ".join(sorted(names)),
                        detail=f"GTIN mapped to {len(names)} files in {language}; keep one.",
                    )
                )

        for name in sorted(disk_files - map_files):
            issues.append(
                _issue(language, name, "video_missing_from_map", "file on disk not in the map")
            )
        for name in sorted(map_files - disk_files):
            issues.append(
                _issue(language, name, "video_file_missing", "map names a file not on disk")
            )
    return issues


def _all_languages(
    vmap: VideoMap, files_by_language: dict[str, list[str]]
) -> list[tuple[str, list[str]]]:
    """Every language appearing in the map or the folders, with its disk filenames."""
    languages = sorted(set(vmap.by_language) | set(files_by_language))
    return [(lang, files_by_language.get(lang, [])) for lang in languages]


def _issue(language: str, filename: str, kind: str, detail: str) -> SourceIssue:
    return SourceIssue(
        gtin="",
        field=f"video.{language}",
        source="operator video folder",
        issue=kind,
        value=filename,
        detail=detail,
    )


# --- Web preparation (transcode) ---------------------------------------------

#: Deterministic-ish ffmpeg flags: H.264/AAC MP4, faststart for web, metadata stripped so the
#: output does not carry a wall-clock timestamp that would churn the upload dedupe hash.
_FFMPEG_FLAGS = (
    "-c:v",
    "libx264",
    "-crf",
    "23",
    "-preset",
    "medium",
    "-c:a",
    "aac",
    "-movflags",
    "+faststart",
    "-map_metadata",
    "-1",
)


def prepare_video(
    src: Path, dest_dir: Path, *, transcode: bool, ffmpeg_bin: str = "ffmpeg"
) -> Path | None:
    """Return an upload-ready video path for ``src``.

    With ``transcode=False`` the source is returned unchanged (upload the ``.mpg`` as-is).
    With ``transcode=True`` the file is transcoded to H.264/AAC MP4 at ``dest_dir/{stem}.mp4``;
    an existing destination is reused (idempotency short-circuit, before the upload hash check).
    Returns ``None`` if ffmpeg fails or is missing, so a bad video is skipped and the page still
    publishes. The warning names the reason — ffmpeg not found, or the last line ffmpeg wrote —
    because "transcode failed" alone sent an operator looking in three wrong places. A partial
    output is removed, or the next run would reuse it as finished.
    """
    if not transcode:
        return src
    dest = dest_dir / f"{src.stem}.mp4"
    if dest.exists():
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin, "-y", "-i", str(src), *_FFMPEG_FLAGS, str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603 — fixed argv, no shell
    except FileNotFoundError:
        _log.warning("ffmpeg not found (%s) — %s skipped; install ffmpeg", ffmpeg_bin, src.name)
        return None
    except (subprocess.CalledProcessError, OSError) as exc:
        dest.unlink(missing_ok=True)
        _log.warning("ffmpeg could not convert %s: %s (skipping video)", src, _ffmpeg_reason(exc))
        return None
    return dest


def _ffmpeg_reason(exc: BaseException) -> str:
    """The last line ffmpeg wrote to stderr, or the exception when it wrote nothing."""
    stderr = getattr(exc, "stderr", None) or b""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else repr(exc)
