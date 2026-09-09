"""
Typed exceptions for the VideoGen integration.

Every provider failure surfaces as one of these, so callers never have to
inspect raw HTTP responses or ``requests`` internals to reason about what went
wrong. ``VideoGenError`` is the single base to catch.
"""


class VideoGenError(Exception):
    """Base class for every VideoGen failure."""

    def __init__(self, message, *, status_code=None, code=None):
        super().__init__(message)
        self.message = message
        # HTTP status returned by VideoGen, when the failure came from a response.
        self.status_code = status_code
        # Machine-readable ``code`` from VideoGen's error body, when present.
        self.code = code


class VideoGenConfigurationError(VideoGenError):
    """The integration is missing required configuration (e.g. the API key)."""


class VideoGenConnectionError(VideoGenError):
    """The VideoGen API could not be reached (DNS, TLS, connection refused...)."""


class VideoGenTimeoutError(VideoGenError):
    """A request timed out, or a run/export did not finish within the deadline."""


class VideoGenAuthError(VideoGenError):
    """VideoGen rejected the credentials or denied access (HTTP 401/403)."""


class VideoGenNotFoundError(VideoGenError):
    """A referenced VideoGen resource does not exist (HTTP 404)."""


class VideoGenRateLimitError(VideoGenError):
    """VideoGen rate limit exceeded (HTTP 429)."""


class VideoGenBadRequestError(VideoGenError):
    """VideoGen rejected the request payload (HTTP 4xx other than the above)."""


class VideoGenServerError(VideoGenError):
    """VideoGen returned a server-side error (HTTP 5xx) or an unusable response."""


class VideoGenProductionError(VideoGenError):
    """A workflow run or export reached a terminal ``failed``/``cancelled`` state."""
