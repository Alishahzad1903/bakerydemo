"""Typed exceptions for the VideoGen integration.

Every failure surfaced by :class:`~bakerydemo.videogen.client.VideoGenClient`
and the production pipeline is a subclass of :class:`VideoGenError`, so callers
can branch on the failure kind without inspecting raw HTTP responses.
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure.

    Attributes:
        message: Human-readable explanation, safe to surface to editors.
        status: HTTP status code the provider returned, when applicable.
        code: Machine-readable ``code`` from the provider's ``ApiError`` body.
        body: The raw parsed response body, for logging/diagnostics.
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
        self.message = message
        self.status = status
        self.code = code
        self.body = body


class VideoGenConfigurationError(VideoGenError):
    """The integration is not configured (e.g. ``VIDEOGEN_API_KEY`` is unset)."""


class VideoGenConnectionError(VideoGenError):
    """The provider could not be reached (DNS/TCP/TLS failure)."""


class VideoGenTimeoutError(VideoGenError):
    """The provider did not respond within the configured timeout."""


class VideoGenAPIError(VideoGenError):
    """The provider returned a non-2xx HTTP response."""


class VideoGenBadRequestError(VideoGenAPIError):
    """400 - the request was malformed or failed provider-side validation."""


class VideoGenAuthenticationError(VideoGenAPIError):
    """401 - the API key is missing, invalid or revoked."""


class VideoGenPermissionError(VideoGenAPIError):
    """403 - the API key may not access the requested resource."""


class VideoGenNotFoundError(VideoGenAPIError):
    """404 - the requested provider resource does not exist."""


class VideoGenRateLimitError(VideoGenAPIError):
    """429 - the provider rate limit was exceeded."""


class VideoGenServerError(VideoGenAPIError):
    """5xx - the provider had an internal error."""


class VideoGenJobFailedError(VideoGenError):
    """A workflow run or export reached a terminal ``failed``/``cancelled`` state.

    This is a *reported* failure (the request itself succeeded), carrying the
    ``error`` details the provider attached to the run or export.
    """
