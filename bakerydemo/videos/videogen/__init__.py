"""Thin client for the VideoGen HTTP API.

Everything the site needs to talk to VideoGen lives in this package: a typed
exception hierarchy (:mod:`.exceptions`) and a small HTTP client
(:mod:`.client`). The client is built strictly from the behaviour documented by
the ``videogen-docs`` reference (base URL, bearer auth, the script-to-video
workflow, run polling and project export).
"""

from .client import VideoGenClient, get_client
from .exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenExportFailedError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
    VideoGenWorkflowFailedError,
)

__all__ = [
    "VideoGenClient",
    "get_client",
    "VideoGenError",
    "VideoGenAuthenticationError",
    "VideoGenPermissionError",
    "VideoGenNotFoundError",
    "VideoGenBadRequestError",
    "VideoGenRateLimitError",
    "VideoGenServerError",
    "VideoGenConnectionError",
    "VideoGenTimeoutError",
    "VideoGenWorkflowFailedError",
    "VideoGenExportFailedError",
]
