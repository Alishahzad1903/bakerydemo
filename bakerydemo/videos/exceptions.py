"""Typed exceptions for the VideoGen integration.

Every failure the VideoGen provider can produce is surfaced to the rest of the
site as one of these types. Nothing SDK-specific (``videogen.core.ApiError``,
``pydantic.ValidationError``, ``httpx.HTTPError``) is allowed to leak past the
provider boundary in :mod:`bakerydemo.videos.provider`.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. no API key is set)."""


class VideoGenAPIError(VideoGenError):
    """The provider returned an error status (wraps the SDK ``ApiError``).

    The provider's HTTP status is preserved on ``status_code`` so callers can
    keep a client 4xx distinct from a provider 5xx.
    """

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"VideoGen API error {status_code}: {message}")


class VideoGenResponseError(VideoGenError):
    """A provider response could not be decoded — the outcome is unknown.

    Raised when the SDK raises ``pydantic.ValidationError`` / ``ValueError``
    while decoding a body. Per the SDK contract these bypass both response
    modes and are *not* API errors, so we never treat them as a plain failure.
    """


class VideoGenTransportError(VideoGenError):
    """The provider was unreachable (connection/DNS/TLS/timeout)."""


class VideoGenTimeoutError(VideoGenError):
    """A provider job did not reach a terminal state within our polling budget.

    This is our own deadline, not a transport timeout. We never start another
    (billable) video to recover from it.
    """


class VideoGenJobFailedError(VideoGenError):
    """A provider job reached a terminal ``failed``/``cancelled`` state."""

    def __init__(self, message: str, *, code: str | None = None, status: str | None = None) -> None:
        self.code = code
        self.status = status
        self.message = message
        super().__init__(message)
