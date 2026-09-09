"""Typed exceptions for the VideoGen integration.

Provider failures are surfaced as these typed exceptions rather than leaking
raw SDK/transport errors into the rest of the site. The integration layer
(:mod:`bakerydemo.videos.videogen_service`) translates every failure kind the
SDK can raise — an ``ApiError`` (HTTP error status), a decode failure
(``pydantic.ValidationError`` / ``ValueError``), or an unwrapped ``httpx``
transport error — into one of these, so callers catch a single, stable
hierarchy.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. no API key set)."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned an error HTTP status.

    ``status_code`` is the provider's HTTP status; ``detail`` is the provider's
    error text (never a raw exception ``repr``).
    """

    def __init__(self, message: str, *, status_code: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class VideoGenUnavailable(VideoGenError):
    """VideoGen could not be reached (transport failure). Outcome unknown."""


class VideoGenUnreadableResponse(VideoGenError):
    """A VideoGen response could not be decoded or was missing required data.

    The call may have succeeded provider-side; the outcome is unknown.
    """


class VideoGenProductionFailed(VideoGenError):
    """VideoGen accepted the work but the job finished in a failed/cancelled state."""


class VideoGenTimeout(VideoGenError):
    """Production did not reach a terminal state within the allotted time."""
