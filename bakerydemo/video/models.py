"""
Persistence for article videos.

One :class:`ArticleVideo` row tracks the single video produced for a page. The
row is the source of truth the API reports from, and it makes asking for a video
twice in a row idempotent: the second request finds the existing row and does
not start (or bill for) a second production.

The relationship to :class:`~wagtail.models.Page` is a one-to-one with
``on_delete=CASCADE`` so a finished video stays downloadable for exactly as long
as the article exists, and is cleaned up automatically when the article is
deleted.
"""

from __future__ import annotations

import uuid

from django.db import models


class ArticleVideo(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    #: Public identifier returned to callers as ``videoJobId``.
    job_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_video",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    #: 0-100. Always 100 once the video is ready.
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    #: The exact narration that was (or will be) synthesised.
    script = models.TextField(blank=True, default="")
    #: Human-readable failure reason; empty unless ``status == FAILED``.
    error = models.TextField(blank=True, default="")

    # Provider-side identifiers, stored for traceability and for diagnosing a
    # failure from what the provider reported (never re-producing to find out).
    workflow_run_id = models.CharField(max_length=255, blank=True, default="")
    project_id = models.CharField(max_length=255, blank=True, default="")
    export_id = models.CharField(max_length=255, blank=True, default="")
    export_file_id = models.CharField(max_length=255, blank=True, default="")

    #: The finished MP4, stored on the site so it stays retrievable.
    video_file = models.FileField(
        upload_to="article_videos/", blank=True, null=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video"
        verbose_name_plural = "article videos"

    def __str__(self) -> str:
        return f"ArticleVideo(page={self.page_id}, status={self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.READY, self.Status.FAILED)

    def mark_failed(self, message: str) -> None:
        self.status = self.Status.FAILED
        self.error = message or "Video production failed."
        self.save(update_fields=["status", "error", "updated_at"])
