"""Persistence for article-video production jobs.

A :class:`VideoJob` is the durable record of one request to turn an article
into a shareable video. It tracks the job through production, stores the
finished MP4 in the site's media storage (so it stays downloadable for as long
as the article exists), and enforces that a single article never has two
concurrent/redundant videos in flight.
"""

from __future__ import annotations

import uuid

from django.db import models


class JobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


# Internal states that count as "there is already a video for this article" for
# idempotency purposes. A failed job does not, so a fresh request may retry.
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.SUCCEEDED)


def export_upload_path(instance: VideoJob, filename: str) -> str:
    return f"videogen_exports/{instance.pk}.mp4"


class VideoJob(models.Model):
    """One request to produce a video for a single article (page)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
        help_text="The article this video was produced from.",
    )

    status = models.CharField(
        max_length=16,
        choices=JobStatus.choices,
        default=JobStatus.PENDING,
        db_index=True,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    # The exact narration handed to the provider, kept for auditability.
    script = models.TextField(blank=True, default="")

    # The finished MP4, stored in the site's media storage.
    video_file = models.FileField(upload_to=export_upload_path, blank=True, null=True)

    # Provider-side identifiers, for traceability / support.
    provider_workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    provider_project_id = models.CharField(max_length=128, blank=True, default="")
    provider_export_id = models.CharField(max_length=128, blank=True, default="")
    provider_export_file_id = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one non-failed job per article: guarantees "asking twice
            # in a row does not produce two videos / bill twice".
            models.UniqueConstraint(
                fields=["page"],
                condition=models.Q(status__in=[s.value for s in ACTIVE_STATUSES]),
                name="videogen_one_active_job_per_page",
            )
        ]

    def __str__(self) -> str:
        return f"VideoJob {self.pk} for page {self.page_id} ({self.status})"

    # -- API-facing view -----------------------------------------------------

    @property
    def api_status(self) -> str:
        """Coarse, caller-facing status: processing / ready / failed."""
        if self.status == JobStatus.SUCCEEDED:
            return "ready"
        if self.status == JobStatus.FAILED:
            return "failed"
        return "processing"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def has_video(self) -> bool:
        return bool(self.video_file)

    # -- state transitions ---------------------------------------------------

    def mark_processing(self, **provider_ids) -> None:
        self.status = JobStatus.PROCESSING
        for field, value in provider_ids.items():
            if value:
                setattr(self, field, value)
        self.save(
            update_fields=["status", "updated_at", *provider_ids.keys()]
        )

    def set_progress(self, percentage: float) -> None:
        clamped = max(0, min(100, int(round(percentage))))
        if clamped == self.progress_percentage:
            return
        self.progress_percentage = clamped
        self.save(update_fields=["progress_percentage", "updated_at"])

    def mark_succeeded(self) -> None:
        self.status = JobStatus.SUCCEEDED
        self.progress_percentage = 100
        self.error = ""
        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "error",
                "video_file",
                "updated_at",
            ]
        )

    def mark_failed(self, error: str) -> None:
        self.status = JobStatus.FAILED
        self.error = (error or "Video production failed.")[:4000]
        self.save(update_fields=["status", "error", "updated_at"])
