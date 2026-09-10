"""Response schemas for the page-video API endpoints.

Field names are intentionally camelCase so they serialise to the exact
top-level keys the API contract requires (``videoJobId``, ``progressPercentage``,
``downloadUrl``, ``error``), matching the shape callers expect.
"""

from __future__ import annotations

from ninja import Schema


class VideoJobCreateResponse(Schema):
    """Returned when production is requested for an article."""

    videoJobId: str
    pageId: int
    status: str


class VideoJobStatusResponse(Schema):
    """The state and outcome of one video production request."""

    videoJobId: str
    pageId: int
    status: str
    progressPercentage: int
    #: Present (non-null) only once ``status == "ready"``.
    downloadUrl: str | None = None
    #: Present (non-null) only once ``status == "failed"``.
    error: str | None = None
