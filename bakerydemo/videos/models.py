"""Persistence for article-video production jobs.

A :class:`VideoJob` tracks one request to turn an article into a video, from
"queued" through to a downloadable MP4 (or a failure). It lives beside the page
it belongs to and is deleted with it, so a finished video stays retrievable for
as long as the article exists and no longer.
"""

from __future__ import annotations

import uuid

from django.db import models


class JobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


#: Statuses in which a job is considered "active" (still counts against the
#: one-active-job-per-page rule). A failed job does not, so a genuine failure
#: may be retried without ever producing two videos for one article.
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.SUCCEEDED)


class VideoJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )
    status = models.CharField(
        max_length=20, choices=JobStatus.choices, default=JobStatus.PENDING
    )
    progress_percentage = models.FloatField(default=0.0)

    # The narration actually sent to VideoGen (title + first sentence of intro).
    script = models.TextField(blank=True)

    # Provider handles, used to poll progress and re-sign the download URL.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)

    # Cached signed MP4 URL and its expiry (Unix seconds), refreshed on demand.
    download_url = models.TextField(blank=True)
    download_url_expires_at = models.BigIntegerField(null=True, blank=True)

    # Populated only on failure.
    error_message = models.TextField(blank=True)
    error_code = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one non-failed job per page. This is what guarantees a
            # second "make a video" request does not start a second (billable)
            # production for the same article.
            models.UniqueConstraint(
                fields=["page"],
                condition=~models.Q(status=JobStatus.FAILED),
                name="unique_active_video_job_per_page",
            )
        ]

    def __str__(self) -> str:
        return f"VideoJob {self.pk} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)
