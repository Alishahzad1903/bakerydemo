"""Typed exceptions for the VideoGen integration.

Every failure that originates from talking to VideoGen is surfaced as one of
these typed exceptions, so callers never have to inspect raw HTTP status codes
or the third-party SDK's own error classes. This is a hard requirement of the
integration: provider failures must be typed.

The hierarchy::

    VideoGenError                      (base for everything in this package)
    ├── VideoGenConfigurationError     (integration is not configured, e.g. no API key)
    ├── VideoGenAPIError               (VideoGen returned an HTTP error response)
    │   └── VideoGenAuthError          (VideoGen rejected our credentials: 401/403)
    ├── VideoGenProductionError        (a run/export reached a terminal "failed"/"cancelled" state)
    └── VideoGenTimeoutError           (we gave up waiting for a run/export to finish)
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every error raised by the VideoGen integration."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is missing required configuration (e.g. the API key).

    This is our own fault, not the provider's — it is raised before any network
    call is attempted.
    """


class VideoGenAPIError(VideoGenError):
    """VideoGen returned an unsuccessful HTTP response.

    Carries the provider's reported ``status`` code, response ``body`` and
    ``request_id`` (when present) so failures can be diagnosed without a second
    call to the provider.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body
        self.request_id = request_id

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        details = []
        if self.status is not None:
            details.append(f"status={self.status}")
        if self.request_id:
            details.append(f"request_id={self.request_id}")
        return f"{base} ({', '.join(details)})" if details else base


class VideoGenAuthError(VideoGenAPIError):
    """VideoGen rejected our API credentials (HTTP 401/403)."""


class VideoGenProductionError(VideoGenError):
    """A workflow run or export finished in a terminal non-success state.

    Raised when VideoGen reports ``status == "failed"`` (or ``"cancelled"``) for
    a run or export we are waiting on. ``provider_status`` records which one, and
    ``body`` keeps the provider's last reported payload for diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_status: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status
        self.body = body


class VideoGenTimeoutError(VideoGenError):
    """We stopped waiting for a run or export to reach a terminal state."""
