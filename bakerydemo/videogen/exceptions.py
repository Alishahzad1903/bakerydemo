"""Typed exceptions for the VideoGen integration.

Every failure that originates from talking to VideoGen (or from our own
pre-flight checks before we talk to it) is surfaced as one of these typed
exceptions rather than a bare ``Exception`` or a leaked SDK error. This keeps
the provider boundary explicit: callers of :mod:`bakerydemo.videogen.provider`
only ever have to catch :class:`VideoGenIntegrationError`.
"""

from __future__ import annotations

from typing import Any


class VideoGenIntegrationError(Exception):
    """Base class for every error raised by the VideoGen integration."""

    #: Human-friendly, safe-to-store summary of what went wrong.
    default_message = "The video provider request failed."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)

    @property
    def message(self) -> str:
        return str(self)


class VideoGenConfigurationError(VideoGenIntegrationError):
    """The integration is not configured correctly (e.g. missing API key)."""

    default_message = "The video provider is not configured."


class VideoGenAPIError(VideoGenIntegrationError):
    """VideoGen returned an HTTP/transport error.

    Wraps the provider's own error so the underlying status code, response body
    and request id remain available for diagnostics without leaking the raw SDK
    exception type past the provider boundary.
    """

    default_message = "The video provider returned an error."

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.request_id = request_id


class VideoGenProductionError(VideoGenIntegrationError):
    """VideoGen accepted the request but the video could not be produced.

    Raised when a workflow run or export reaches a terminal ``failed`` /
    ``cancelled`` state, or when the finished project yields no downloadable
    MP4.
    """

    default_message = "The provider could not produce the video."


class VideoGenTimeoutError(VideoGenIntegrationError):
    """Production did not reach a terminal state within the allotted time."""

    default_message = "Timed out while waiting for the video to be produced."
