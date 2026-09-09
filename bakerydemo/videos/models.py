from __future__ import annotations

import uuid

from django.db import models

from .constants import VideoStatus


class ArticleVideo(models.Model):
    """A narrated video produced from one blog article.

    One row per page (``OneToOneField``) so that asking for a video twice in a
    row reuses the same production instead of billing VideoGen again. The row —
    and the stored MP4 — live for as long as the article does (``CASCADE``).
    """

    #: Public, opaque job identifier returned as ``videoJobId`` and used in the
    #: status/download URLs. Generated up front so the POST can return before
    #: any VideoGen id exists.
    job_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="article_video",
    )
    status = models.CharField(
        max_length=20, choices=VideoStatus.choices, default=VideoStatus.PENDING
    )
    progress_percentage = models.FloatField(default=0.0)
    error_message = models.TextField(blank=True, default="")

    # VideoGen-side identifiers, filled in as the pipeline progresses.
    videogen_workflow_run_id = models.CharField(max_length=255, blank=True, default="")
    videogen_project_id = models.CharField(max_length=255, blank=True, default="")
    videogen_export_id = models.CharField(max_length=255, blank=True, default="")

    #: The finished MP4, downloaded from VideoGen and re-hosted by the site.
    video_file = models.FileField(
        upload_to="article_videos/", null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "article video"
        verbose_name_plural = "article videos"

    def __str__(self) -> str:
        return f"ArticleVideo({self.job_id}, {self.status})"

    # -- state transitions -------------------------------------------------

    def set_progress(self, percentage: float) -> None:
        self.progress_percentage = max(0.0, min(100.0, float(percentage)))
        self.save(update_fields=["progress_percentage", "updated_at"])

    def mark_processing(self, percentage: float = 1.0) -> None:
        self.status = VideoStatus.PROCESSING
        self.progress_percentage = max(0.0, min(100.0, float(percentage)))
        self.error_message = ""
        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "error_message",
                "updated_at",
            ]
        )

    def mark_ready(self) -> None:
        self.status = VideoStatus.READY
        self.progress_percentage = 100.0
        self.error_message = ""
        self.save(
            update_fields=[
                "status",
                "progress_percentage",
                "error_message",
                "updated_at",
            ]
        )

    def mark_failed(self, message: str) -> None:
        self.status = VideoStatus.FAILED
        self.error_message = message or "Video production failed."
        self.save(update_fields=["status", "error_message", "updated_at"])

    def reset_for_retry(self) -> None:
        """Clear a previously failed production so it can be re-run in place."""
        if self.video_file:
            self.video_file.delete(save=False)
        self.status = VideoStatus.PENDING
        self.progress_percentage = 0.0
        self.error_message = ""
        self.videogen_workflow_run_id = ""
        self.videogen_project_id = ""
        self.videogen_export_id = ""
        self.save()
