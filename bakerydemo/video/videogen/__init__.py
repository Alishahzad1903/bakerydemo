"""
Client library for the VideoGen (https://videogen.io) HTTP API.

This subpackage is the *only* place in the project that knows how to talk to
VideoGen. It is deliberately framework-agnostic (no Django imports) so it can be
unit-tested in isolation and swapped for a different provider account simply by
changing the base URL / API key passed in from Django settings.

Every provider failure is surfaced as a typed :class:`VideoGenError` subclass –
callers never see a raw ``requests`` exception or a non-2xx response.
"""

from .client import (
    ProjectExport,
    ScriptToVideoResult,
    VideoGenClient,
    WorkflowRun,
)
from .exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenExportError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenWorkflowError,
)

__all__ = [
    "VideoGenClient",
    "ScriptToVideoResult",
    "WorkflowRun",
    "ProjectExport",
    "VideoGenError",
    "VideoGenAuthenticationError",
    "VideoGenPermissionError",
    "VideoGenNotFoundError",
    "VideoGenBadRequestError",
    "VideoGenRateLimitError",
    "VideoGenServerError",
    "VideoGenConnectionError",
    "VideoGenWorkflowError",
    "VideoGenExportError",
]
