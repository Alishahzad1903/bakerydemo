"""Data model for article-to-video jobs."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class VideoJobStatus(models.TextChoices):
    """Lifecycle of a single video-production request.

    The four values map cleanly onto the three outcomes an API caller must be
    able to distinguish without guessing:

    * ``PENDING`` / ``PROCESSING`` -> still being produced
    * ``READY``                    -> ready to download
    * ``FAILED``                   -> failed (carries an error message)
    """

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class VideoJob(models.Model):
    """One request to turn an article into a narrated video.

    A job owns the whole pipeline: VideoGen workflow run -> project export ->
    the MP4 stored on the site. The finished file is stored locally so it stays
    downloadable for as long as the article exists, independent of VideoGen's
    7-day signed-URL expiry.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoJobStatus.choices,
        default=VideoJobStatus.PENDING,
        db_index=True,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")

    # The narration actually sent to VideoGen (the article's own words).
    script = models.TextField(blank=True, default="")

    # Provider-side references, captured as the pipeline progresses.
    workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    project_id = models.CharField(max_length=128, blank=True, default="")
    export_id = models.CharField(max_length=128, blank=True, default="")

    # The finished MP4, stored on the site (durable, unlike the signed URL).
    video_file = models.FileField(upload_to="article_videos/", blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one non-failed job per page. A page can be retried only
            # after its previous job has failed, so asking for a video twice in
            # a row returns the same job and never bills twice.
            models.UniqueConstraint(
                fields=["page"],
                condition=~Q(status="failed"),
                name="one_active_video_job_per_page",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (VideoJobStatus.READY, VideoJobStatus.FAILED)

    @property
    def is_ready(self) -> bool:
        return self.status == VideoJobStatus.READY and bool(self.video_file)
