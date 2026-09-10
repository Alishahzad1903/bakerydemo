"""Orchestration: start a job (idempotently) and drive it to a finished MP4.

There is no task queue or broker in this project (and the task forbids adding
one), so production runs in a background daemon thread. The whole VideoGen
pipeline for one job — start, poll, export, poll — is one logical unit of work
that owns a single context-managed client and its own DB connection.

Billing safety: the two billable calls (``script_to_video`` and
``export_project``) run **at most once per job** and are never re-attempted. If
an attempt fails, the job is marked failed and diagnosed from what the provider
reported; a fresh video is never produced "to see what happens". Only the
idempotent polling reads may be retried on a transient transport hiccup.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from django.db import IntegrityError, connection, transaction

from .exceptions import VideoGenError, VideoGenProductionError
from .models import ACTIVE_STATUSES, JobStatus, VideoJob
from .videogen_client import VideoGenService

logger = logging.getLogger("bakerydemo.videos")

ServiceFactory = Callable[[], VideoGenService]

# Polling cadence and the overall wall-clock budget for one production.
POLL_INTERVAL_SECONDS = 6.0
MAX_PIPELINE_SECONDS = 30 * 60
# How many consecutive transient failures of an idempotent poll read we tolerate
# before giving up (reads never bill, so retrying them is safe).
MAX_CONSECUTIVE_POLL_ERRORS = 5

# Refresh a stored signed URL when it is missing or within this margin of expiry.
URL_REFRESH_MARGIN_SECONDS = 3600

# Terminal provider job statuses (JobStatus enum wire values on the VideoGen side).
_PROVIDER_SUCCEEDED = "succeeded"
_PROVIDER_TERMINAL_FAILURE = {"failed", "cancelled"}


# ---------------------------------------------------------------------------
# Starting a job
# ---------------------------------------------------------------------------


def start_or_get_job(page, script: str, *, spawn: bool = True) -> tuple[VideoJob, bool]:
    """Return the active job for ``page``, creating and starting one if needed.

    Idempotent: asking twice in a row for the same article returns the same job
    and does not start a second production. Returns ``(job, created)``.
    """
    created = False
    with transaction.atomic():
        existing = (
            VideoJob.objects.select_for_update()
            .filter(page=page, status__in=ACTIVE_STATUSES)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing, False
        try:
            job = VideoJob.objects.create(
                page=page, status=JobStatus.PENDING, script=script
            )
            created = True
        except IntegrityError:
            # Lost a race against a concurrent request; reuse the winner's job.
            job = (
                VideoJob.objects.filter(page=page, status__in=ACTIVE_STATUSES)
                .order_by("-created_at")
                .first()
            )
            if job is None:
                raise
            return job, False

    if created and spawn:
        _spawn_pipeline(job.id)
    return job, created


def _spawn_pipeline(job_id) -> None:
    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id,),
        name=f"videogen-pipeline-{job_id}",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _update(job_id, **fields) -> None:
    VideoJob.objects.filter(pk=job_id).update(**fields)


def _status_value(status) -> str:
    """Wire value of a provider JobStatus (open enum -> str subclass, or str)."""
    return str(status)


def _error_fields(error_model) -> tuple[str, str]:
    """Extract (message, code) from a provider ApiErrorModel, if any."""
    if error_model is None:
        return "", ""
    message = getattr(error_model, "message", "") or ""
    code = getattr(error_model, "code", None)
    return message, code if isinstance(code, str) else ""


def _await_terminal(
    fetch,
    *,
    job_id,
    deadline: float,
    progress_floor: float,
    progress_ceiling: float,
    stage: str,
):
    """Poll ``fetch()`` until the provider job succeeds, or raise.

    ``fetch`` returns an object exposing ``status``, ``progress_percentage`` and
    ``error`` (both WorkflowRun and ProjectExport do). Provider progress (0-100)
    is mapped into the [floor, ceiling] slice of the job's overall progress.
    """
    consecutive_errors = 0
    while True:
        if time.monotonic() > deadline:
            raise VideoGenProductionError(
                f"VideoGen {stage} did not finish within the time budget.",
                code="timeout",
            )
        try:
            result = fetch()
            consecutive_errors = 0
        except VideoGenError:
            # Idempotent read: tolerate a bounded run of transient failures.
            consecutive_errors += 1
            if consecutive_errors > MAX_CONSECUTIVE_POLL_ERRORS:
                raise
            logger.warning(
                "Transient error polling VideoGen %s for job %s (%d/%d)",
                stage,
                job_id,
                consecutive_errors,
                MAX_CONSECUTIVE_POLL_ERRORS,
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        status = _status_value(result.status)
        provider_progress = float(getattr(result, "progress_percentage", 0.0) or 0.0)
        mapped = progress_floor + (progress_ceiling - progress_floor) * (
            max(0.0, min(provider_progress, 100.0)) / 100.0
        )
        _update(job_id, progress_percentage=round(mapped, 2))

        if status == _PROVIDER_SUCCEEDED:
            return result
        if status in _PROVIDER_TERMINAL_FAILURE:
            message, code = _error_fields(result.error)
            raise VideoGenProductionError(
                message or f"VideoGen {stage} {status}.",
                code=code or status,
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def run_pipeline(
    job_id, service_factory: ServiceFactory = VideoGenService.from_settings
) -> None:
    """Drive a single job to completion. Intended to run in a daemon thread."""
    try:
        job = VideoJob.objects.filter(pk=job_id).first()
        if job is None:
            logger.error("VideoGen pipeline: job %s vanished before start", job_id)
            return
        _update(job_id, status=JobStatus.PROCESSING, progress_percentage=1.0)
        deadline = time.monotonic() + MAX_PIPELINE_SECONDS

        with service_factory() as service:
            # 1. Start production (billable — exactly once).
            started = service.start_script_to_video(job.script)
            _update(
                job_id,
                workflow_run_id=started.workflow_run_id,
                project_id=started.project_id,
            )

            # 2. Wait for the workflow to build the video.
            _await_terminal(
                lambda: service.get_workflow_run(started.workflow_run_id),
                job_id=job_id,
                deadline=deadline,
                progress_floor=1.0,
                progress_ceiling=80.0,
                stage="workflow run",
            )

            # 3. Export to a single 720p MP4 (billable — exactly once).
            export = service.export_project(started.project_id)
            _update(job_id, export_id=export.export_id)

            # 4. Wait for the export and capture the signed download URL.
            finished = _await_terminal(
                lambda: service.get_project_export(
                    started.project_id, export.export_id
                ),
                job_id=job_id,
                deadline=deadline,
                progress_floor=80.0,
                progress_ceiling=100.0,
                stage="project export",
            )

            _update(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress_percentage=100.0,
                download_url=finished.download_url or "",
                download_url_expires_at=finished.download_url_expires_at,
                error_message="",
                error_code="",
            )
            logger.info("VideoGen pipeline: job %s succeeded", job_id)

    except VideoGenProductionError as exc:
        logger.warning("VideoGen pipeline: job %s failed: %s", job_id, exc)
        _mark_failed(job_id, exc.message, exc.code or "")
    except VideoGenError as exc:
        logger.warning("VideoGen pipeline: job %s failed: %s", job_id, exc)
        _mark_failed(job_id, str(exc), getattr(exc, "code", "") or "")
    except Exception:
        logger.exception("VideoGen pipeline: job %s crashed", job_id)
        _mark_failed(job_id, "An unexpected error occurred while producing the video.")
    finally:
        # Release this thread's DB connection so it is not leaked.
        connection.close()


def _mark_failed(job_id, message: str, code: str = "") -> None:
    _update(
        job_id,
        status=JobStatus.FAILED,
        error_message=message or "Video production failed.",
        error_code=code,
    )


# ---------------------------------------------------------------------------
# Download URL longevity
# ---------------------------------------------------------------------------


def refresh_download_url(
    job: VideoJob,
    *,
    service_factory: ServiceFactory = VideoGenService.from_settings,
    now: float | None = None,
) -> str:
    """Return a currently-valid signed MP4 URL for a succeeded job.

    The stored URL is reused unless it is missing or within an hour of expiry,
    in which case it is re-signed via ``get_project_export`` (a read — not
    billed media work) and persisted. This keeps the video downloadable for as
    long as the article exists. On a transient provider failure the stored URL
    (if any) is returned rather than raising.
    """
    if job.status != JobStatus.SUCCEEDED:
        return job.download_url or ""

    now = time.time() if now is None else now
    fresh_enough = (
        job.download_url
        and job.download_url_expires_at is not None
        and job.download_url_expires_at - now > URL_REFRESH_MARGIN_SECONDS
    )
    if fresh_enough or not (job.project_id and job.export_id):
        return job.download_url or ""

    try:
        with service_factory() as service:
            export = service.get_project_export(job.project_id, job.export_id)
    except VideoGenError:
        logger.warning("Could not refresh download URL for job %s", job.pk)
        return job.download_url or ""

    if export.download_url:
        VideoJob.objects.filter(pk=job.pk).update(
            download_url=export.download_url,
            download_url_expires_at=export.download_url_expires_at,
        )
        job.download_url = export.download_url
        job.download_url_expires_at = export.download_url_expires_at
    return job.download_url or ""
