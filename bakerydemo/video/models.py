"""Persistence for article-video production jobs.

A :class:`VideoJob` is the site's record of one request to turn an article into
a video. Exactly one job exists per page (``OneToOneField``), which is what makes
asking for a video twice in a row idempotent: the second request finds the
existing job instead of producing — and paying for — a second video.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class VideoJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


# Statuses at which a job is considered "live" — either still being produced or
# already finished successfully. A POST for a page with a live job returns that
# job unchanged (idempotent). Only a FAILED job may be retried.
ACTIVE_STATUSES = frozenset(
    {VideoJobStatus.PENDING, VideoJobStatus.PROCESSING, VideoJobStatus.READY}
)


class VideoJob(models.Model):
    """One video-production request for one page."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
        help_text="The article this video was produced from.",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoJobStatus.choices,
        default=VideoJobStatus.PENDING,
        db_index=True,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # The exact narration that was sent to VideoGen, kept for auditability.
    script = models.TextField(blank=True)

    # VideoGen identifiers, recorded as the pipeline advances so a job can be
    # traced back to the provider's own records.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)
    export_file_id = models.CharField(max_length=255, blank=True)

    # The finished MP4, downloaded once from VideoGen and stored on the site so
    # it stays retrievable for as long as the article exists — independent of
    # VideoGen's own 7-day signed-URL expiry.
    video_file = models.FileField(upload_to="article_videos/", blank=True, null=True)

    # A human-readable explanation, populated only when ``status`` is FAILED.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_ready(self) -> bool:
        return self.status == VideoJobStatus.READY

    @property
    def is_failed(self) -> bool:
        return self.status == VideoJobStatus.FAILED

    def reset_for_retry(self, script: str) -> None:
        """Reset a previously failed job so it can be produced again.

        Reuses the same row (and therefore the one-per-page guarantee) rather
        than creating a duplicate. Crucially, if the VideoGen workflow already
        produced a project (``project_id`` is set), that work — the actual, paid
        video — is *preserved* and only the export side is cleared, so a retry
        re-exports the existing project instead of producing a second video.
        """
        self.status = VideoJobStatus.PENDING
        self.progress_percentage = 0
        self.export_id = ""
        self.export_file_id = ""
        self.error = ""
        if self.video_file:
            self.video_file.delete(save=False)
        self.video_file = None

        if not self.project_id:
            # The workflow never completed — nothing was produced, so start over
            # from scratch (and refresh the script in case the article changed).
            self.script = script
            self.workflow_run_id = ""

        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "script",
                "workflow_run_id",
                "export_id",
                "export_file_id",
                "error",
                "video_file",
                "updated_at",
            ]
        )

    def mark_failed(self, message: str) -> None:
        self.status = VideoJobStatus.FAILED
        self.error = message
        self.save(update_fields=["status", "error", "updated_at"])

    def touch(self) -> None:
        self.updated_at = timezone.now()
