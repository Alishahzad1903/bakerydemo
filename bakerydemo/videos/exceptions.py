"""Typed exceptions for the VideoGen integration.

Every provider failure is surfaced as one of these, never as a raw SDK/``httpx``
exception. The translation happens in :mod:`bakerydemo.videos.videogen_client`, so the
rest of the code base handles a single, well-defined failure hierarchy.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigurationError(VideoGenError):
    """The integration is misconfigured (e.g. no API key, or a disallowed export tier)."""


class VideoGenAPIError(VideoGenError):
    """VideoGen returned a non-2xx response (the provider rejected or errored on the call)."""

    def __init__(self, status_code: int, message: str, code: str | None = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f"VideoGen API error {status_code}: {message}")


class VideoGenProtocolError(VideoGenError):
    """VideoGen answered with a body the SDK could not decode, or one missing required
    fields. The outcome of the call is unknown."""


class VideoGenUnavailableError(VideoGenError):
    """VideoGen could not be reached (connection/timeout/TLS). The outcome is unknown."""
