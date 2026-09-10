"""Response schemas for the video endpoints.

The task fixes the exact top-level field names (``videoJobId``, ``status``,
``progressPercentage``, ``downloadUrl``, ``error``). The v3 API is otherwise
snake_case, so we declare snake_case fields with camelCase aliases and emit them
with ``by_alias=True`` on each operation.
"""

from __future__ import annotations

from ninja import Schema
from pydantic import ConfigDict, Field


class StartVideoResponse(Schema):
    # populate_by_name lets us return objects keyed by the python field name while
    # still emitting the camelCase alias (via by_alias=True on the operation).
    model_config = ConfigDict(populate_by_name=True)

    video_job_id: str = Field(alias="videoJobId")


class VideoStatusResponse(Schema):
    model_config = ConfigDict(populate_by_name=True)

    #: One of "processing", "succeeded", "failed" — tells the caller, without
    #: guessing, whether the video is still being produced, ready, or failed.
    status: str
    progress_percentage: float = Field(alias="progressPercentage")
    #: Where to download the finished MP4. Present only once ``status`` is "succeeded".
    download_url: str | None = Field(default=None, alias="downloadUrl")
    #: What went wrong. Present only once ``status`` is "failed".
    error: str | None = None
