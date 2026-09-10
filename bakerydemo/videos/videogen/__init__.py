"""Client library for the VideoGen video-production API.

This package is the only place in the project that knows how to talk to
VideoGen over HTTP. Everything the rest of the site needs is re-exported here:

- :class:`~bakerydemo.videos.videogen.client.VideoGenClient` — the HTTP client.
- The typed exception hierarchy in
  :mod:`bakerydemo.videos.videogen.exceptions`, so callers can surface provider
  failures as typed exceptions rather than opaque errors.
"""

from .client import VideoGenClient
from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenJobFailedError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)

__all__ = [
    "VideoGenClient",
    "VideoGenError",
    "VideoGenConfigurationError",
    "VideoGenConnectionError",
    "VideoGenTimeoutError",
    "VideoGenJobFailedError",
    "VideoGenAPIError",
    "VideoGenAuthError",
    "VideoGenBadRequestError",
    "VideoGenNotFoundError",
    "VideoGenRateLimitError",
    "VideoGenServerError",
]
