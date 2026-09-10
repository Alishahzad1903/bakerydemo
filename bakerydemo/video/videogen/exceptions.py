"""
Typed exception hierarchy for VideoGen provider failures.

The integration contract requires that *every* provider failure surfaces as a
typed exception rather than a bare ``requests`` error or a silently-swallowed
non-2xx response. Callers can catch the broad :class:`VideoGenError` or branch
on a specific subclass.

Two axes of failure are modelled:

* **Transport / HTTP errors** – a request reached (or failed to reach) VideoGen
  and came back non-2xx. These map from HTTP status codes and carry the
  provider's machine-readable ``code`` when present.
* **Asynchronous job failures** – a request succeeded, but the long-running
  workflow or export it kicked off later reported a terminal ``failed`` /
  ``cancelled`` state. These carry the provider-reported reason.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen failure.

    Attributes:
        message: Human-readable explanation (safe to surface to API callers).
        status: HTTP status code, when the failure originated from a response.
        code: Provider ``snake_case`` machine-readable error code, if supplied.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        return " ".join(parts)


# --- Transport / HTTP errors --------------------------------------------------


class VideoGenConnectionError(VideoGenError):
    """The request never produced an HTTP response (DNS, timeout, reset)."""


class VideoGenBadRequestError(VideoGenError):
    """VideoGen rejected the request as invalid (HTTP 400 / 422)."""


class VideoGenAuthenticationError(VideoGenError):
    """The API key is missing, empty, invalid or revoked (HTTP 401)."""


class VideoGenPermissionError(VideoGenError):
    """The API key lacks access to the requested resource (HTTP 403)."""


class VideoGenNotFoundError(VideoGenError):
    """The requested resource does not exist for this account (HTTP 404)."""


class VideoGenRateLimitError(VideoGenError):
    """A per-team, per-hour rate limit was exceeded (HTTP 429).

    Transient: the caller should back off and retry rather than treat the
    underlying job as failed.
    """


class VideoGenServerError(VideoGenError):
    """VideoGen reported an internal error (HTTP 5xx).

    Transient: retry-able; not a terminal job failure.
    """


# --- Asynchronous job failures ------------------------------------------------


class VideoGenWorkflowError(VideoGenError):
    """A script-to-video workflow run reached a terminal ``failed``/``cancelled``
    state. Terminal – the run will not recover."""


class VideoGenExportError(VideoGenError):
    """A project export reached a terminal ``failed``/``cancelled`` state.
    Terminal – the export will not recover."""


#: HTTP transport errors that are transient and safe to retry (the underlying
#: work is still in progress on VideoGen's side).
TRANSIENT_HTTP_ERRORS: tuple[type[VideoGenError], ...] = (
    VideoGenConnectionError,
    VideoGenRateLimitError,
    VideoGenServerError,
)
