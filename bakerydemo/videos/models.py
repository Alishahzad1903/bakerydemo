"""Persistence for article video-production jobs."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class VideoJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


#: Non-terminal statuses — a job in one of these is still being produced.
IN_PROGRESS_STATUSES = (VideoJobStatus.PENDING, VideoJobStatus.PROCESSING)


class VideoJob(models.Model):
    """One request to turn a blog article into a shareable narrated video.

    The job's lifetime is tied to the article: the foreign key cascades, so a
    finished video stays retrievable for as long as the article exists and is
    removed with it. A partial unique constraint keeps at most one *active*
    (non-failed) job per page, which is what makes repeat requests idempotent —
    asking twice never produces or bills a second video.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )

    # The narration built from the article's own words (title + first sentence
    # of the introduction). Stored for auditability and idempotency.
    script = models.TextField()

    status = models.CharField(
        max_length=16,
        choices=VideoJobStatus.choices,
        default=VideoJobStatus.PENDING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # VideoGen provider handles, captured as the job progresses.
    workflow_run_id = models.CharField(max_length=64, blank=True)
    project_id = models.CharField(max_length=64, blank=True)
    export_id = models.CharField(max_length=64, blank=True)
    export_file_id = models.CharField(max_length=64, blank=True)

    # Cached signed download URL for the finished MP4 (re-signed on demand).
    download_url = models.TextField(blank=True)
    download_url_expires_at = models.BigIntegerField(null=True, blank=True)

    # Populated with the provider's reported reason when a job fails.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["page"],
                condition=~Q(status="failed"),
                name="unique_active_video_job_per_page",
            )
        ]

    def __str__(self):
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_in_progress(self) -> bool:
        return self.status in IN_PROGRESS_STATUSES

    @property
    def is_ready(self) -> bool:
        return self.status == VideoJobStatus.READY

    @property
    def is_failed(self) -> bool:
        return self.status == VideoJobStatus.FAILED

    def download_url_is_fresh(self, min_remaining_seconds: int = 3600) -> bool:
        """Whether the cached signed URL is present and not about to expire."""
        if not self.download_url:
            return False
        if self.download_url_expires_at is None:
            # No expiry known — treat as needing a refresh to be safe.
            return False
        now = int(timezone.now().timestamp())
        return self.download_url_expires_at - now > min_remaining_seconds
