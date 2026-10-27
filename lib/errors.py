"""Typed exception hierarchy for the GS1 Digital Link Orchestrator.

Every module raises exceptions from this hierarchy rather than bare
``Exception`` (see ``docs/IMPLEMENTATION_SPEC.md`` §1 and §4.1). Callers can
catch :class:`OrchestratorError` to handle any tool-originated failure, or a
specific subclass for finer control.

**The three API errors put the failing call and the server's answer in their message.**
``run_execute`` records a failed row as ``repr(exc)``, so whatever is *not* in the message is
not in ``runs/*.jsonl`` — and the run log is the only durable per-row record this tool keeps.
For a long time the message was the status code alone, which is how a live ``403`` came to be
recorded as ``WordPressAPIError('WordPress API error 403')``: the HTML naming the culprit went
to a console nobody has on a scheduled run, and the row never said *which* of a page write, an
ACF write and two media uploads had failed. Diagnosing that cost hours and one wrong conclusion.

The body is scrubbed by :func:`lib.logging_setup.scrub_response_body` and bounded by
:data:`ERROR_BODY_LIMIT` before it goes anywhere near the message; ``response_body`` still holds
the raw text for programmatic inspection.
"""

from __future__ import annotations

from typing import Final

from lib.logging_setup import scrub_response_body

#: Longest response-body excerpt carried in an error message or a log line, in characters.
#: Shared by the API clients so a body is bounded identically wherever it surfaces.
ERROR_BODY_LIMIT: Final = 500

#: Appended to an excerpt that was cut, so a truncated body never reads as a complete one.
_TRUNCATION_MARKER: Final = "…"


def _body_excerpt(response_body: str) -> str:
    """Scrub, collapse whitespace in, and bound a response body for use in a message.

    Whitespace is collapsed because the bodies that matter most here are HTML error pages —
    a security plugin's block page runs to many indented lines, of which only the first few
    identify it. Collapsed, the useful part fits inside the bound and the message stays one
    line in both the console and the JSONL run log.
    """
    scrubbed = " ".join(scrub_response_body(response_body).split())
    if len(scrubbed) <= ERROR_BODY_LIMIT:
        return scrubbed
    return scrubbed[:ERROR_BODY_LIMIT] + _TRUNCATION_MARKER


def _api_detail(
    summary: str, response_body: str, *, call: str | None = None, extra: str | None = None
) -> str:
    """Compose the message an API error carries into ``repr()``, hence into the run log."""
    detail = summary
    if call:
        detail += f" on {call}"
    if extra:
        detail += f" ({extra})"
    excerpt = _body_excerpt(response_body)
    if excerpt:
        detail += f": {excerpt}"
    return detail


class OrchestratorError(Exception):
    """Base class for every error raised by this tool."""


class ConfigError(OrchestratorError):
    """Configuration is missing, malformed, or internally inconsistent."""


class MissingCredentialError(OrchestratorError):
    """A required secret (API token, app password) is absent from the env."""


class ExportParseError(OrchestratorError):
    """An Excel/CSV export row could not be parsed into a ``ProductRecord``."""


