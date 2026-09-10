"""Typed exceptions for VideoGen provider failures.

The VideoGen API reports errors through non-2xx HTTP status codes plus a JSON
``ApiError`` body of the shape ``{"message": str, "code": str | None}``
(https://docs.videogen.io/errors). We translate those into a small exception
hierarchy so the rest of the integration can catch meaningful types instead of
inspecting status codes.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen failure.

    Attributes:
        message: Human-readable explanation (safe to surface to operators).
        status: HTTP status code, when the failure came from an HTTP response.
        code: Machine-readable ``snake_case`` error code, when provided.
        body: The raw parsed error body, for diagnostics/logging.
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

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.code:
            parts.append(f"[{self.code}]")
        return " ".join(parts)


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured locally (e.g. missing API key)."""


class VideoGenConnectionError(VideoGenError):
    """The request never produced an HTTP response (network error / timeout)."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned a non-2xx HTTP response."""


class VideoGenBadRequestError(VideoGenAPIError):
    """400 / 422 - invalid parameters or malformed request body."""


class VideoGenAuthenticationError(VideoGenAPIError):
    """401 - missing or invalid API key."""


class VideoGenPermissionError(VideoGenAPIError):
    """403 - the API key lacks access to the resource."""


class VideoGenNotFoundError(VideoGenAPIError):
    """404 - the resource does not exist or does not belong to the team."""


class VideoGenRateLimitError(VideoGenAPIError):
    """429 - rate limit exceeded."""


class VideoGenServerError(VideoGenAPIError):
    """5xx - something failed on VideoGen's side."""


class VideoGenWorkflowFailedError(VideoGenError):
    """A workflow run reached a terminal ``failed`` / ``cancelled`` status."""


class VideoGenExportFailedError(VideoGenError):
    """A project export reached a terminal ``failed`` / ``cancelled`` status."""


def api_error_for_status(
    status: int,
    message: str,
    *,
    code: str | None = None,
    body: object | None = None,
) -> VideoGenAPIError:
    """Map an HTTP status code to the most specific ``VideoGenAPIError``."""
    if status in (400, 422):
        cls: type[VideoGenAPIError] = VideoGenBadRequestError
    elif status == 401:
        cls = VideoGenAuthenticationError
    elif status == 403:
        cls = VideoGenPermissionError
    elif status == 404:
        cls = VideoGenNotFoundError
    elif status == 429:
        cls = VideoGenRateLimitError
    elif 500 <= status < 600:
        cls = VideoGenServerError
    else:
        cls = VideoGenAPIError
    return cls(message, status=status, code=code, body=body)
