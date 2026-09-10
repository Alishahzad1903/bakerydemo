"""Persistence for article videos.

One :class:`ArticleVideo` exists per page (``OneToOneField``). This is what makes
producing a video idempotent — asking twice for the same article reuses the same
row rather than starting (and billing) a second job. The ``CASCADE`` delete ties
the video's lifetime to the article: while the article exists, its video stays
retrievable; when the article is deleted, the record goes with it.
"""

from __future__ import annotations

import uuid

from django.db import models
from wagtail.models import Page


class ArticleVideo(models.Model):
    """A VideoGen-produced video for a single blog article."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    #: Public, opaque job identifier returned to API callers as ``videoJobId``.
    job_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    page = models.OneToOneField(
        Page, on_delete=models.CASCADE, related_name="article_video"
    )
    #: The narration script actually sent to VideoGen (title + first intro sentence).
    script = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    #: Overall completion, 0-100, spanning both the build and export phases.
    progress_percentage = models.FloatField(default=0.0)

    # Provider-side identifiers, populated as the job progresses.
    workflow_run_id = models.CharField(max_length=255, blank=True, default="")
    project_id = models.CharField(max_length=255, blank=True, default="")
    export_id = models.CharField(max_length=255, blank=True, default="")

    #: Last-known signed MP4 URL (refreshed live on read while READY).
    download_url = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video"
        verbose_name_plural = "article videos"

    def __str__(self) -> str:
        return f"ArticleVideo({self.job_id}, page={self.page_id}, {self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in {self.Status.READY, self.Status.FAILED}
