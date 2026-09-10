"""Persistence for article videos.

One :class:`ArticleVideo` row per page (a ``OneToOneField``) is what makes the
feature idempotent — asking for a video twice for the same article returns the
same row and never starts a second, billable render — and what keeps the
finished MP4 downloadable for as long as the article exists (the row, and the
stored file, are deleted with the page via ``on_delete=CASCADE``).
"""

from __future__ import annotations

import uuid

from django.db import models


def _video_upload_path(instance: ArticleVideo, filename: str) -> str:
    # Keep the stored file name stable and tied to the opaque job id.
    return f"article_videos/{instance.job_id}.mp4"


class ArticleVideo(models.Model):
    """A single VideoGen production for one blog article."""

    class Status(models.TextChoices):
        # "pending" and "processing" both mean "still being produced"; they are
        # kept distinct so callers can see whether work has actually started.
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    TERMINAL_STATUSES = frozenset({Status.SUCCEEDED, Status.FAILED})

    #: Opaque, stable, externally exposed id ("videoJobId").
    job_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )

    #: The article this video belongs to. OneToOne enforces idempotency.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_video",
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    #: The verbatim narration script, built from the article's own words.
    script = models.TextField()

    #: VideoGen identifiers, filled in as the pipeline progresses.
    provider_run_id = models.CharField(max_length=128, blank=True)
    provider_project_id = models.CharField(max_length=128, blank=True)
    provider_export_id = models.CharField(max_length=128, blank=True)
    provider_file_id = models.CharField(max_length=128, blank=True)

    #: The finished MP4, downloaded and served through this site.
    video_file = models.FileField(upload_to=_video_upload_path, blank=True, null=True)
    video_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    #: Failure details, surfaced verbatim on the status endpoint.
    error_message = models.TextField(blank=True)
    error_code = models.CharField(max_length=128, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video"
        verbose_name_plural = "article videos"
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"ArticleVideo(page={self.page_id}, status={self.status})"

    # -- state helpers -----------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.SUCCEEDED and bool(self.video_file)

    def set_progress(self, percentage: int) -> None:
        """Persist a monotonic-ish progress update without racing status."""
        self.progress_percentage = max(0, min(100, int(percentage)))
        self.save(update_fields=["progress_percentage", "updated_at"])

    def mark_processing(self, percentage: int | None = None) -> None:
        self.status = self.Status.PROCESSING
        if percentage is not None:
            self.progress_percentage = max(0, min(100, int(percentage)))
        self.save(update_fields=["status", "progress_percentage", "updated_at"])

    def mark_succeeded(self) -> None:
        self.status = self.Status.SUCCEEDED
        self.progress_percentage = 100
        self.error_message = ""
        self.error_code = ""
        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "error_message",
                "error_code",
                "updated_at",
            ]
        )

    def mark_failed(self, message: str, *, code: str = "") -> None:
        self.status = self.Status.FAILED
        self.error_message = message or "Video production failed."
        self.error_code = code or ""
        self.save(
            update_fields=[
                "status",
                "error_message",
                "error_code",
                "updated_at",
            ]
        )
