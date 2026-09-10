"""Typed exceptions for the VideoGen integration.

Every failure surfaced by :mod:`bakerydemo.video.client` is one of these types, so
callers never have to inspect raw ``requests`` errors or HTTP status codes. The
service layer catches :class:`VideoGenError` and records ``str(exc)`` on the job so
the ``GET`` endpoint can report *what went wrong* to the API consumer.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen failure.

    Attributes
    ----------
    message:
        Human-readable explanation, safe to surface to an API consumer.
    status:
        HTTP status code returned by VideoGen, when the failure was an HTTP
        response (``None`` for connection/config errors).
    code:
        Machine-readable ``snake_case`` error code from VideoGen's ``ApiError``
        body, when present.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.code:
            parts.append(f"(code={self.code})")
        if self.status is not None:
            parts.append(f"[HTTP {self.status}]")
        return " ".join(parts)


class VideoGenConfigurationError(VideoGenError):
    """The integration is not configured (e.g. ``VIDEOGEN_API_KEY`` is unset)."""


class VideoGenConnectionError(VideoGenError):
    """The VideoGen API could not be reached (network error or timeout)."""


class VideoGenBadRequestError(VideoGenError):
    """VideoGen rejected the request as invalid (HTTP 400)."""


class VideoGenAuthError(VideoGenError):
    """The API key is missing, invalid or revoked (HTTP 401)."""


class VideoGenForbiddenError(VideoGenError):
    """The API key lacks access to the resource (HTTP 403)."""


class VideoGenNotFoundError(VideoGenError):
    """The referenced VideoGen resource does not exist (HTTP 404)."""


class VideoGenRateLimitError(VideoGenError):
    """VideoGen is rate limiting the account (HTTP 429)."""


class VideoGenServerError(VideoGenError):
    """VideoGen reported an internal error (HTTP 5xx)."""


class VideoGenProductionError(VideoGenError):
    """A workflow run or export finished in a non-successful terminal state.

    Raised when VideoGen itself reports ``failed`` or ``cancelled`` for the
    asynchronous production/export, carrying the provider-reported reason.
    """


class VideoGenTimeoutError(VideoGenError):
    """Production/export did not reach a terminal state within the poll budget."""


def error_for_status(
    status: int, message: str, code: str | None = None
) -> VideoGenError:
    """Map an HTTP status code onto the most specific :class:`VideoGenError`."""
    mapping: dict[int, type[VideoGenError]] = {
        400: VideoGenBadRequestError,
        401: VideoGenAuthError,
        403: VideoGenForbiddenError,
        404: VideoGenNotFoundError,
        429: VideoGenRateLimitError,
    }
    if status in mapping:
        exc_class = mapping[status]
    elif 500 <= status < 600:
        exc_class = VideoGenServerError
    else:
        exc_class = VideoGenError
    return exc_class(message, status=status, code=code)
