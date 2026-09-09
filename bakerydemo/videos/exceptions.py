"""
Typed exceptions for the article-video integration.

Provider (VideoGen) failures are surfaced as typed exceptions so callers of the
integration never have to inspect raw HTTP bodies or SDK internals to know what
went wrong.
"""

from __future__ import annotations

from typing import Any, Optional


class VideoProductionError(Exception):
    """Base class for every error raised by the article-video integration."""


class VideoGenConfigurationError(VideoProductionError):
    """The integration is misconfigured (e.g. missing ``VIDEOGEN_API_KEY``).

    This is our fault, not the provider's — it is raised before any call to
    VideoGen is attempted.
    """


class VideoGenProviderError(VideoProductionError):
    """VideoGen (the provider) rejected a request or a job it accepted failed.

    Wraps the provider's HTTP ``status``, response ``body`` and ``request_id``
    when available so failures are diagnosable without re-deriving them from
    logs.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        body: Any = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.request_id = request_id

    def __str__(self) -> str:
        base = super().__str__()
        details = []
        if self.status:
            details.append(f"status={self.status}")
        if self.request_id:
            details.append(f"request_id={self.request_id}")
        if details:
            return f"{base} ({', '.join(details)})"
        return base


class VideoGenTimeoutError(VideoProductionError):
    """A VideoGen job did not reach a terminal state within the allotted time."""
