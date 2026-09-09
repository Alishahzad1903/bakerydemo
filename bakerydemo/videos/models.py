"""Persistence for article video-generation jobs."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class VideoJobStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    PROCESSING = "processing", _("Processing")
    READY = "ready", _("Ready")
    FAILED = "failed", _("Failed")


#: Statuses that represent an in-flight or already-produced video. While a job
#: for a page is in one of these states, a second request must reuse it rather
#: than produce (and pay for) another video.
ACTIVE_STATUSES = (
    VideoJobStatus.PENDING,
    VideoJobStatus.PROCESSING,
    VideoJobStatus.READY,
)


def video_upload_path(instance: VideoJob, filename: str) -> str:
    return f"article_videos/{instance.page_id}/{instance.pk}.mp4"


class VideoJob(models.Model):
    """One request to turn a published article into a narrated MP4.

    The primary key is a UUID and is what the API exposes as ``videoJobId``.
    The job is tied to its article via a ``CASCADE`` foreign key, so a finished
    video stays retrievable for exactly as long as the article exists.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoJobStatus.choices,
        default=VideoJobStatus.PENDING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    # The exact narration used, captured for auditing / reproducibility.
    script = models.TextField(blank=True, default="")

    # The finished video, downloaded from VideoGen and stored on the site so it
    # does not depend on VideoGen's expiring signed URLs.
    video_file = models.FileField(
        upload_to=video_upload_path, blank=True, null=True
    )

    # Provider-side identifiers, kept for traceability and debugging.
    videogen_workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    videogen_project_id = models.CharField(max_length=128, blank=True, default="")
    videogen_export_id = models.CharField(max_length=128, blank=True, default="")
    videogen_export_file_id = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Guarantees at most one in-flight/ready job per article at the
            # database level. This is the real guard against producing (and
            # being billed for) two videos for the same article — SQLite does
            # not honour ``select_for_update`` row locks, so the constraint,
            # not the lock, enforces idempotency under concurrent requests.
            models.UniqueConstraint(
                fields=["page"],
                condition=models.Q(status__in=list(ACTIVE_STATUSES)),
                name="videos_one_active_job_per_page",
            )
        ]

    def __str__(self) -> str:
        return f"VideoJob {self.pk} for page {self.page_id} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in (VideoJobStatus.READY, VideoJobStatus.FAILED)
