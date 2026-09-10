"""Typed exceptions for VideoGen API failures.

The integration contract requires that *every* provider failure surfaces as a
typed exception rather than a bare dict or a generic ``Exception``. The
hierarchy mirrors the error model documented by VideoGen: a non-2xx response
carries an ``ApiError`` body of ``{"message": ..., "code": ...}`` and the HTTP
status is the primary signal, so each transport error maps to a subclass keyed
on status. Two additional subclasses model *terminal* asynchronous failures
(a workflow run or an export that the provider itself reports as ``failed``),
which are not HTTP errors but still must propagate as typed exceptions.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen failure.

    ``status`` is the HTTP status code when the failure originated from a
    response (``None`` for transport/terminal failures). ``code`` is the
    machine-readable ``ApiError.code`` when the provider supplied one.
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
            parts.append(f"code={self.code}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        return " ".join(parts)


# --- Transport errors, keyed on HTTP status -------------------------------


class VideoGenBadRequestError(VideoGenError):
    """HTTP 400 - invalid parameters or malformed request."""


class VideoGenAuthenticationError(VideoGenError):
    """HTTP 401 - the API key is missing, empty, invalid or revoked."""


class VideoGenPermissionError(VideoGenError):
    """HTTP 403 - the key does not have access to this resource."""


class VideoGenNotFoundError(VideoGenError):
    """HTTP 404 - the resource does not exist or belongs to another team."""


class VideoGenRateLimitError(VideoGenError):
    """HTTP 429 - rate limit exceeded; retry after backing off."""


class VideoGenServerError(VideoGenError):
    """HTTP 5xx - something went wrong on VideoGen's side."""


class VideoGenConnectionError(VideoGenError):
    """The request never produced an HTTP response (DNS, TCP, TLS, ...)."""


class VideoGenTimeoutError(VideoGenConnectionError):
    """The request or a polling loop exceeded its time budget."""


# --- Terminal asynchronous failures ---------------------------------------


class VideoGenWorkflowFailedError(VideoGenError):
    """A script-to-video workflow run reached the terminal ``failed`` state."""


class VideoGenExportFailedError(VideoGenError):
    """A project export reached the terminal ``failed`` state."""


def error_for_status(status: int, message: str, code: str | None) -> VideoGenError:
    """Map an HTTP ``status`` to the most specific :class:`VideoGenError`."""
    mapping: dict[int, type[VideoGenError]] = {
        400: VideoGenBadRequestError,
        401: VideoGenAuthenticationError,
        403: VideoGenPermissionError,
        404: VideoGenNotFoundError,
        429: VideoGenRateLimitError,
    }
    exc_class = mapping.get(status)
    if exc_class is None:
        exc_class = VideoGenServerError if status >= 500 else VideoGenError
    return exc_class(message, status=status, code=code)
