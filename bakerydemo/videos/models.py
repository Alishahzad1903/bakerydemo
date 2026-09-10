"""Persistence for article video jobs.

A :class:`VideoJob` records one request to turn a blog article into a video and
tracks it through to a downloadable MP4. It is deliberately additive: nothing
here touches the existing page, blog or image models beyond a foreign key.
"""

from __future__ import annotations

import uuid

from django.db import models


class VideoJob(models.Model):
    """One "produce a video for this article" request and its outcome.

    The primary key doubles as the public ``videoJobId``. A job is tied to its
    page with ``on_delete=CASCADE`` so a produced video stays retrievable for
    exactly as long as the article exists - and no longer.
    """

    class Status(models.TextChoices):
        # Accepted, provider work not yet started.
        PENDING = "pending", "Pending"
        # Provider is generating and/or exporting the video.
        PROCESSING = "processing", "Processing"
        # Finished; the MP4 is downloadable.
        READY = "ready", "Ready"
        # Terminally failed; ``error`` explains why.
        FAILED = "failed", "Failed"

    #: Statuses that mean "do not start another billable video for this page".
    #: A brand new request only proceeds when no job in one of these states
    #: already exists for the page, which is what makes a repeated request
    #: idempotent and un-billed.
    ACTIVE_STATUSES = (Status.PENDING, Status.PROCESSING, Status.READY)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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

    # The exact narration sent to the provider, kept for auditing/debugging.
    script = models.TextField(blank=True)

    # Provider-side identifiers, populated as the pipeline advances.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)

    # Signed URL to the finished MP4 (refreshed on read while the job is ready).
    download_url = models.URLField(max_length=2048, blank=True)

    # Human-readable failure reason when ``status == FAILED``.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # At most one non-failed job per page at the database level. This is
            # the backstop for "asking twice must not produce two videos": a
            # second request finds the existing active job instead of starting
            # (and billing) another.
            models.UniqueConstraint(
                fields=["page"],
                condition=~models.Q(status="failed"),
                name="unique_active_video_job_per_page",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"VideoJob {self.id} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.READY, self.Status.FAILED)
