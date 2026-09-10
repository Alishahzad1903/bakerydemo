"""Orchestration for turning a published article into a shareable video.

This module owns the whole flow: resolve the article, build its narration,
create (or reuse) a job, and drive the VideoGen pipeline to a finished MP4 that
is stored in the site's own storage.

Production runs the pipeline on a background daemon thread so the POST can
return immediately — there is deliberately no broker or task queue. Tests can
inject a synchronous runner instead.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from django.core.files.base import ContentFile
from django.db import IntegrityError, connections, transaction
from django.http import Http404

from bakerydemo.blog.models import BlogPage

from .constants import VideoJobStatus
from .models import VideoJob
from .narration import build_script
from .videogen import VideoGenClient
from .videogen.exceptions import VideoGenError

logger = logging.getLogger("bakerydemo.videos")

JobRunner = Callable[[str], None]


class NotAnArticleError(Exception):
    """The page exists and is live, but is not a blog article."""


# ---------------------------------------------------------------------------
# Article resolution & narration
# ---------------------------------------------------------------------------


def resolve_article(page_id: int) -> BlogPage:
    """Return the live :class:`BlogPage` with ``page_id``.

    Raises :class:`~django.http.Http404` when no published article matches, and
    :class:`NotAnArticleError` when the page exists but is not a blog article.
    """
    page = BlogPage.objects.filter(pk=page_id, live=True).first()
    if page is not None:
        return page

    # Distinguish "no such published page" from "published, but wrong type", so
    # the caller gets an accurate error.
    from wagtail.models import Page

    generic = Page.objects.filter(pk=page_id, live=True).first()
    if generic is None:
        raise Http404("No published blog article matches the given page id.")
    raise NotAnArticleError(f"Page {page_id} is not a blog article.")


def resolve_article_any(page_id: int) -> BlogPage:
    """Return the :class:`BlogPage` with ``page_id`` regardless of live state.

    Used for reading/downloading an already-produced video: a finished video
    stays retrievable for as long as the article exists, even if it is later
    unpublished. Raises :class:`~django.http.Http404` when no such page exists
    and :class:`NotAnArticleError` when the page is not a blog article.
    """
    page = BlogPage.objects.filter(pk=page_id).first()
    if page is not None:
        return page

    from wagtail.models import Page

    generic = Page.objects.filter(pk=page_id).first()
    if generic is None:
        raise Http404("No blog article matches the given page id.")
    raise NotAnArticleError(f"Page {page_id} is not a blog article.")


def script_for_article(page: BlogPage) -> str:
    """Build the narration script for ``page`` from its own title + intro."""
    return build_script(page.title, page.introduction or "")


# ---------------------------------------------------------------------------
# Job creation (idempotent) & lookup
# ---------------------------------------------------------------------------


def _active_job_for_page(page_id: int) -> VideoJob | None:
    return (
        VideoJob.objects.filter(page_id=page_id)
        .exclude(status=VideoJobStatus.FAILED)
        .order_by("-created_at")
        .first()
    )


def request_video(
    page: BlogPage,
    user,
    *,
    runner: JobRunner | None = None,
) -> tuple[VideoJob, bool]:
    """Return the video job for ``page``, creating and starting one if needed.

    Idempotent: if a non-failed job already exists for the page, it is returned
    unchanged (``created=False``) — no second video is produced and no second
    charge is incurred. Otherwise a new job is created and its pipeline is
    started via ``runner`` (defaults to a background daemon thread).
    """
    run = runner or start_job_thread

    existing = _active_job_for_page(page.pk)
    if existing is not None:
        return existing, False

    script = script_for_article(page)

    try:
        with transaction.atomic():
            job = VideoJob.objects.create(
                page=page,
                requested_by=user if getattr(user, "pk", None) else None,
                script=script,
                status=VideoJobStatus.PENDING,
                progress_percentage=0,
            )
    except IntegrityError:
        # A concurrent request created the job first; return theirs.
        existing = _active_job_for_page(page.pk)
        if existing is not None:
            return existing, False
        raise

    transaction.on_commit(lambda: run(str(job.pk)))
    return job, True


def get_job_for_page(page_id: int, job_id) -> VideoJob:
    """Return the job ``job_id`` belonging to ``page_id`` or raise ``Http404``."""
    job = VideoJob.objects.filter(pk=job_id, page_id=page_id).first()
    if job is None:
        raise Http404("No video job matches the given id for this article.")
    return job


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------


def start_job_thread(job_id: str) -> None:
    """Default runner: execute the pipeline on a background daemon thread."""
    thread = threading.Thread(
        target=_thread_entry,
        args=(job_id,),
        name=f"videojob-{job_id}",
        daemon=True,
    )
    thread.start()


def _thread_entry(job_id: str) -> None:
    try:
        run_pipeline(job_id)
    except Exception:  # pragma: no cover - defensive; run_pipeline handles its own
        logger.exception("Unhandled error while producing video job %s", job_id)
    finally:
        # Release this thread's database connection.
        connections.close_all()


class _ProgressWriter:
    """Throttled writer that maps provider progress into a job-wide band."""

    def __init__(self, job: VideoJob, low: int, high: int) -> None:
        self._job = job
        self._low = low
        self._high = high
        self._last_written = job.progress_percentage

    def __call__(self, provider_percentage: float) -> None:
        fraction = max(0.0, min(100.0, provider_percentage)) / 100.0
        value = int(self._low + (self._high - self._low) * fraction)
        value = max(self._job.progress_percentage, value)
        # Only touch the database on a meaningful change.
        if value >= self._last_written + 3 or value >= self._high:
            self._set_progress(value)

    def _set_progress(self, value: int) -> None:
        self._last_written = value
        self._job.progress_percentage = value
        self._job.save(update_fields=["progress_percentage", "updated_at"])


def run_pipeline(job_id: str, *, client: VideoGenClient | None = None) -> VideoJob:
    """Drive one job through VideoGen to a finished, stored MP4.

    Any provider failure is recorded on the job (status ``FAILED`` with an error
    message) rather than propagated, so a caller polling the job always sees a
    definite outcome.
    """
    job = VideoJob.objects.get(pk=job_id)
    owns_client = client is None
    client = client or VideoGenClient()

    try:
        _mark_processing(job)

        run = client.start_script_to_video(job.script)
        job.videogen_workflow_run_id = run.workflow_run_id
        if run.project_id:
            job.videogen_project_id = run.project_id
        job.save(
            update_fields=[
                "videogen_workflow_run_id",
                "videogen_project_id",
                "updated_at",
            ]
        )

        completed_run = client.wait_for_run(
            run.workflow_run_id,
            on_progress=_ProgressWriter(job, low=5, high=70),
        )
        project_id = run.project_id or completed_run.get("project_id")
        if not project_id:
            raise VideoGenError(
                "VideoGen did not report a project id for the completed run."
            )
        job.videogen_project_id = project_id

        export = client.start_export(project_id)
        job.videogen_export_id = export.export_id
        job.save(
            update_fields=[
                "videogen_project_id",
                "videogen_export_id",
                "updated_at",
            ]
        )

        export_result = client.wait_for_export(
            project_id,
            export.export_id,
            on_progress=_ProgressWriter(job, low=70, high=99),
        )
        file_id = export_result.get("export_file_id")
        if not file_id:
            raise VideoGenError(
                "VideoGen did not report an export file id for the finished export."
            )

        data = client.download_file(file_id)
        _store_success(job, file_id=file_id, data=data)
        logger.info("Video job %s succeeded (%d bytes)", job_id, len(data))
        return job
    except VideoGenError as exc:
        _mark_failed(job, str(exc))
        logger.warning("Video job %s failed: %s", job_id, exc)
        return job
    except Exception as exc:  # unexpected — still record a definite outcome
        _mark_failed(job, f"Unexpected error while producing the video: {exc}")
        logger.exception("Video job %s failed unexpectedly", job_id)
        return job
    finally:
        if owns_client:
            client.close()


def _mark_processing(job: VideoJob) -> None:
    job.status = VideoJobStatus.PROCESSING
    job.error_message = ""
    if job.progress_percentage < 5:
        job.progress_percentage = 5
    job.save(
        update_fields=["status", "error_message", "progress_percentage", "updated_at"]
    )


def _store_success(job: VideoJob, *, file_id: str, data: bytes) -> None:
    job.videogen_file_id = file_id
    job.video_file.save(f"{job.pk}.mp4", ContentFile(data), save=False)
    job.status = VideoJobStatus.SUCCEEDED
    job.progress_percentage = 100
    job.error_message = ""
    job.save(
        update_fields=[
            "videogen_file_id",
            "video_file",
            "status",
            "progress_percentage",
            "error_message",
            "updated_at",
        ]
    )


def _mark_failed(job: VideoJob, message: str) -> None:
    job.status = VideoJobStatus.FAILED
    job.error_message = message[:2000]
    job.save(update_fields=["status", "error_message", "updated_at"])
