"""Background orchestration of the article-to-video pipeline.

The production of a video is a multi-step, minutes-long process:

    script-to-video  ->  poll workflow run  ->  export (720p)
                     ->  poll export        ->  download MP4  ->  store

The task must return before this finishes and there is **no** task queue/broker
available (and none may be introduced), so the pipeline runs in a daemon
:class:`threading.Thread`. Progress and outcome are persisted on the
:class:`~bakerydemo.video.models.ArticleVideo` row, which the GET endpoint reads.

The steps that *start* provider work (the workflow and the single export) are
guarded so a resumed run never starts them twice — protecting the "exactly one
video, billed once" guarantee even if the pipeline is re-entered.
"""

from __future__ import annotations

import logging
import threading
import time

from django.core.files.base import ContentFile
from django.db import connection
from videogen.models.enums import JobStatus

from .exceptions import (
    VideoGenError,
    VideoGenProductionError,
    VideoGenResponseError,
    VideoGenTimeoutError,
)
from .models import ArticleVideo, VideoStatus
from .provider import VideoGenGateway

logger = logging.getLogger("bakerydemo.video")

# Polling cadence and the per-phase wall-clock budget. The SDK performs no
# retries and no polling of its own, so both are ours to set.
POLL_INTERVAL_SECONDS = 5.0
PHASE_TIMEOUT_SECONDS = 1200.0  # 20 min ceiling per phase (production, export)

# Progress bands (0-100) across the whole pipeline.
_WORKFLOW_LO, _WORKFLOW_HI = 0, 80
_EXPORT_LO, _EXPORT_HI = 80, 95
_DOWNLOAD_PROGRESS = 95

_SUCCEEDED = JobStatus.SUCCEEDED.value
_TERMINAL_FAILURES = {JobStatus.FAILED.value, JobStatus.CANCELLED.value}


def _status_value(status: object) -> str:
    """Normalise a possibly-open ``JobStatus`` value to its wire string."""
    return str(status)


def _fraction(percentage: float | None) -> float:
    """Clamp a provider 0-100 percentage to a 0.0-1.0 fraction."""
    value = float(percentage or 0.0)
    return min(max(value, 0.0), 100.0) / 100.0


def _error_message(error: object) -> str:
    message = getattr(error, "message", None)
    return message if isinstance(message, str) and message else "no reason reported"


def _mark_processing(job: ArticleVideo) -> None:
    if job.status != VideoStatus.PROCESSING:
        job.status = VideoStatus.PROCESSING
        job.save(update_fields=["status", "updated_at"])


def _mark_failed(job: ArticleVideo, reason: str) -> None:
    job.status = VideoStatus.FAILED
    job.error = reason[:2000]
    job.save(update_fields=["status", "error", "updated_at"])


def _set_progress(job: ArticleVideo, value: float) -> None:
    percentage = int(round(min(max(value, 0.0), 100.0)))
    if percentage != job.progress_percentage:
        job.progress_percentage = percentage
        job.save(update_fields=["progress_percentage", "updated_at"])


def _check_deadline(deadline: float, phase: str) -> None:
    if time.monotonic() > deadline:
        raise VideoGenTimeoutError(
            f"VideoGen {phase} did not finish within {int(PHASE_TIMEOUT_SECONDS)}s."
        )


def _poll_workflow(job: ArticleVideo, gateway: VideoGenGateway) -> None:
    """Poll the workflow run until the video has been built into the project."""
    deadline = time.monotonic() + PHASE_TIMEOUT_SECONDS
    while True:
        run = gateway.get_workflow_run(job.provider_workflow_run_id)
        status = _status_value(run.status)
        _set_progress(
            job,
            _WORKFLOW_LO
            + _fraction(run.progress_percentage) * (_WORKFLOW_HI - _WORKFLOW_LO),
        )
        if status == _SUCCEEDED:
            return
        if status in _TERMINAL_FAILURES:
            raise VideoGenProductionError(
                f"video production failed ({status}): {_error_message(run.error)}"
            )
        _check_deadline(deadline, "video production")
        time.sleep(POLL_INTERVAL_SECONDS)


