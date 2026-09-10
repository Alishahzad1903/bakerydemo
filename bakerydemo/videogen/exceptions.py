"""Typed exceptions for the VideoGen integration.

Every failure that originates from the VideoGen provider (a bad response, a
failed workflow run, a failed export, a network problem, a misconfiguration)
is surfaced as one of these exceptions rather than a bare ``requests`` error or
a ``KeyError``. Callers — the orchestration service, the management command and
the tests — can therefore catch :class:`VideoGenError` to handle "the provider
let us down" distinctly from ordinary bugs.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen failure surfaced by this integration."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is not configured correctly (e.g. missing API key)."""


class VideoGenConnectionError(VideoGenError):
    """A network-level failure talking to VideoGen (timeout, DNS, TLS, ...)."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned an unexpected/error HTTP response.

    Carries the HTTP ``status_code`` and, when the provider supplied a machine
    readable ``ApiError`` body, its ``code``. ``payload`` keeps the parsed
    response body (or raw text) for logging and debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        payload: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload


class VideoGenWorkflowError(VideoGenError):
    """A VideoGen workflow run reached a terminal non-success state.

    Raised when the ``script-to-video`` run finishes as ``failed`` or
    ``cancelled``. ``code`` mirrors the provider's ``ApiError.code`` when present.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class VideoGenExportError(VideoGenError):
    """A VideoGen project export reached a terminal non-success state."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class VideoGenTimeoutError(VideoGenError):
    """Polling a workflow run or export exceeded the allotted time budget."""
