"""Typed exceptions surfacing VideoGen provider failures.

The integration never lets a raw SDK/transport exception escape the client
wrapper. Every provider failure is translated, in one place
(``videogen_client.VideoGenService``), into one of the types below so callers
reason about a single, stable failure hierarchy instead of ``videogen.*`` /
``httpx.*`` / ``pydantic`` internals.
"""

from __future__ import annotations


class VideoGenError(Exception):
    """Base class for every VideoGen integration failure."""


class VideoGenConfigError(VideoGenError):
    """The integration is misconfigured (e.g. no ``VIDEOGEN_API_KEY`` set).

    Nothing was sent to the provider — this is our fault, not a rejection.
    """


class VideoGenRequestError(VideoGenError):
    """The provider rejected or failed a request (a non-2xx HTTP response).

    ``status_code`` is the provider's HTTP status; ``detail`` is the provider's
    (untyped) error body text, for logging only.
    """

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"VideoGen request failed with HTTP {status_code}")


class VideoGenUnavailableError(VideoGenError):
    """The provider could not be reached (transport failure).

    The outcome of the call is unknown — a reset after the bytes reached the
    server is indistinguishable from one before it.
    """


class VideoGenUnreadableError(VideoGenError):
    """The provider's response could not be decoded.

    The outcome is unknown: a 2xx body that fails to decode may mean the call
    succeeded server-side and only the response was unreadable.
    """


class VideoGenProductionError(VideoGenError):
    """The provider reported a terminal failure while producing the video.

    Raised when a workflow run or project export reaches ``failed``/``cancelled``.
    ``code`` and ``message`` come from the provider's ``ApiErrorModel`` when present.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        self.message = message
        self.code = code
        super().__init__(message)
