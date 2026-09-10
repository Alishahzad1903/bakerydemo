"""Persistence for a single article-to-video job.

The job row *is* the idempotency key and the state machine: exactly one job per
page (``OneToOneField``), advanced through the VideoGen build → export lifecycle
by ``bakerydemo.videos.service``. It records the provider ids so a finished MP4
stays retrievable for as long as the article (and therefore this row, via the
``CASCADE`` foreign key) exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db import models


@dataclass(frozen=True)
class VideoStatusDTO:
    """Boundary object handed to the API response schema (python-named attributes)."""

    status: str
    progress_percentage: float
    download_url: str | None
    error: str | None


class VideoJob(models.Model):
    """One video-production job for one blog article."""

    class Status(models.TextChoices):
        # Internal lifecycle. ``PENDING``/``BUILDING``/``EXPORTING`` all surface to
        # the API as "processing"; ``READY`` as "succeeded"; ``FAILED`` as "failed".
        PENDING = "pending", "Pending"
        BUILDING = "building", "Building"
        EXPORTING = "exporting", "Exporting"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    #: Opaque public identifier — this is the ``videoJobId`` in the URL/response.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: One job per page; deleting the article deletes the job (and its downloadability).
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )

    #: The exact narration text sent to VideoGen (title + first intro sentence, capped).
    script = models.TextField()

    # Provider handles, filled in as the job advances.
    workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    project_id = models.CharField(max_length=128, blank=True, default="")
    export_id = models.CharField(max_length=128, blank=True, default="")

    # Result / progress.
    progress_percentage = models.FloatField(default=0.0)
    download_url = models.TextField(blank=True, default="")
    download_url_expires_at = models.BigIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "video job"
        verbose_name_plural = "video jobs"

    def __str__(self) -> str:
        return f"VideoJob {self.pk} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.READY, self.Status.FAILED)

    @property
    def public_status(self) -> str:
        """The three-state status a caller can act on without guessing."""
        if self.status == self.Status.READY:
            return "succeeded"
        if self.status == self.Status.FAILED:
            return "failed"
        return "processing"

    def status_dto(self) -> VideoStatusDTO:
        """Project the row onto the API response contract."""
        return VideoStatusDTO(
            status=self.public_status,
            progress_percentage=round(self.progress_percentage, 1),
            # Only expose a download location once the MP4 is actually ready.
            download_url=self.download_url or None
            if self.status == self.Status.READY
            else None,
            # Only expose an error once the job has actually failed.
            error=self.error_message or "Video production failed"
            if self.status == self.Status.FAILED
            else None,
        )