def _poll_export(job: ArticleVideo, gateway: VideoGenGateway) -> str:
    """Poll the export until it succeeds; return the signed MP4 download URL."""
    deadline = time.monotonic() + PHASE_TIMEOUT_SECONDS
    while True:
        export = gateway.get_project_export(
            job.provider_project_id, job.provider_export_id
        )
        status = _status_value(export.status)
        _set_progress(
            job,
            _EXPORT_LO
            + _fraction(export.progress_percentage) * (_EXPORT_HI - _EXPORT_LO),
        )
        if status == _SUCCEEDED:
            url = export.download_url
            if not url:
                raise VideoGenResponseError(
                    "export succeeded but VideoGen returned no download URL."
                )
            return url
        if status in _TERMINAL_FAILURES:
            raise VideoGenProductionError(
                f"video export failed ({status}): {_error_message(export.error)}"
            )
        _check_deadline(deadline, "video export")
        time.sleep(POLL_INTERVAL_SECONDS)


def _run_pipeline(job: ArticleVideo, gateway: VideoGenGateway) -> None:
    """Drive the whole pipeline, updating ``job`` as it advances."""
    _mark_processing(job)

    # 1. Start the script-to-video workflow (at most once).
    if not job.provider_workflow_run_id:
        workflow_run_id, project_id = gateway.start_script_to_video(job.script)
        job.provider_workflow_run_id = workflow_run_id
        job.provider_project_id = project_id
        job.save(
            update_fields=[
                "provider_workflow_run_id",
                "provider_project_id",
                "updated_at",
            ]
        )

    # 2. Wait for the video to be built into the project.
    _poll_workflow(job, gateway)

    # 3. Start the single 720p export (at most once).
    if not job.provider_export_id:
        export_id = gateway.export_project(job.provider_project_id)
        job.provider_export_id = export_id
        job.save(update_fields=["provider_export_id", "updated_at"])

    # 4. Wait for the export and get the signed download URL.
    download_url = _poll_export(job, gateway)

    # 5. Download the finished MP4 and store it locally for durable delivery.
    content = gateway.download_mp4(download_url)
    _set_progress(job, _DOWNLOAD_PROGRESS)
    job.video_file.save(f"{job.job_id}.mp4", ContentFile(content), save=False)
    job.status = VideoStatus.READY
    job.progress_percentage = 100
    job.error = ""
    job.save(
        update_fields=[
            "video_file",
            "status",
            "progress_percentage",
            "error",
            "updated_at",
        ]
    )


def produce(job: ArticleVideo, gateway: VideoGenGateway) -> None:
    """Run the pipeline, translating provider failures into a FAILED job.

    This is the synchronous, injectable entry point used by both the background
    thread and the tests. Provider failures (typed :class:`VideoGenError`) are
    recorded on the job; unexpected errors propagate.
    """
    try:
        _run_pipeline(job, gateway)
    except VideoGenError as exc:
        logger.warning("Video job %s failed: %s", job.job_id, exc)
        _mark_failed(job, str(exc))


def _background_target(job_pk: int) -> None:
    job = None
    try:
        job = ArticleVideo.objects.get(pk=job_pk)
        with VideoGenGateway() as gateway:
            produce(job, gateway)
    except Exception:
        logger.exception("Unexpected error producing video job pk=%s", job_pk)
        if job is not None:
            try:
                _mark_failed(job, "an unexpected server error occurred.")
            except Exception:
                logger.exception("Could not record failure for job pk=%s", job_pk)
    finally:
        # A spawned thread owns its own DB connection; close it so it is not
        # leaked back into the pool.
        connection.close()


def run_in_background(job: ArticleVideo) -> threading.Thread:
    """Start the production pipeline for ``job`` in a daemon thread."""
    thread = threading.Thread(
        target=_background_target,
        args=(job.pk,),
        name=f"article-video-{job.job_id}",
        daemon=True,
    )
    thread.start()
    return thread
