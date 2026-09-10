"""Typed exceptions for the VideoGen integration.

Every failure the VideoGen provider (or the transport to it) can produce is
surfaced to the rest of the site as one of these types, never as a raw
``videogen.core.ApiError``, ``pydantic.ValidationError`` or ``httpx`` error.
The translation happens in one place — :mod:`bakerydemo.videos.service`.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. no API key configured).

    This is our fault, not the provider's — nothing was ever sent.
    """


class VideoGenAPIError(VideoGenError):
    """The VideoGen API answered with an error status.

    ``status_code`` carries the provider's HTTP status so a caller can keep a
    provider 4xx distinct from a provider/transport 5xx.
    """

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"VideoGen API error (HTTP {status_code}): {detail}".rstrip(": "))


class VideoGenUnavailableError(VideoGenError):
    """VideoGen could not be reached (transport failure); outcome unknown."""


class VideoGenUnreadableError(VideoGenError):
    """VideoGen returned a response we could not read; outcome unknown.

    Raised for a decode failure (a body that does not match the SDK's schema)
    and for a 2xx that decoded but is missing a member we depend on.
    """


class VideoGenTimeoutError(VideoGenError):
    """A VideoGen job did not reach a terminal state within our time budget."""
