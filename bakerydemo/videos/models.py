"""Data model for article-to-video jobs.

A :class:`VideoJob` records one request to turn a published article into a
narrated video, tracks its progress through VideoGen, and — once finished —
owns the downloaded MP4 so it stays retrievable through the site for as long as
the article exists.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .constants import PUBLIC_STATUS_BY_INTERNAL, VideoJobStatus


def video_upload_to(instance: VideoJob, filename: str) -> str:
    return f"article_videos/{instance.pk}.mp4"


class VideoJob(models.Model):
    """One request to produce a shareable video for a single article."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The article this video is for. Deleting the article deletes its videos,
    # which is exactly the retention we want: a finished video stays available
    # for as long as — and no longer than — the article exists.
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

    # The narration script actually sent to VideoGen (built from the article's
    # own words), kept for transparency and debugging.
    script = models.TextField(blank=True)

    # Human-readable failure reason, populated when ``status == FAILED``.
    error_message = models.TextField(blank=True)

    # Provider-side identifiers, useful for support/diagnosis.
    videogen_workflow_run_id = models.CharField(max_length=128, blank=True)
    videogen_project_id = models.CharField(max_length=128, blank=True)
    videogen_export_id = models.CharField(max_length=128, blank=True)
    videogen_file_id = models.CharField(max_length=128, blank=True)

    # The finished MP4, downloaded into the site's own storage.
    video_file = models.FileField(upload_to=video_upload_to, blank=True, null=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one non-failed job per page. This is what makes a repeat
            # request idempotent: a second POST finds the existing job instead
            # of producing (and billing) a second video.
            models.UniqueConstraint(
                fields=["page"],
                condition=~models.Q(status=VideoJobStatus.FAILED),
                name="videos_one_active_job_per_page",
            ),
        ]
        indexes = [
            models.Index(fields=["page", "status"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"VideoJob {self.pk} for page {self.page_id} ({self.status})"

    # -- derived state -------------------------------------------------------

    @property
    def public_status(self) -> str:
        """The unambiguous status exposed on the API."""
        return PUBLIC_STATUS_BY_INTERNAL.get(VideoJobStatus(self.status), self.status)

    @property
    def is_ready(self) -> bool:
        return self.status == VideoJobStatus.SUCCEEDED and bool(self.video_file)

    @property
    def is_failed(self) -> bool:
        return self.status == VideoJobStatus.FAILED
