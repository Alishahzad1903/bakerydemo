"""VideoGen integration package: typed client wrapper and exceptions."""

from .client import (
    StartedExport,
    StartedRun,
    VideoGenClient,
    VideoGenConfig,
)
from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConfigurationError,
    VideoGenError,
    VideoGenProductionError,
    VideoGenTimeoutError,
)

__all__ = [
    "StartedExport",
    "StartedRun",
    "VideoGenClient",
    "VideoGenConfig",
    "VideoGenAPIError",
    "VideoGenAuthError",
    "VideoGenConfigurationError",
    "VideoGenError",
    "VideoGenProductionError",
    "VideoGenTimeoutError",
]
