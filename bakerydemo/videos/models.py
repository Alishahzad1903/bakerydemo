"""Persistence for video-production jobs.

A :class:`VideoJob` is the site's record of one request to turn an article into
a video. It is keyed one-to-one on the page so that asking for a video twice in
a row for the same article reuses the existing job — no second video is
produced and the account is not billed twice. Because it cascades on page
deletion, a finished video stays retrievable for exactly as long as the article
exists.
"""

from __future__ import annotations

import uuid

from django.db import models


class VideoJob(models.Model):
    """A single article-to-video production request and its outcome."""

    class Status(models.TextChoices):
        # The three states a caller must be able to distinguish without
        # guessing: still being produced, ready to download, or failed.
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # One job per page enforces idempotency and prevents double billing.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PROCESSING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # The exact narration sent to VideoGen, built from the article's own words.
    script = models.TextField()

    # Provider-side identifiers, populated as the pipeline advances.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)

    # Where the finished MP4 can be downloaded (a signed provider URL). Empty
    # until the export succeeds.
    download_url = models.URLField(max_length=2000, blank=True)

    # Human-readable failure reason; empty unless status is FAILED.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "video job"
        verbose_name_plural = "video jobs"

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"VideoJob({self.id}, page={self.page_id}, status={self.status})"
