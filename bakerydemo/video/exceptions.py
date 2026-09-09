"""Typed exceptions for the VideoGen integration.

Every failure that originates from the VideoGen provider — a non-2xx HTTP
response, a network/timeout error, or a job the provider itself reports as
``failed``/``cancelled`` — is surfaced to the rest of the application as one of
the typed exceptions defined here, never as a bare ``requests`` error or a raw
dict. Callers can therefore branch on the *kind* of failure (auth, rate limit,
bad request, provider outage, …) instead of parsing strings.
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every error raised by the VideoGen integration.

    ``message`` is always human-readable and safe to surface to an API caller.
    ``status`` is the HTTP status code returned by VideoGen (``None`` for
    client-side/transport errors). ``code`` is VideoGen's machine-readable
    ``snake_case`` error code when present. ``body`` is the parsed error body.
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

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.code:
            parts.append(f"[{self.code}]")
        return " ".join(parts)


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. ``VIDEOGEN_API_KEY`` is unset)."""


class VideoGenTransportError(VideoGenError):
    """A network-level failure — connection error, timeout or unreadable body."""


class VideoGenAuthError(VideoGenError):
    """VideoGen rejected the credentials (HTTP 401/403)."""


class VideoGenBadRequestError(VideoGenError):
    """VideoGen rejected the request parameters (HTTP 400/422)."""


class VideoGenNotFoundError(VideoGenError):
    """A referenced VideoGen resource does not exist (HTTP 404)."""


class VideoGenRateLimitError(VideoGenError):
    """VideoGen is rate limiting the account (HTTP 429)."""


class VideoGenServerError(VideoGenError):
    """VideoGen returned a server error (HTTP 5xx)."""


class VideoGenJobFailedError(VideoGenError):
    """VideoGen reported the workflow run or export as ``failed``/``cancelled``.

    This is not a transport error — the request succeeded, but the provider's
    own reported outcome for the job is a terminal failure.
    """


def error_for_status(
    status: int,
    message: str,
    *,
    code: str | None = None,
    body: Any = None,
) -> VideoGenError:
    """Map an HTTP status code to the most specific ``VideoGenError`` subclass."""
    if status in (401, 403):
        cls: type[VideoGenError] = VideoGenAuthError
    elif status in (400, 422):
        cls = VideoGenBadRequestError
    elif status == 404:
        cls = VideoGenNotFoundError
    elif status == 429:
        cls = VideoGenRateLimitError
    elif status >= 500:
        cls = VideoGenServerError
    else:
        cls = VideoGenError
    return cls(message, status=status, code=code, body=body)
