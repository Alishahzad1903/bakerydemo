"""Persistence for VideoGen video-production jobs.

A :class:`VideoJob` records one article-to-video request and its progress
through the provider pipeline. It is keyed one-to-one on the source page so that
asking for a video twice for the same article returns the same job instead of
starting (and paying for) a second video.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class JobStatus(models.TextChoices):
    """Internal lifecycle states of a video job.

    The public API collapses these into ``processing`` / ``ready`` / ``failed``
    via :attr:`VideoJob.public_status`.
    """

    # Row reserved but the provider workflow has not been started yet.
    PENDING = "pending", "Pending"
    # VideoGen is generating the video (workflow run pending/running).
    GENERATING = "generating", "Generating"
    # Generation succeeded; VideoGen is exporting the MP4.
    EXPORTING = "exporting", "Exporting"
    # Export succeeded; the site is fetching and storing the MP4.
    STORING = "storing", "Storing"
    # Finished MP4 is stored and downloadable.
    READY = "ready", "Ready"
    # Something went wrong; ``error`` explains what.
    FAILED = "failed", "Failed"


#: States from which the job can still make forward progress.
ACTIVE_STATUSES = frozenset(
    {
        JobStatus.PENDING,
        JobStatus.GENERATING,
        JobStatus.EXPORTING,
        JobStatus.STORING,
    }
)


class PublicStatus(models.TextChoices):
    """The three outcomes a caller can distinguish without guessing."""

    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


def video_upload_path(instance: VideoJob, filename: str) -> str:
    return f"videogen/page-{instance.page_id}/{filename}"


class VideoJob(models.Model):
    """One request to produce a shareable video for a single blog article."""

    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
        help_text="The article this video was produced from.",
    )
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.PENDING,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)

    # The narration actually sent to VideoGen (built from the article's own
    # words). Stored for auditability.
    script = models.TextField(blank=True)

    # Provider correlation identifiers.
    workflow_run_id = models.CharField(max_length=255, blank=True)
    project_id = models.CharField(max_length=255, blank=True)
    export_id = models.CharField(max_length=255, blank=True)

    # The finished MP4, stored locally so it stays downloadable for as long as
    # the article exists (the provider's signed URLs expire).
    video_file = models.FileField(upload_to=video_upload_path, blank=True, null=True)

    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "video job"
        verbose_name_plural = "video jobs"

    def __str__(self) -> str:
        return f"VideoJob(page={self.page_id}, status={self.status})"

    # -- Derived, API-facing properties -----------------------------------

    @property
    def public_status(self) -> str:
        if self.status == JobStatus.READY:
            return PublicStatus.READY
        if self.status == JobStatus.FAILED:
            return PublicStatus.FAILED
        return PublicStatus.PROCESSING

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_ready(self) -> bool:
        return self.status == JobStatus.READY and bool(self.video_file)

    def mark_failed(self, message: str) -> None:
        self.status = JobStatus.FAILED
        self.error = message
        self.save(update_fields=["status", "error", "updated_at"])

    def touch(self, **fields) -> None:
        """Persist the given attribute changes plus ``updated_at``."""
        for name, value in fields.items():
            setattr(self, name, value)
        update_fields = [*fields.keys(), "updated_at"]
        self.updated_at = timezone.now()
        self.save(update_fields=update_fields)
