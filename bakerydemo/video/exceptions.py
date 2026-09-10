"""Typed exceptions for the VideoGen integration.

Every failure that the VideoGen provider (or the transport reaching it) can
raise is translated into one of the exceptions below, so the rest of the
application never has to know about the SDK's own exception surface. This is the
"surface provider failures as typed exceptions" contract from the task brief.

The translation itself lives in ``provider.py`` (``VideoGenGateway._translate``);
this module only declares the vocabulary.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. ``VIDEOGEN_API_KEY`` is unset).

    Raised before any request is attempted, so it never means "the provider
    said no" — it means this deployment cannot talk to VideoGen at all.
    """


class VideoGenAPIError(VideoGenError):
    """The provider returned a non-2xx response (wraps the SDK ``ApiError``).

    ``status_code`` is the HTTP status; ``code`` and ``message`` are the
    provider's machine-readable code and human message when it sent a JSON
    error body, otherwise ``None``/the raw text.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.provider_message = message


class VideoGenTransportError(VideoGenError):
    """The provider could not be reached (wraps an ``httpx`` transport error).

    The outcome of the request is unknown: a socket reset after the bytes
    reached VideoGen is indistinguishable from one before.
    """


class VideoGenResponseError(VideoGenError):
    """The provider's response could not be decoded (wraps a decode failure).

    Raised for a ``pydantic.ValidationError``/``ValueError`` from the SDK — the
    body did not match the declared schema, so the outcome is unreadable.
    """


class VideoGenProductionError(VideoGenError):
    """VideoGen accepted the work but it reached a terminal failure state.

    Carries the provider-reported reason (from the job's ``error`` field) so a
    caller can diagnose the failure without launching another job.
    """


class VideoGenTimeoutError(VideoGenError):
    """A workflow run or export did not reach a terminal state in time."""
