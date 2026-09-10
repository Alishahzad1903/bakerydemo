"""Typed exceptions for the VideoGen integration.

Provider failures are always surfaced as one of these typed exceptions rather
than as ``None``, a raw dict, or an untyped SDK error, so callers (and the
background runner) can react to *what* went wrong.
"""

from __future__ import annotations

from typing import Any


class VideoIntegrationError(Exception):
    """Base class for every error raised by the article-video integration."""


class VideoGenConfigurationError(VideoIntegrationError):
    """The integration is not configured (e.g. ``VIDEOGEN_API_KEY`` is unset)."""


class VideoGenProviderError(VideoIntegrationError):
    """A call to VideoGen failed.

    Wraps the underlying provider error, preserving the HTTP ``status`` and
    parsed ``body`` when available so failures can be diagnosed without a retry.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class VideoGenerationError(VideoGenProviderError):
    """The script-to-video workflow could not be produced (start or run failed)."""


class VideoExportError(VideoGenProviderError):
    """The finished project could not be exported to a downloadable MP4."""


class VideoProductionTimeout(VideoGenProviderError):
    """Producing the video exceeded the allotted time budget."""
