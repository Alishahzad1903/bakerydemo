"""Persistence for article video-production jobs.

A :class:`VideoJob` tracks one request to turn a published article into a
narrated MP4. It is the source of truth the status endpoint reads, and it holds
the finished MP4 locally so the download stays available for as long as the
article exists (VideoGen's own signed URLs expire; a locally stored copy does
not).
"""

from __future__ import annotations

import uuid

from django.db import models


def _video_upload_to(instance: VideoJob, filename: str) -> str:
    # Namespaced by page then job so files never collide and are easy to reason
    # about on disk.
    return f"article_videos/{instance.page_id}/{instance.id}.mp4"


class VideoJob(models.Model):
    """One article-to-video production request and its outcome."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    #: Terminal statuses — no further state transitions occur.
    TERMINAL = frozenset({Status.SUCCEEDED, Status.FAILED})
    #: Statuses that represent a live, non-refundable production (blocks a
    #: duplicate, re-billed production for the same article).
    ACTIVE = frozenset({Status.PENDING, Status.RUNNING, Status.SUCCEEDED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # CASCADE: the video only exists to serve its article. When the article is
    # deleted the job (and its download endpoint) goes with it.
    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    #: The verbatim narration script sent to VideoGen (article's own words).
    script = models.TextField(blank=True)

    #: Provider identifiers, captured as the production progresses.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)
    export_file_id = models.CharField(max_length=255, blank=True)

    #: The finished MP4, stored locally so it persists beyond signed-URL expiry.
    video_file = models.FileField(upload_to=_video_upload_to, blank=True, null=True)

    #: Populated when ``status == FAILED``.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["page", "status"]),
        ]

    def __str__(self) -> str:
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    def mark_failed(self, message: str) -> None:
        self.status = self.Status.FAILED
        self.error = message
        self.save(update_fields=["status", "error", "updated_at"])
