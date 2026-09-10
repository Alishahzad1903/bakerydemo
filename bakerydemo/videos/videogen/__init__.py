"""Thin, typed client for the VideoGen HTTP API.

Everything this package knows about VideoGen comes from the official docs at
https://docs.videogen.io. The client surfaces provider failures as typed
exceptions (see :mod:`bakerydemo.videos.videogen.exceptions`) so callers never
have to branch on raw HTTP status codes.
"""

from .client import VideoGenClient, get_client
from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenExportFailedError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenWorkflowFailedError,
)

__all__ = [
    "VideoGenClient",
    "get_client",
    "VideoGenError",
    "VideoGenConfigurationError",
    "VideoGenConnectionError",
    "VideoGenAPIError",
    "VideoGenAuthenticationError",
    "VideoGenPermissionError",
    "VideoGenNotFoundError",
    "VideoGenBadRequestError",
    "VideoGenRateLimitError",
    "VideoGenServerError",
    "VideoGenWorkflowFailedError",
    "VideoGenExportFailedError",
]
