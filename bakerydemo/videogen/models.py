"""Persistence for article-to-video jobs.

A :class:`VideoJob` records one request to turn a published blog article into a
video, and everything needed to follow it through to a downloadable MP4:

* the ``uuid`` exposed to API callers as ``videoJobId``;
* a one-to-one link to the source page (so at most one video — and one bill —
  exists per article, giving idempotency);
* the exact narration script that was (or will be) narrated;
* the VideoGen identifiers gathered along the way (workflow run, project, export)
  so the flow is resumable and never re-created / re-billed;
* the terminal outcome: the stored MP4 (``video_file``) or the ``error``.
"""

from __future__ import annotations

import uuid

from django.db import models


def video_upload_to(instance: VideoJob, filename: str) -> str:
    return f"videogen/{instance.uuid}.mp4"


class VideoJobStatus(models.TextChoices):
    # A caller can tell, without guessing, which of these a job is in:
    PENDING = "pending", "Pending"  # accepted, not started yet
    PROCESSING = "processing", "Processing"  # being produced by VideoGen
    READY = "ready", "Ready"  # finished MP4 is downloadable
    FAILED = "failed", "Failed"  # something went wrong; see ``error``


#: Non-terminal statuses — a job in one of these may still make progress.
ACTIVE_STATUSES = (VideoJobStatus.PENDING, VideoJobStatus.PROCESSING)


class VideoJob(models.Model):
    uuid = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    # One video per article: the OneToOne makes "ask twice → same job" a
    # database-enforced invariant rather than an application convention.
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
    )

    narration_script = models.TextField(
        help_text="The article's own words that are narrated, verbatim."
    )
    status = models.CharField(
        max_length=16,
        choices=VideoJobStatus.choices,
        default=VideoJobStatus.PENDING,
        db_index=True,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # VideoGen identifiers, filled in as the flow advances. Their presence is
    # what makes the flow resumable without producing a second video.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)
    export_file_id = models.CharField(max_length=255, blank=True)

    # The finished MP4, stored in the site's own media storage so it stays
    # retrievable through the site for as long as the article exists.
    video_file = models.FileField(upload_to=video_upload_to, blank=True, null=True)

    error = models.TextField(blank=True)
    error_code = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "video job"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VideoJob {self.uuid} for page {self.page_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (VideoJobStatus.READY, VideoJobStatus.FAILED)

    def download_url(self, request=None) -> str | None:
        """Absolute URL to the stored MP4, or ``None`` if not ready.

        Built from the site's own media storage, so it stays valid for as long
        as the file (and therefore the article) exists — independent of any
        provider signed-URL expiry.
        """
        if self.status != VideoJobStatus.READY or not self.video_file:
            return None
        url = self.video_file.url
        if request is not None:
            return request.build_absolute_uri(url)
        return url
