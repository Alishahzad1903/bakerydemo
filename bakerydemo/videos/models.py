import uuid

from django.db import models


class VideoJobState(models.TextChoices):
    """Internal lifecycle of a video job.

    ``PRODUCING`` and ``EXPORTING`` are in-progress; ``READY`` and ``FAILED`` are terminal.
    The public API collapses these into ``processing`` / ``ready`` / ``failed``.
    """

    PRODUCING = "producing", "Producing"  # VideoGen workflow is building the project/video
    EXPORTING = "exporting", "Exporting"  # project is being rendered to an MP4
    READY = "ready", "Ready"  # MP4 downloaded and stored; downloadable
    FAILED = "failed", "Failed"  # workflow or export failed


class VideoJob(models.Model):
    """One video-production request for one blog article.

    A ``OneToOneField`` to the page makes the request idempotent per article: a second
    request reuses the same job rather than producing (and billing) a second video. The
    finished MP4 is stored locally so it stays downloadable for as long as the article
    exists (the FK cascades on page deletion).
    """

    public_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="video_job",
    )
    state = models.CharField(
        max_length=16,
        choices=VideoJobState.choices,
        default=VideoJobState.PRODUCING,
    )
    script = models.TextField(help_text="Narration script sent to VideoGen (verbatim).")

    # Opaque VideoGen identifiers.
    workflow_run_id = models.CharField(max_length=128, blank=True)
    project_id = models.CharField(max_length=128, blank=True)
    export_id = models.CharField(max_length=128, blank=True)
    # Compare-and-swap guard ensuring the (billable) export is requested exactly once.
    export_requested = models.BooleanField(default=False)

    progress = models.FloatField(default=0.0)  # 0-100, blended across the two phases
    error_code = models.CharField(max_length=128, blank=True)
    error_message = models.TextField(blank=True)

    mp4 = models.FileField(upload_to="article_videos/", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video job"

    def __str__(self):
        return f"VideoJob({self.public_id}, page={self.page_id}, state={self.state})"

    @property
    def is_terminal(self) -> bool:
        return self.state in (VideoJobState.READY, VideoJobState.FAILED)

    @property
    def api_status(self) -> str:
        """Coarse status exposed on the API: ``processing`` | ``ready`` | ``failed``."""
        if self.state == VideoJobState.READY:
            return "ready"
        if self.state == VideoJobState.FAILED:
            return "failed"
        return "processing"
