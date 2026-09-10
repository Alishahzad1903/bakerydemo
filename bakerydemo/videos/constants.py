"""Shared constants for the article-video feature."""

from __future__ import annotations

from django.db import models


class VideoJobStatus(models.TextChoices):
    """Internal lifecycle states of a :class:`~bakerydemo.videos.models.VideoJob`."""

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


#: Internal states that count as "in flight or already produced" — a page may
#: have at most one job in one of these states, which is what makes asking for a
#: video twice in a row idempotent (no second video, no second bill).
ACTIVE_STATUSES = (
    VideoJobStatus.PENDING,
    VideoJobStatus.PROCESSING,
    VideoJobStatus.SUCCEEDED,
)

#: Public, unambiguous status values exposed on the API. A caller can tell,
#: without guessing, whether the video is still being produced, is ready to
#: download, or failed.
PUBLIC_STATUS_PROCESSING = "processing"
PUBLIC_STATUS_READY = "ready"
PUBLIC_STATUS_FAILED = "failed"

PUBLIC_STATUS_BY_INTERNAL = {
    VideoJobStatus.PENDING: PUBLIC_STATUS_PROCESSING,
    VideoJobStatus.PROCESSING: PUBLIC_STATUS_PROCESSING,
    VideoJobStatus.SUCCEEDED: PUBLIC_STATUS_READY,
    VideoJobStatus.FAILED: PUBLIC_STATUS_FAILED,
}
