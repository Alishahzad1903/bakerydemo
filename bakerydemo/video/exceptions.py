"""
Typed exceptions for the VideoGen integration.

The integration surfaces every provider failure as a typed exception rather
than leaking raw SDK/transport errors or generic ``Exception``s. Callers (the
background producer and the tests) can therefore distinguish a misconfiguration
from a provider-side failure from a timeout.
"""

from __future__ import annotations

from typing import Any


class VideoGenIntegrationError(Exception):
    """Base class for every error raised by the VideoGen integration."""


class VideoGenConfigurationError(VideoGenIntegrationError):
    """The integration is not configured correctly (e.g. no API key)."""


class VideoGenServiceError(VideoGenIntegrationError):
    """
    VideoGen reported a failure while producing or exporting the video.

    Carries the provider's HTTP status, response body and request id (when
    available) plus the pipeline ``stage`` that failed, so failures can be
    diagnosed from what the provider reported without producing another video.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status: int | None = None,
        provider_body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.provider_body = provider_body
        self.request_id = request_id

    def __str__(self) -> str:
        base = super().__str__()
        details = [f"stage={self.stage}"]
        if self.status is not None:
            details.append(f"status={self.status}")
        if self.request_id:
            details.append(f"request_id={self.request_id}")
        return f"{base} ({', '.join(details)})"


class VideoGenTimeoutError(VideoGenIntegrationError):
    """A VideoGen operation did not reach a terminal state within the timeout."""

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


class VideoGenCancelledError(VideoGenIntegrationError):
    """A VideoGen operation was cancelled before completing."""

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage
