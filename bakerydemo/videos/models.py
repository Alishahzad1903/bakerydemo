import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


def article_video_upload_to(instance, filename):
    # Store each finished MP4 under a stable, per-job path in MEDIA_ROOT.
    return f"article_videos/{instance.job_id}.mp4"


class VideoStatus(models.TextChoices):
    # The video has been requested but production has not started yet.
    PENDING = "pending", _("Pending")
    # VideoGen is producing the video (script -> video -> export -> download).
    PROCESSING = "processing", _("Processing")
    # The MP4 is stored and downloadable.
    READY = "ready", _("Ready")
    # Production failed; ``error`` carries the reason.
    FAILED = "failed", _("Failed")


class ArticleVideo(models.Model):
    """A request to turn one published blog article into a narrated video.

    One live row per article represents the "current" video: while a row is
    pending/processing/ready it is reused, so asking for a video twice in a row
    neither produces two videos nor bills twice. A failed row does not block a
    fresh attempt.

    The finished MP4 is stored locally (``video_file``) rather than pointed at a
    provider URL, so it stays downloadable through the site for as long as the
    article — and therefore this row (``on_delete=CASCADE``) — exists.
    """

    # Public identifier returned as ``videoJobId``. Opaque and unguessable.
    job_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_videos",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoStatus.choices,
        default=VideoStatus.PENDING,
        db_index=True,
    )
    progress_percentage = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    # The exact narration script sent to VideoGen (article text, verbatim).
    script = models.TextField(blank=True, default="")

    # The stored, finished MP4.
    video_file = models.FileField(
        upload_to=article_video_upload_to, blank=True, null=True
    )

    # VideoGen identifiers, kept for traceability and idempotent recovery.
    workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    project_id = models.CharField(max_length=128, blank=True, default="")
    export_id = models.CharField(max_length=128, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one non-failed video per article. This is the database-level
            # guarantee behind request idempotency: a concurrent second POST that
            # slips past the application check hits this constraint instead of
            # starting a second (billed) production run.
            models.UniqueConstraint(
                fields=["page"],
                condition=~models.Q(status="failed"),
                name="unique_active_video_per_page",
            ),
        ]

    def __str__(self):
        return f"ArticleVideo({self.job_id}, page={self.page_id}, {self.status})"

    @property
    def is_terminal(self):
        return self.status in (VideoStatus.READY, VideoStatus.FAILED)

    @property
    def is_ready(self):
        return self.status == VideoStatus.READY and bool(self.video_file)
