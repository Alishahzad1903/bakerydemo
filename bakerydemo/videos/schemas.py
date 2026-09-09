"""Response schemas for the article-video API.

Field names are written in camelCase directly so the JSON keys match the
contract exactly (``videoJobId``, ``progressPercentage``, ``downloadUrl``,
``error``). Django-Ninja serializes response schema fields verbatim
(``by_alias`` defaults to ``False``), so this needs no alias configuration.
"""

from ninja import Schema


class StartVideoResponse(Schema):
    videoJobId: str


class VideoStatusResponse(Schema):
    videoJobId: str
    # "pending" / "processing" -> still being produced; "succeeded" -> ready to
    # download; "failed" -> production stopped with an error.
    status: str
    progressPercentage: int
    # Where to download the finished MP4; present only once ``status`` is
    # "succeeded".
    downloadUrl: str | None = None
    # What went wrong; present only when ``status`` is "failed".
    error: str | None = None
