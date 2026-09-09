"""Typed exceptions for the VideoGen integration.

Every failure that originates from the VideoGen provider (HTTP errors,
malformed responses, workflow/export failures, timeouts) is surfaced as one of
these typed exceptions so callers never have to inspect raw HTTP status codes
or provider payloads to reason about what went wrong.
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every VideoGen provider failure.

    Attributes:
        message: Human-readable description of the failure.
        status: HTTP status code when the failure came from an HTTP response,
            otherwise ``None``.
        code: Provider-specific error code when available.
        request_id: The provider's request id (from the ``x-request-id``
            response header) when available, useful for support tickets.
        body: The parsed provider response body when available.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        self.body = body

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.code:
            parts.append(f"code={self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts) if len(parts) == 1 else f"{parts[0]} ({', '.join(parts[1:])})"


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. the API key is not set)."""


class VideoGenConnectionError(VideoGenError):
    """The VideoGen API could not be reached (network/DNS/connection error)."""


class VideoGenTimeoutError(VideoGenError):
    """A VideoGen request or a poll loop exceeded its time budget."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned an unsuccessful HTTP response (4xx/5xx)."""


class VideoGenAuthError(VideoGenAPIError):
    """VideoGen rejected the credentials (HTTP 401/403)."""


class VideoGenRateLimitError(VideoGenAPIError):
    """VideoGen rate-limited the request (HTTP 429)."""


class VideoGenResponseError(VideoGenError):
    """VideoGen returned a success status but a response we cannot interpret."""


class VideoGenWorkflowError(VideoGenError):
    """A VideoGen workflow run reached a terminal ``failed``/``cancelled`` state."""


class VideoGenExportError(VideoGenError):
    """A VideoGen project export reached a terminal ``failed``/``cancelled`` state."""
