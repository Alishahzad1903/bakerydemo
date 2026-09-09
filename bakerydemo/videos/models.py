import uuid

from django.db import models
from django.utils import timezone
from wagtail.models import Page


class VideoStatus(models.TextChoices):
    """Lifecycle of an :class:`ArticleVideo`.

    ``PENDING`` and ``PROCESSING`` mean the video is still being produced;
    ``SUCCEEDED`` means the finished MP4 is ready to download; ``FAILED`` means
    production stopped with an error. These are the site's own states, mapped
    from the VideoGen provider's job statuses by the integration layer, and are
    what the API surfaces so a caller never has to guess.
    """

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


IN_PROGRESS_STATUSES = frozenset({VideoStatus.PENDING, VideoStatus.PROCESSING})


class ArticleVideo(models.Model):
    """A narrated video produced from one published blog article.

    One row per page (``page`` is unique), which is what makes producing a
    video idempotent: a second request for the same article finds the existing
    row and never starts a second, separately-billed production. The finished
    MP4 is stored in the site's own media storage so it stays retrievable for
    as long as the article — and therefore this row — exists (the row is
    removed with its page via ``on_delete=CASCADE``).
    """

    # Opaque, caller-facing job id (the API's ``videoJobId``). Deliberately
    # decoupled from any VideoGen-internal id.
    job_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    page = models.OneToOneField(
        Page,
        on_delete=models.CASCADE,
        related_name="article_video",
    )

    status = models.CharField(
        max_length=16,
        choices=VideoStatus.choices,
        default=VideoStatus.PENDING,
    )
    # Completion progress 0-100 for the current production.
    progress = models.PositiveSmallIntegerField(default=0)
    # Human-readable failure reason; empty unless ``status`` is ``failed``.
    error = models.TextField(blank=True, default="")

    # The exact narration script sent to VideoGen (title + first sentence of
    # the introduction), kept for auditing what was produced and billed.
    script = models.TextField(blank=True, default="")

    # Provider-side identifiers, retained for diagnostics and to avoid
    # re-creating provider work on a resumed production.
    provider_workflow_run_id = models.CharField(max_length=128, blank=True, default="")
    provider_project_id = models.CharField(max_length=128, blank=True, default="")
    provider_export_id = models.CharField(max_length=128, blank=True, default="")

    # The finished MP4, stored in the site's media storage.
    mp4 = models.FileField(
        upload_to="article_videos/", blank=True, null=True, max_length=255
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    ready_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Article video"

    def __str__(self) -> str:
        return f"Video for page {self.page_id} ({self.status})"

    @property
    def is_in_progress(self) -> bool:
        return self.status in IN_PROGRESS_STATUSES

    @property
    def is_ready(self) -> bool:
        return self.status == VideoStatus.SUCCEEDED and bool(self.mp4)

    def mark_processing(self, progress: int = 0) -> None:
        self.status = VideoStatus.PROCESSING
        self.progress = progress
        self.save(update_fields=["status", "progress", "updated_at"])

    def update_progress(self, progress: int) -> None:
        # Never let progress go backwards, and clamp to the 0-99 band while
        # still processing (100 is reserved for a ready video).
        clamped = max(0, min(99, int(progress)))
        if clamped <= self.progress:
            return
        self.progress = clamped
        self.save(update_fields=["progress", "updated_at"])

    def mark_failed(self, message: str) -> None:
        self.status = VideoStatus.FAILED
        self.error = message or "Video production failed."
        self.save(update_fields=["status", "error", "updated_at"])

    def mark_succeeded(self) -> None:
        self.status = VideoStatus.SUCCEEDED
        self.progress = 100
        self.error = ""
        self.ready_at = timezone.now()
        self.save(
            update_fields=["status", "progress", "error", "ready_at", "updated_at"]
        )

    def reset_for_new_attempt(self) -> None:
        """Clear a terminal (failed) row so a fresh production can start."""
        self.status = VideoStatus.PENDING
        self.progress = 0
        self.error = ""
        self.script = ""
        self.provider_workflow_run_id = ""
        self.provider_project_id = ""
        self.provider_export_id = ""
        if self.mp4:
            self.mp4.delete(save=False)
        self.mp4 = None
        self.ready_at = None
        self.save()
