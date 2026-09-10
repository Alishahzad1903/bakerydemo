"""Response schemas for the article-video endpoints.

Field names are snake_case in Python but serialised as camelCase (the API
contract: ``videoJobId``, ``progressPercentage``, ``downloadUrl``). Routes pass
``by_alias=True`` so these aliases are what goes on the wire.
"""

from __future__ import annotations

from ninja import Schema
from pydantic import AliasGenerator, ConfigDict
from pydantic.alias_generators import to_camel


class _CamelSchema(Schema):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=AliasGenerator(
            validation_alias=to_camel,
            serialization_alias=to_camel,
        ),
    )


class StartVideoResponse(_CamelSchema):
    """Returned by ``POST .../video/``. Identifies the work via ``videoJobId``."""

    video_job_id: str
    status: str
    progress_percentage: int


class VideoStatusResponse(_CamelSchema):
    """Returned by ``GET .../video/{videoJobId}/``.

    ``status`` distinguishes producing / ready / failed. ``downloadUrl`` is set
    only when the MP4 is ready; ``error`` is set only when the job failed.
    """

    video_job_id: str
    status: str
    progress_percentage: int
    download_url: str | None = None
    error: str | None = None
