"""Persistence for article video jobs.

An :class:`ArticleVideo` row records one request to turn a blog article into a
video, and tracks it through to a downloadable MP4. The row is tied to its page
with ``on_delete=CASCADE``: the video is retrievable for exactly as long as the
article exists.
"""

from __future__ import annotations

import uuid

from django.db import models


class ArticleVideo(models.Model):
    """A single "turn this article into a video" request and its outcome."""

    class Status(models.TextChoices):
        # The three outcomes a caller must be able to tell apart: still being
        # produced, ready to download, or failed.
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    # Public, opaque identifier returned to callers as ``videoJobId``.
    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_videos",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PROCESSING,
    )
    progress_percentage = models.FloatField(default=0.0)
    error = models.TextField(blank=True, default="")

    # The exact narration we sent (title + first sentence of the intro).
    script = models.TextField()

    # VideoGen-side identifiers, filled in as the job advances.
    workflow_run_id = models.CharField(max_length=255, blank=True, default="")
    project_id = models.CharField(max_length=255, blank=True, default="")
    export_id = models.CharField(max_length=255, blank=True, default="")

    # Last known signed MP4 URL. Refreshed on read, so it never goes stale.
    download_url = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["page", "status"]),
        ]

    def __str__(self) -> str:
        return f"ArticleVideo(page={self.page_id}, job={self.job_id}, status={self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.READY, self.Status.FAILED}
