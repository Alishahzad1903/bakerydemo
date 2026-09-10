"""Typed exceptions for VideoGen interactions.

The integration contract requires that provider failures surface as typed
exceptions rather than leaking raw HTTP/transport errors. Everything raised by
:class:`~bakerydemo.videos.videogen.client.VideoGenClient` (and the production
orchestration built on top of it) derives from :class:`VideoGenError`.
"""

from __future__ import annotations

from typing import Any


class VideoGenError(Exception):
    """Base class for every VideoGen failure raised by this integration."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. missing ``VIDEOGEN_API_KEY``).

    This is an operator/deployment problem, not a provider failure.
    """


class VideoGenCapabilityUnavailable(VideoGenError):
    """A capability this integration requires is not covered by the VideoGen API skill.

    Raised (rather than guessing or inventing request parameters) when a
    mandated video shape cannot be expressed with what the skill documents.
    Keeps the real, billed account from ever being charged for a video that
    does not match the required shape.
    """


class VideoGenConnectionError(VideoGenError):
    """A network-level failure talking to VideoGen (DNS, TCP, TLS, timeout)."""


class VideoGenTimeoutError(VideoGenError):
    """A poll loop exceeded its allotted time before reaching a terminal state."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned a non-2xx HTTP response.

    Attributes:
        status_code: HTTP status code returned by VideoGen.
        code: Machine-readable error code from the response body, if any.
        payload: The parsed response body (dict) or raw text, if available.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload


class VideoGenProductionFailed(VideoGenError):
    """VideoGen reported that a workflow run or export reached a failed/cancelled state.

    Attributes:
        status: The terminal provider status (``failed`` or ``cancelled``).
        provider_error: The provider-reported error payload, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str | None = None,
        provider_error: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.provider_error = provider_error
