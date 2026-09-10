"""Persistence for article-to-video jobs.

An :class:`ArticleVideo` records one request to turn a published blog article into
a narrated MP4. There is **at most one** ``ArticleVideo`` per page (a ``OneToOne``
to ``wagtailcore.Page``): asking for a video twice in a row returns the same row,
which is how the integration guarantees it never produces or bills a second video.

Because the row is tied to the page with ``on_delete=CASCADE``, a finished video
stays retrievable through the API for as long as the article exists.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class VideoStatus(models.TextChoices):
    """The three states an API consumer can observe, unambiguously."""

    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


# VideoGen download URLs are signed and valid for 7 days; re-sign well before then.
DOWNLOAD_URL_REFRESH_AFTER = timedelta(days=6)


class ArticleVideo(models.Model):
    """A single video-production job for one blog article."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # One job per page enforces idempotency and prevents double-billing.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_video",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoStatus.choices,
        default=VideoStatus.PROCESSING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    # The narration actually sent to VideoGen (title + first sentence of intro).
    script = models.TextField(blank=True, default="")

    # Provider-side identifiers, populated as the pipeline progresses.
    workflow_run_id = models.CharField(max_length=255, blank=True, default="")
    project_id = models.CharField(max_length=255, blank=True, default="")
    export_id = models.CharField(max_length=255, blank=True, default="")

    # Cached signed download URL and when it was last (re-)signed.
    download_url = models.TextField(blank=True, default="")
    download_url_signed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "article video"
        verbose_name_plural = "article videos"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"ArticleVideo({self.id}, page={self.page_id}, status={self.status})"

    # -- state helpers -----------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in {VideoStatus.READY, VideoStatus.FAILED}

    def set_progress(self, percentage: int) -> None:
        self.progress_percentage = max(0, min(100, int(percentage)))
        self.save(update_fields=["progress_percentage", "updated_at"])

    def mark_failed(self, message: str) -> None:
        self.status = VideoStatus.FAILED
        self.error = message
        self.save(update_fields=["status", "error", "updated_at"])

    def mark_ready(self, download_url: str) -> None:
        self.status = VideoStatus.READY
        self.progress_percentage = 100
        self.error = ""
        self.download_url = download_url
        self.download_url_signed_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "error",
                "download_url",
                "download_url_signed_at",
                "updated_at",
            ]
        )

    @property
    def download_url_is_stale(self) -> bool:
        """True when the cached signed URL should be re-fetched from VideoGen."""
        if not self.download_url or self.download_url_signed_at is None:
            return True
        return timezone.now() - self.download_url_signed_at > DOWNLOAD_URL_REFRESH_AFTER
