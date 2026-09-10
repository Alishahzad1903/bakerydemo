"""Thin, typed client for the VideoGen HTTP API.

Every VideoGen interaction in this project goes through :class:`VideoGenClient`.
Provider failures are surfaced as the typed exceptions defined in
:mod:`bakerydemo.videos.videogen.exceptions`.
"""

from .client import VideoGenClient, get_client
from .exceptions import (
    VideoGenAPIError,
    VideoGenCapabilityUnavailable,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenProductionFailed,
    VideoGenTimeoutError,
)

__all__ = [
    "VideoGenClient",
    "get_client",
    "VideoGenError",
    "VideoGenConfigurationError",
    "VideoGenCapabilityUnavailable",
    "VideoGenConnectionError",
    "VideoGenAPIError",
    "VideoGenTimeoutError",
    "VideoGenProductionFailed",
]
