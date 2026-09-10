"""Orchestration for producing an article video with VideoGen.

This module owns the end-to-end pipeline and the idempotent job bookkeeping. It
deliberately uses an in-process background thread rather than a task queue or
broker: the brief forbids introducing that infrastructure, and the finished MP4
is persisted to storage so durability does not depend on the worker staying
alive.

Pipeline for one job:

1. ``POST /v1/workflows/script-to-video``  (stock footage, 16:9, a voice only)
2. poll the workflow run until it succeeds
3. ``POST /v1/projects/{id}/export``       (single 720p-class export)
4. poll the export until it succeeds
5. download the signed MP4 and store it on the site
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time

from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, transaction

from .models import VideoJob, VideoJobStatus
from .narration import build_narration_script
from .videogen import (
    VideoGenError,
    VideoGenExportFailedError,
    VideoGenWorkflowFailedError,
    get_client,
)
from .videogen.client import VideoGenClient

logger = logging.getLogger("bakerydemo.videos")

# Progress checkpoints (percent) for the phases of the pipeline.
_PROGRESS_CREATED = 5
_PROGRESS_WORKFLOW_SPAN = (5, 50)
_PROGRESS_EXPORT_STARTED = 55
_PROGRESS_EXPORT_SPAN = (55, 90)
_PROGRESS_DOWNLOADING = 92


def enqueue_video_job(page, user) -> tuple[VideoJob, bool]:
    """Return ``(job, created)`` for ``page``, producing at most one video.

    If a non-failed job already exists for the page it is returned unchanged
    (idempotent: no second video is produced and nothing is billed twice).
    Otherwise a new job is created and production is kicked off in the
    background.
    """
    created = False
    try:
        with transaction.atomic():
            existing = (
                VideoJob.objects.select_for_update()
                .filter(page=page)
                .exclude(status=VideoJobStatus.FAILED)
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                return existing, False

            job = VideoJob.objects.create(
                page=page,
                requested_by=user if getattr(user, "pk", None) else None,
                status=VideoJobStatus.PENDING,
                script=build_narration_script(page),
            )
            created = True
    except IntegrityError:
        # A concurrent request won the race for this page; return its job.
        existing = (
            VideoJob.objects.filter(page=page)
            .exclude(status=VideoJobStatus.FAILED)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing, False
        raise

    if created:
        _start_worker(job.id)
    return job, created


def _start_worker(job_id) -> None:
    """Run the pipeline, inline when ``VIDEOGEN_RUN_SYNC`` is set, else threaded."""
    if getattr(settings, "VIDEOGEN_RUN_SYNC", False):
        process_video_job(job_id)
        return
    thread = threading.Thread(
        target=_threaded_process,
        args=(job_id,),
        name=f"videogen-job-{job_id}",
        daemon=True,
    )
    thread.start()


def _threaded_process(job_id) -> None:
    from django.db import connections

    try:
        process_video_job(job_id)
    finally:
        # Release this thread's DB connections.
        connections.close_all()


def _update(job: VideoJob, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    job.save(update_fields=[*fields.keys(), "updated_at"])


def _scaled(span: tuple[int, int], provider_percent: int) -> int:
    low, high = span
    pct = max(0, min(100, provider_percent))
    return low + round((high - low) * pct / 100)


def process_video_job(job_id, client: VideoGenClient | None = None) -> VideoJob:
    """Drive a single job to a terminal state. Safe to call once per job."""
    # Atomically claim the job so a job is never processed twice.
    claimed = (
        VideoJob.objects.filter(id=job_id, status=VideoJobStatus.PENDING).update(
            status=VideoJobStatus.PROCESSING, progress_percentage=1
        )
    )
    job = VideoJob.objects.get(id=job_id)
    if not claimed:
        logger.info("VideoJob %s already claimed (status=%s)", job_id, job.status)
        return job

    client = client or get_client()
    try:
        _run_pipeline(job, client)
    except VideoGenError as exc:
        logger.warning("VideoJob %s failed: %s", job_id, exc)
        _fail(job, message=str(exc), code=getattr(exc, "code", "") or "")
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("VideoJob %s failed unexpectedly", job_id)
        _fail(job, message=f"Unexpected error: {exc}", code="internal_error")
    return job


def _fail(job: VideoJob, *, message: str, code: str = "") -> None:
    job.refresh_from_db(fields=["status"])
    _update(
        job,
        status=VideoJobStatus.FAILED,
        error=message or "Video production failed.",
        error_code=code or "",
    )


def _run_pipeline(job: VideoJob, client: VideoGenClient) -> None:
    # 1. Start the script-to-video workflow (the single billed production).
    create = client.create_script_to_video(
        script=job.script,
        aspect_ratio=getattr(settings, "VIDEOGEN_ASPECT_RATIO", "16:9"),
        visual_style=getattr(settings, "VIDEOGEN_VISUAL_STYLE", {"type": "STOCK"}),
    )
    workflow_run_id = create.get("workflowRunId")
    project_id = create.get("projectId")
    if not workflow_run_id or not project_id:
        raise VideoGenError(
            "VideoGen did not return a workflowRunId/projectId for the run.",
            body=create,
        )
    _update(
        job,
        workflow_run_id=str(workflow_run_id),
        project_id=str(project_id),
        progress_percentage=_PROGRESS_CREATED,
    )

    # 2. Poll the workflow run to completion.
    run = _poll(
        lambda: client.get_workflow_run(job.workflow_run_id),
        job=job,
        span=_PROGRESS_WORKFLOW_SPAN,
    )
    if not run.succeeded:
        raise VideoGenWorkflowFailedError(
            _terminal_error_message(
                "Video generation", run.status, run.error
            ),
            code=_error_code(run.error),
            body=run.raw,
        )

    # 3. Start a single export at the configured (720p-class) quality.
    export_start = client.export_project(
        job.project_id,
        quality=getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "STANDARD"),
    )
    export_id = export_start.get("exportId")
    if not export_id:
        raise VideoGenError(
            "VideoGen did not return an exportId for the export.",
            body=export_start,
        )
    _update(
        job,
        export_id=str(export_id),
        progress_percentage=_PROGRESS_EXPORT_STARTED,
    )

    # 4. Poll the export to completion.
    export = _poll(
        lambda: client.get_export(job.project_id, job.export_id),
        job=job,
        span=_PROGRESS_EXPORT_SPAN,
    )
    if not export.succeeded:
        raise VideoGenExportFailedError(
            _terminal_error_message("Export", export.status, export.error),
            code=_error_code(export.error),
            body=export.raw,
        )
    if not export.download_url:
        raise VideoGenExportFailedError(
            "Export succeeded but VideoGen returned no download URL.",
            body=export.raw,
        )

    # 5. Download and persist the MP4 locally so it stays available.
    _update(job, progress_percentage=_PROGRESS_DOWNLOADING)
    _store_mp4(job, client, export.download_url)

    _update(
        job,
        status=VideoJobStatus.READY,
        progress_percentage=100,
        error="",
        error_code="",
    )


def _poll(fetch, *, job: VideoJob, span: tuple[int, int]):
    """Poll ``fetch()`` until the returned object reports a terminal status."""
    interval = float(getattr(settings, "VIDEOGEN_POLL_INTERVAL_SECONDS", 5))
    timeout = float(getattr(settings, "VIDEOGEN_POLL_TIMEOUT_SECONDS", 900))
    deadline = time.monotonic() + timeout

    while True:
        result = fetch()
        _update(
            job,
            progress_percentage=_scaled(span, result.progress_percentage),
        )
        if result.is_terminal:
            return result
        if time.monotonic() >= deadline:
            raise VideoGenError(
                f"Timed out after {timeout:.0f}s waiting for VideoGen "
                f"(last status: {result.status or 'unknown'})."
            )
        time.sleep(interval)


def _store_mp4(job: VideoJob, client: VideoGenClient, url: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        for chunk in client.stream_download(url):
            if chunk:
                tmp.write(chunk)
        tmp.flush()
        if tmp.tell() == 0:
            raise VideoGenExportFailedError(
                "Downloaded video was empty.",
            )
        tmp.seek(0)
        # save() writes through the configured storage backend.
        job.video_file.save(f"{job.id}.mp4", File(tmp), save=False)
    job.save(update_fields=["video_file", "updated_at"])


def _terminal_error_message(subject: str, status: str, error) -> str:
    detail = _error_text(error)
    status = status or "unknown"
    if detail:
        return f"{subject} {status}: {detail}"
    return f"{subject} {status}."


def _error_text(error) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or "").strip()
    if error:
        return str(error).strip()
    return ""


def _error_code(error) -> str:
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""
