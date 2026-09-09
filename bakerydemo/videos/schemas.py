from __future__ import annotations

from ninja import Schema


class StartVideoResponse(Schema):
    """Response body of ``POST .../video/`` — identifies the accepted work."""

    videoJobId: str


class VideoStatusResponse(Schema):
    """Response body of ``GET .../video/{videoJobId}/``.

    ``status`` lets a caller tell, without guessing, whether the video is still
    being produced (``pending``/``processing``), ready (``ready``), or failed
    (``failed``). ``downloadUrl`` is populated only when ready; ``error`` only
    when failed.
    """

    videoJobId: str
    status: str
    progressPercentage: float
    downloadUrl: str | None = None
    error: str | None = None