class GS1APIError(OrchestratorError):
    """A GS1 NL Digital Link API call returned a non-success response.

    Attributes:
        status_code: The HTTP status code of the failing response.
        response_body: The raw, unscrubbed response body, for programmatic inspection.
            The message carries a scrubbed, bounded excerpt of it.
        error_results: The parsed ``ErrorResult[]`` payload when the 400 body
            follows the standard v2 shape
            ``[{"identifier": ..., "errors": [{"code": ..., "message": ...}]}]``;
            ``None`` when the body is not in that shape (see §5.1).
        request_id: The server-assigned request id, when the API returns one.
        call: The request that failed, e.g. ``POST /digital-link/v2/... (gtin 087...)``,
            or ``None`` when the failure is not tied to one call.
    """

    def __init__(  # noqa: PLR0913 — one param per attribute; bundling them only hides them
        self,
        status_code: int,
        response_body: str,
        error_results: list[dict[str, object]] | None = None,
        request_id: str | None = None,
        call: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        self.error_results = error_results
        self.request_id = request_id
        self.call = call
        super().__init__(
            _api_detail(
                f"GS1 API error {status_code}",
                response_body,
                call=call,
                extra=f"request_id={request_id}" if request_id else None,
            )
        )


class WordPressAPIError(OrchestratorError):
    """A WordPress REST API call returned a non-success response.

    Attributes:
        status_code: The HTTP status code of the failing response.
        response_body: The raw, unscrubbed response body, for programmatic inspection.
            The message carries a scrubbed, bounded excerpt of it.
        call: The request that failed, e.g.
            ``POST /wp-json/wp/v2/media (upload media hero-a1b2c3d4e5f6)``, or ``None`` when
            the failure is a guard rather than a call.
    """

    def __init__(self, status_code: int, response_body: str, call: str | None = None) -> None:
        self.status_code = status_code
        self.response_body = response_body
        self.call = call
        super().__init__(
            _api_detail(f"WordPress API error {status_code}", response_body, call=call)
        )


class MediaOwnershipError(OrchestratorError):
    """A media attachment was about to be deleted that this tool did not upload.

    The media sibling of :class:`GtinMismatchError`, and it exists because the asymmetry was
    real: every mutating *page* path re-reads the page and refuses unless its ``meta.gtin``
    matches, while ``delete_media`` took an id at face value. On the live pilot site that gap
    covered 366 of 406 attachments — the client's own media library, uploaded long before this
    tool existed.

    Ownership is read from ``meta.content_sha256``, which :meth:`~lib.wp_client.
    WordPressClient.upload_media` writes on every attachment it creates and which is empty on
    everything else.

    **The failure is deliberately conservative.** An attachment of ours whose finalise call never
    landed carries no hash and so reads as "not ours": this refuses to delete it and asks for a
    human. Leaving one orphan behind is recoverable; deleting a client's product photo is not.

    Attributes:
        media_id: The attachment that was not deleted.
        reason: Why it could not be claimed — no hash, or unreadable.
    """

    def __init__(self, media_id: int, reason: str) -> None:
        self.media_id = media_id
        self.reason = reason
        super().__init__(
            f"refusing to delete media {media_id}: {reason}. Only attachments this tool "
            f"uploaded (carrying meta.content_sha256) may be deleted"
        )


class MediaIntegrityError(OrchestratorError):
    """WordPress stored a different number of bytes than were uploaded.

    A live upload was cut off mid-transfer, leaving a 1.5 MB fragment of an 8 MB video in the
    media library. WordPress answered ``201`` and the run treated it as a success, so a page was
    published against a video that would not play.

    **Why this raises rather than warns.** Dedup is a lookup on a content-addressed slug derived
    from the SHA-256 of the *local* bytes, so a fragment stored under that slug is returned by
    every later run as if it were the whole file — re-running never repairs it. The attachment
    is therefore deleted before this is raised, and the raise is what stops a page being
    published against media known to be broken.

    Attributes:
        path: The local file that was uploaded.
        sent_bytes: How many bytes were sent.
        stored_bytes: How many bytes WordPress reports having stored.
        media_id: The attachment that was created.
        deleted: Whether that attachment was successfully removed. ``False`` means it is still
            on the site and needs deleting by hand — the message says so.
        call: The request that produced it, for the run log.
    """

    def __init__(  # noqa: PLR0913 — one param per attribute; bundling them only hides them
        self,
        path: object,
        sent_bytes: int,
        stored_bytes: int,
        media_id: int,
        *,
        deleted: bool,
        call: str | None = None,
    ) -> None:
        self.path = path
        self.sent_bytes = sent_bytes
        self.stored_bytes = stored_bytes
        self.media_id = media_id
        self.deleted = deleted
        self.call = call
        cleanup = (
            "the attachment was deleted"
            if deleted
            else f"the attachment was NOT deleted — remove media {media_id} by hand"
        )
        super().__init__(
            f"WordPress stored {stored_bytes} bytes of {path} but {sent_bytes} were sent "
            f"(attachment {media_id}); {cleanup}"
        )


class OverwriteError(OrchestratorError):
    """A write would replace an existing Digital Link and overwrite was not allowed.

    Raised by ``gs1_dl_client.safe_upsert`` when the GTIN already has an entry and the
    caller did not pass ``overwrite=True`` — the GET-before-write guard that prevents
    silently clobbering a live resolver target.

    Attributes:
        gtin: The GTIN whose existing entry would be overwritten.
        existing: The current server state (snapshot) that would be replaced.
    """

    def __init__(self, gtin: str, existing: object) -> None:
        self.gtin = gtin
        self.existing = existing
        super().__init__(
            f"Digital Link already exists for GTIN {gtin}; refusing to overwrite "
            "(pass overwrite=True to replace)"
        )


class GtinMismatchError(OrchestratorError):
    """A WordPress page exists at the target slug but belongs to a different GTIN.

    Raised by ``wp_client.upsert_page`` when the page found by slug (or id) carries a
    ``meta.gtin`` that does not match the row being written (edge E8). The WordPress
    sibling of :class:`OverwriteError`: a GET-before-write guard that refuses to
    overwrite an unrelated page. Distinct from :class:`WordPressAPIError` (E11, a slug
    collision the server reports as 409) so callers can *log and skip the row* rather
    than treat it as a transport failure needing human intervention.

    Attributes:
        gtin: The GTIN of the row being written.
        existing_gtin: The GTIN recorded on the page already at that slug/id.
        wp_page_id: The id of the conflicting WordPress page.
    """

    def __init__(self, gtin: str, existing_gtin: str, wp_page_id: int) -> None:
        self.gtin = gtin
        self.existing_gtin = existing_gtin
        self.wp_page_id = wp_page_id
        super().__init__(
            f"WordPress page {wp_page_id} has meta.gtin {existing_gtin!r}, "
            f"which does not match row GTIN {gtin!r}; skipping to avoid overwriting"
        )


class TemplateError(OrchestratorError):
    """A template could not be resolved or rendered."""


class StateError(OrchestratorError):
    """The state file could not be loaded, parsed, or written."""


class ProcessListError(OrchestratorError):
    """The process list is missing, unreadable, malformed, or empty.

    Raised by ``lib.process_list.load_process_list`` when the operator's control
    file (``input/{client_id}/process/selection/selections.xlsx``) cannot be opened, has no sheet
    carrying the configured GTIN column, or carries the column with no GTINs under
    it. The list names exactly which GTINs a run may touch, so a missing or
    malformed one is an operator-config error and ``run_plan.py`` treats it like
    :class:`ConfigError` (exit 2).

    **Empty is an error, not an empty run.** A file that parses to zero GTINs would
    otherwise yield an empty plan and a run that reports success having published
    nothing — the silent no-op this tool keeps having to design against.
    """


class VideoMapError(OrchestratorError):
    """The video mapping is missing, unreadable, or not valid YAML.

    Raised by ``lib.media_video.load_video_map`` for the operator's ``mapping.yml``. The sibling
    of :class:`ProcessListError`, and for the same reason: it is a hand-edited input file, so its
    failures belong to the operator rather than to a stack trace.

    **The malformed case is the one that matters.** A *missing* file already reported cleanly,
    because every caller caught :class:`OSError`; a *hand-edited* one raised ``yaml.YAMLError``,
    which inherits from ``Exception`` alone and so escaped all of them. That put a 25-line
    traceback in front of an operator for the single failure that cannot happen unless a human
    edited the file — and this is a file the design requires a human to edit and a client to sign
    off. A stray tab from a text editor was enough. The YAML error already carries the line and
    column; this wraps it so that reaches the operator instead.
    """


class GeneratorError(OrchestratorError):
    """This run's generated content could not be loaded, parsed, written, or validated.

    Raised by ``lib.generator`` for a corrupt, unwritable, or wrong-client
    ``generation_results.json``, or a producer result that fails validation (e.g. empty
    bullet lists). Mirrors :class:`StateError`: the file is what a run publishes from, and a
    malformed one is a fault the operator must see rather than silently ignore.
    """


class LLMAPIError(OrchestratorError):
    """An Anthropic Messages API call failed or returned an unusable response.

    Raised by ``lib.llm.AnthropicClient`` for a non-success HTTP status, a transport failure,
    or a 200 whose body lacks the forced ``produce_copy`` tool call. The WordPress/GS1 sibling
    for the copy-generation backend: the operator must see a producer failure, not have it
    silently skip a product.

    Attributes:
        status_code: The HTTP status of the failing response, or ``0`` for a transport failure.
        response_body: The raw response body (already sliced to a bounded length by the caller).
        call: The request that failed, or ``None`` when the call itself succeeded and it is the
            response that could not be used.
    """

    def __init__(
        self,
        status_code: int,
        response_body: str,
        message: str | None = None,
        call: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        self.call = call
        super().__init__(
            _api_detail(message or f"Anthropic API error {status_code}", response_body, call=call)
        )
