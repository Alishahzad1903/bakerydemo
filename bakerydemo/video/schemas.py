"""Response schemas for the article-video API endpoints.

Field names are ``camelCase`` to match the v3 API's JSON conventions and the
top-level field names the contract requires (``videoJobId``, ``status``,
``progressPercentage``, ``downloadUrl``, ``error``).
"""

from __future__ import annotations

from ninja import Schema


class VideoJobCreatedSchema(Schema):
    """Returned when a video production request is accepted."""

    videoJobId: str


class VideoJobStatusSchema(Schema):
    """The state and outcome of a single video production request.

    ``status`` is one of ``processing`` / ``ready`` / ``failed`` so a caller can
    tell, without guessing, whether the video is still being produced, is ready
    to download, or failed.

    * ``downloadUrl`` is populated only when ``status == "ready"``.
    * ``error`` is populated only when ``status == "failed"``.
    """

    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None
