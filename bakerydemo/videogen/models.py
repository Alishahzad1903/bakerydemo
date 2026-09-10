"""Persistence for article video production jobs.

One :class:`ArticleVideo` row represents the single video produced for a page.
The ``page`` relation is one-to-one, which is what makes producing a video
idempotent: asking twice for the same article resolves to the same row and
never starts (or bills) a second production.
"""

from __future__ import annotations

import uuid

from django.db import models
from wagtail.models import Page


class ArticleVideo(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    #: Public identifier surfaced to API callers as ``videoJobId``.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: One video per page - drives idempotency and cascades on page deletion.
    page = models.OneToOneField(
        Page,
        on_delete=models.CASCADE,
        related_name="article_video",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    #: The narration text actually sent to the provider (the article's own words).
    script = models.TextField()

    # Provider-side identifiers, filled in as production progresses.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)

    # Last known signed download URL (re-signed on demand via the provider).
    download_url = models.URLField(max_length=2000, blank=True)
    download_url_expires_at = models.DateTimeField(null=True, blank=True)

    #: Populated when ``status == FAILED`` with what the provider reported.
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    TERMINAL_STATUSES = frozenset({Status.READY, Status.FAILED})

    class Meta:
        verbose_name = "article video"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ArticleVideo(page={self.page_id}, status={self.status})"

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.READY

    @property
    def is_failed(self) -> bool:
        return self.status == self.Status.FAILED

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES
