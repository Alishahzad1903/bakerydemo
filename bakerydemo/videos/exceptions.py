"""Typed exceptions for the VideoGen integration.

Every failure that originates from the VideoGen provider (a bad request, an
auth problem, exhausted credits, a failed render, a timeout, ...) is surfaced
as one of these exceptions rather than a bare ``requests`` error or a silently
swallowed ``None``. Callers (the service layer, the API layer) can therefore
branch on a stable, documented hierarchy instead of parsing strings.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen provider failure."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. no API key is available).

    This is a deployment/environment problem, not a provider-side failure.
    """


class VideoGenAPIError(VideoGenError):
    """A non-2xx HTTP response from the VideoGen API.

    The provider returns a standard error body (``{message, code, ...}``) with
    every non-2xx response; those details are captured here so callers can
    branch on :attr:`code` rather than the human-readable :attr:`message`.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        internal_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.internal_error_code = internal_error_code

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status_code is not None:
            parts.append(f"http={self.status_code}")
        return " ".join(parts)


class VideoGenAuthError(VideoGenAPIError):
    """The API key was missing, invalid, or not permitted (HTTP 401/403)."""


class VideoGenInsufficientCreditsError(VideoGenAPIError):
    """The account is out of credits / rate limited for billing (HTTP 429)."""


class VideoGenConnectionError(VideoGenError):
    """A network-level failure talking to the VideoGen API (no HTTP response)."""


class VideoGenTimeoutError(VideoGenError):
    """A workflow run or export did not reach a terminal state in time."""


class VideoGenJobFailedError(VideoGenError):
    """The provider reported a terminal ``failed``/``cancelled`` state.

    :attr:`code` mirrors the provider's machine-readable error code when present.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
