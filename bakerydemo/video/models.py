import uuid

from django.db import models
from django.db.models import Q


class VideoJob(models.Model):
    """
    One request to turn a blog article into a narrated MP4 via VideoGen.

    A job tracks the work from the moment it is accepted through to a
    downloadable MP4 (or a failure). The public identifier is an opaque UUID so
    ids are not enumerable. At most one *active* job (pending/processing/ready)
    may exist per page — enforced at the database level — which is what makes a
    repeated request return the existing job instead of producing (and paying
    for) a second video.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    # Statuses for which a video already exists or is being produced. A new
    # request while one of these is present reuses the existing job.
    ACTIVE_STATUSES = (Status.PENDING, Status.PROCESSING, Status.READY)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_jobs",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    # Overall progress across production + export, 0-100.
    progress = models.PositiveSmallIntegerField(default=0)

    # The narration script sent to VideoGen, built from the article's own text.
    # Stored for auditability and so the job can be replayed deterministically.
    narration = models.TextField(blank=True)

    # VideoGen identifiers gathered as the job progresses.
    workflow_run_id = models.CharField(max_length=128, blank=True)
    project_id = models.CharField(max_length=128, blank=True)
    export_id = models.CharField(max_length=128, blank=True)
    export_file_id = models.CharField(max_length=128, blank=True)

    # Where to download the finished MP4 (a VideoGen signed URL) and when that
    # signed URL expires, so it can be re-signed on demand.
    download_url = models.TextField(blank=True)
    download_url_expires_at = models.DateTimeField(null=True, blank=True)

    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["page", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["page"],
                condition=Q(status__in=["pending", "processing", "ready"]),
                name="unique_active_video_job_per_page",
            )
        ]

    def __str__(self):
        return f"VideoJob {self.pk} (page={self.page_id}, status={self.status})"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    # --- state transitions (each persists only the fields it touches) ---

    def set_progress(self, value):
        clamped = max(0, min(100, int(value)))
        if clamped != self.progress:
            self.progress = clamped
            self.save(update_fields=["progress", "updated_at"])

    def mark_processing(self):
        self.status = self.Status.PROCESSING
        self.save(update_fields=["status", "updated_at"])

    def mark_ready(self):
        self.status = self.Status.READY
        self.progress = 100
        self.save(update_fields=["status", "progress", "updated_at"])

    def mark_failed(self, message):
        self.status = self.Status.FAILED
        self.error = message or "Video production failed."
        self.save(update_fields=["status", "error", "updated_at"])
