"""Typed exceptions for the VideoGen integration.

Every VideoGen provider failure is surfaced to the rest of the site as one of
these types, never as a raw SDK ``ApiError``, ``httpx`` transport error or
pydantic ``ValidationError``. The translation happens in
:mod:`bakerydemo.videos.service`.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. ``VIDEOGEN_API_KEY`` is unset,
    or ``VIDEOGEN_EXPORT_QUALITY`` is not a known tier). Nothing was sent to
    the provider."""


class VideoGenContentError(VideoGenError):
    """The article does not contain enough of its own text to narrate."""


class VideoGenApiError(VideoGenError):
    """The VideoGen API returned a non-2xx response.

    Carries the provider HTTP status and message so callers can distinguish a
    client-side rejection (4xx) from a provider outage (5xx).
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"VideoGen API error {status_code}: {message}")


class VideoGenUnreachableError(VideoGenError):
    """The VideoGen API (or the signed MP4 host) could not be reached, or the
    connection failed mid-request. The outcome of any write is unknown."""


class VideoGenUnreadableResponseError(VideoGenError):
    """The VideoGen API returned a response the SDK could not decode, or one
    missing a field we depend on. The outcome is unknown."""


class VideoGenProductionError(VideoGenError):
    """The VideoGen workflow run or project export itself reached a terminal
    ``failed``/``cancelled`` state, or did not finish in time."""
