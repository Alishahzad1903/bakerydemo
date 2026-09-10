"""Typed exceptions for the VideoGen integration.

Every failure that originates from the VideoGen provider is surfaced as one of
these exceptions so callers never have to inspect raw HTTP responses. They form
a small hierarchy rooted at :class:`VideoGenError`.
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every VideoGen provider failure."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is not configured correctly (e.g. missing API key).

    This is a local misconfiguration rather than a provider response, but it is
    part of the same hierarchy so callers can catch a single base class.
    """


class VideoGenConnectionError(VideoGenError):
    """The VideoGen API could not be reached (network / DNS / TLS failure)."""


class VideoGenTimeoutError(VideoGenConnectionError):
    """A request to the VideoGen API timed out before a response arrived."""


class VideoGenAPIError(VideoGenError):
    """The VideoGen API returned a non-2xx response.

    Carries the HTTP ``status`` code and the parsed provider error ``body``
    (VideoGen's ``ApiError`` shape: ``{"message": ..., "code": ...}``) so the
    detail can be logged and, where safe, relayed to callers.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        base = super().__str__()
        if self.status is not None:
            return f"[{self.status}] {base}"
        return base


class VideoGenAuthError(VideoGenAPIError):
    """Authentication with VideoGen failed (HTTP 401 / 403)."""


class VideoGenNotFoundError(VideoGenAPIError):
    """A VideoGen resource was not found (HTTP 404)."""


class VideoGenRateLimitError(VideoGenAPIError):
    """VideoGen rate limit exceeded (HTTP 429)."""


class VideoGenServerError(VideoGenAPIError):
    """VideoGen reported a server-side failure (HTTP 5xx)."""
