"""Persistence for article-to-video jobs.

One :class:`ArticleVideo` row represents the single video for one blog article.
The ``OneToOneField`` to the page is what makes "ask twice, produce once" true:
there can only ever be one job per article, and it is deleted together with the
article (so a ready video stays downloadable for exactly as long as the article
exists, and no longer).
"""

from __future__ import annotations

import uuid

from django.db import models


class VideoStatus(models.TextChoices):
    """Lifecycle of an article video, as seen by an API caller.

    ``PENDING``/``PROCESSING`` are in-progress; ``READY`` and ``FAILED`` are
    terminal. This vocabulary is the site's own — it is a deliberately small
    projection of VideoGen's multi-stage pipeline (workflow run + export) onto
    the three outcomes a caller needs to distinguish.
    """

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class ArticleVideo(models.Model):
    """A narrated video produced for a single published blog article."""

    #: Opaque, non-guessable identifier surfaced to API callers as ``videoJobId``.
    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    #: The article this video belongs to. One video per article; cascade-deleted
    #: with the page so the video never outlives its source article.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_video",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoStatus.choices,
        default=VideoStatus.PENDING,
    )

    #: 0-100 across the whole pipeline (production + export + download).
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    #: The exact narration used, kept for auditing/debugging.
    script = models.TextField(blank=True, default="")

    #: Populated only when ``status == FAILED``; the provider-reported reason.
    error = models.TextField(blank=True, default="")

    #: The finished MP4, stored locally so it stays downloadable independently
    #: of VideoGen's short-lived signed URLs.
    video_file = models.FileField(
        upload_to="article_videos/",
        blank=True,
        null=True,
    )

    # --- Provider bookkeeping (opaque VideoGen identifiers) ------------------
    provider_project_id = models.CharField(max_length=128, blank=True, default="")
    provider_workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    provider_export_id = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video"
        verbose_name_plural = "article videos"

    def __str__(self) -> str:
        return f"ArticleVideo(page={self.page_id}, status={self.status})"

    @property
    def is_terminal(self) -> bool:
        """True once the job has finished, whether it succeeded or failed."""
        return self.status in (VideoStatus.READY, VideoStatus.FAILED)

    @property
    def is_ready(self) -> bool:
        return self.status == VideoStatus.READY and bool(self.video_file)
