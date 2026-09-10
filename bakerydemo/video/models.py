"""
Persistence for article → video jobs.

A :class:`VideoJob` is the durable record of one request to turn a published
article into a narrated video. It is the integration's source of truth:

* It enforces **idempotency** – a one-to-one link to the page means asking for a
  video twice in a row reuses the same job (one video produced, billed once).
* It tracks the **VideoGen identifiers** (workflow run, project, export) needed
  to follow the asynchronous build and export to completion.
* It stores the **finished MP4** in the site's own media storage so the video
  stays downloadable through the API for as long as the article exists,
  independent of VideoGen's signed-URL expiry.
"""

from __future__ import annotations

import uuid

from django.db import models


class VideoJob(models.Model):
    """One article-to-video production request and its outcome."""

    class Status(models.TextChoices):
        # "processing" covers the whole in-flight pipeline (submitting, the
        # VideoGen build, and the export) so callers have a single "still being
        # produced" signal, as required by the status contract.
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # One job per page → idempotent production and a natural uniqueness guard.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PROCESSING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # The exact narration sent to VideoGen (the article's own words).
    script = models.TextField()

    # VideoGen identifiers, populated as the pipeline advances.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)
    export_file_id = models.CharField(max_length=255, blank=True)

    # The finished MP4, stored in the site's own media storage once ready.
    video_file = models.FileField(
        upload_to="article_videos/",
        blank=True,
        null=True,
    )

    # Populated with the provider-reported reason when ``status == FAILED``.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "video job"
        verbose_name_plural = "video jobs"
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.READY, self.Status.FAILED)

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.READY and bool(self.video_file)
