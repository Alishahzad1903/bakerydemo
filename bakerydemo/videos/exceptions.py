"""Typed exceptions for the VideoGen integration.

The integration surfaces *every* provider failure as one of these, so callers of
the service layer never see a raw ``videogen`` SDK exception, an ``httpx``
transport error, or a pydantic decode error. The API layer maps them onto HTTP
responses (see ``bakerydemo.videos.api``).
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every failure raised by this integration."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. no API key). Our fault, not the provider's."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned a non-2xx response. Carries the provider's HTTP status.

    In this SDK every operation's error body is a ``RawError`` (no typed error
    arm anywhere), so we keep the provider status plus its raw text.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"VideoGen API error {status_code}: {detail}")


class VideoGenResponseError(VideoGenError):
    """VideoGen's response could not be read (decode failure or a required field
    missing). The outcome of the call is *unknown* — never assume it failed."""


class VideoGenUnavailableError(VideoGenError):
    """VideoGen could not be reached (transport failure/timeout). Outcome unknown."""
