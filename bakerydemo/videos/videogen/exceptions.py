"""Typed exceptions for VideoGen provider failures.

Every failure that originates with the VideoGen provider is surfaced as one of
these types, never as a bare ``Exception``, a ``requests`` error, or a raw dict.
Callers can therefore branch on the failure mode (auth vs. rate-limit vs. the
provider reporting the job itself failed) without string-matching messages.

The hierarchy mirrors the provider's documented error model: a JSON body with a
human-readable ``message`` and an optional machine-readable ``code``, keyed off
the HTTP status code as the primary signal.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen-related failure.

    Attributes:
        message: Human-readable description, safe to log.
        status: HTTP status code, when the failure came from an HTTP response.
        code: Machine-readable ``snake_case`` code from the provider, if any.
        body: The decoded provider response body, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        body: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured locally (e.g. no API key set).

    This is raised before any request leaves the process, so it never implies
    the provider was contacted or billed.
    """


class VideoGenConnectionError(VideoGenError):
    """The request never reached VideoGen (DNS, TCP, TLS or timeout error)."""


class VideoGenTimeoutError(VideoGenError):
    """A polling loop exceeded its deadline before reaching a terminal state."""


class VideoGenJobFailedError(VideoGenError):
    """VideoGen accepted the work but later reported it ``failed``/``cancelled``.

    Distinct from :class:`VideoGenAPIError`: the HTTP calls all succeeded, but
    the asynchronous job the provider was running did not produce a video.
    """


class VideoGenAPIError(VideoGenError):
    """VideoGen returned a non-2xx HTTP response."""


class VideoGenBadRequestError(VideoGenAPIError):
    """HTTP 400 — invalid parameters or malformed request body."""


class VideoGenAuthError(VideoGenAPIError):
    """HTTP 401/403 — missing, invalid or unauthorized API key."""


class VideoGenNotFoundError(VideoGenAPIError):
    """HTTP 404 — the resource does not exist or is not on this team."""


class VideoGenRateLimitError(VideoGenAPIError):
    """HTTP 429 — rate limit exceeded.

    Attributes:
        retry_after: Seconds to wait before retrying, parsed from the response
            when the provider supplies it; otherwise ``None``.
    """

    def __init__(self, *args, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class VideoGenServerError(VideoGenAPIError):
    """HTTP 5xx — an error on the VideoGen side."""


def api_error_for_status(
    status: int,
    message: str,
    *,
    code: str | None = None,
    body: object | None = None,
    retry_after: float | None = None,
) -> VideoGenAPIError:
    """Return the most specific :class:`VideoGenAPIError` for an HTTP status."""
    if status == 400:
        return VideoGenBadRequestError(message, status=status, code=code, body=body)
    if status in (401, 403):
        return VideoGenAuthError(message, status=status, code=code, body=body)
    if status == 404:
        return VideoGenNotFoundError(message, status=status, code=code, body=body)
    if status == 429:
        return VideoGenRateLimitError(
            message, status=status, code=code, body=body, retry_after=retry_after
        )
    if 500 <= status < 600:
        return VideoGenServerError(message, status=status, code=code, body=body)
    return VideoGenAPIError(message, status=status, code=code, body=body)
