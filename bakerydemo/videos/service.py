"""Orchestration for turning an article into a downloadable MP4.

State machine (advanced lazily whenever the status endpoint is polled — no worker,
no task queue):

    PRODUCING ──workflow succeeded──▶ EXPORTING ──export succeeded──▶ READY
        │                                 │
        └── workflow failed/cancelled ────┴── export failed/cancelled ──▶ FAILED

Exactly one billable video (one ``script_to_video``) and one billable export
(``export_project``) are ever produced per job:

  * one job per article (``OneToOneField`` + ``get_or_create``), so a repeat request never
    starts a second workflow;
  * the export is claimed with an atomic compare-and-swap, so concurrent polls cannot
    trigger a second export.
"""

from __future__ import annotations

import logging

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from . import videogen_client as vg
from .exceptions import (
    VideoGenAPIError,
    VideoGenError,
    VideoGenUnavailableError,
)
from .models import VideoJob, VideoJobState
from .narration import build_narration_script

logger = logging.getLogger(__name__)

# VideoGen JobStatus wire values (lowercase), from the contract sheet.
_STATUS_IN_PROGRESS = ("pending", "running")
_STATUS_SUCCEEDED = "succeeded"
_STATUS_TERMINAL_FAILURE = ("failed", "cancelled")


def _clamp(value) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def start_or_get_job(page) -> tuple[VideoJob, bool]:
    """Return the (single) job for ``page``, starting the workflow the first time.

    Idempotent: a second call returns the existing job without producing another video.
    The one exception is a job that failed *before* any workflow was created (nothing was
    billed): that is safe to restart.
    """
    try:
        with transaction.atomic():
            job, created = VideoJob.objects.get_or_create(
                page=page,
                defaults={"script": build_narration_script(page)},
            )
    except IntegrityError:
        # Lost a race to create; the winner's row now exists.
        job, created = VideoJob.objects.get(page=page), False

    if created:
        _start_workflow(job)
    elif job.state == VideoJobState.FAILED and not job.workflow_run_id:
        # Nothing was ever produced/billed for this job — safe to retry the start.
        _start_workflow(job)

    return job, created


def _start_workflow(job: VideoJob) -> None:
    try:
        resp = vg.start_script_to_video(job.script)
    except VideoGenError as exc:
        _fail_from_exception(job, exc)
        return

    job.workflow_run_id = resp.workflow_run_id
    job.project_id = resp.project_id
    job.state = VideoJobState.PRODUCING
    job.progress = 0.0
    job.error_code = ""
    job.error_message = ""
    job.save(
        update_fields=[
            "workflow_run_id",
            "project_id",
            "state",
            "progress",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )


def reconcile(job: VideoJob) -> VideoJob:
    """Advance ``job`` by reading VideoGen's reported state. Safe to call repeatedly.

    A definitive provider outcome (workflow/export ``failed``/``cancelled``, or a 4xx
    rejection) moves the job to FAILED. Transient trouble (5xx, 429, network, unreadable
    body) is left alone so the next poll retries.
    """
    if job.is_terminal:
        return job

    try:
        if job.state == VideoJobState.PRODUCING:
            _reconcile_workflow(job)
        if job.state == VideoJobState.EXPORTING:
            _reconcile_export(job)
    except VideoGenAPIError as exc:
        if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 429:
            _fail_from_exception(job, exc)
        else:
            logger.warning("Transient VideoGen API error for job %s: %s", job.public_id, exc)
    except VideoGenError as exc:
        logger.warning("Transient VideoGen error for job %s: %s", job.public_id, exc)

    return job


def _reconcile_workflow(job: VideoJob) -> None:
    if not job.workflow_run_id:
        return
    run = vg.get_workflow_run(job.workflow_run_id)
    status = str(run.status)

    if status in _STATUS_IN_PROGRESS:
        job.progress = _clamp(run.progress_percentage) * 0.5
        job.save(update_fields=["progress", "updated_at"])
    elif status == _STATUS_SUCCEEDED:
        job.progress = 50.0
        job.save(update_fields=["progress", "updated_at"])
        _start_export(job)
    elif status in _STATUS_TERMINAL_FAILURE:
        code, message = vg.error_model_to_pair(run.error, f"workflow_{status}")
        _fail(job, code, message)
    # An unknown (newer) status is treated as still-in-progress: leave as-is.


def _start_export(job: VideoJob) -> None:
    """Claim and start the single export with a compare-and-swap.

    Only one caller can flip ``export_requested`` from ``False`` to ``True`` in the DB, so
    the (billable) export is requested exactly once even under concurrent polling.
    """
    claimed = (
        VideoJob.objects.filter(
            pk=job.pk, export_requested=False, export_id=""
        ).update(export_requested=True, state=VideoJobState.EXPORTING)
    )
    if not claimed:
        job.refresh_from_db()
        return

    from django.conf import settings

    quality = getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "HIGH")
    resp = vg.start_export(job.project_id, quality)  # may raise; caught by reconcile()
    VideoJob.objects.filter(pk=job.pk).update(export_id=resp.export_id)
    job.refresh_from_db()


def _reconcile_export(job: VideoJob) -> None:
    if not job.export_id:
        # Claimed but the id is not stored yet (or the export call is retrying). Wait.
        return
    export = vg.get_project_export(job.project_id, job.export_id)
    status = str(export.status)

    if status in _STATUS_IN_PROGRESS:
        job.progress = 50.0 + _clamp(export.progress_percentage) * 0.5
        job.save(update_fields=["progress", "updated_at"])
    elif status == _STATUS_SUCCEEDED:
        _finalize(job, export)
    elif status in _STATUS_TERMINAL_FAILURE:
        code, message = vg.error_model_to_pair(export.error, f"export_{status}")
        _fail(job, code, message)


def _finalize(job: VideoJob, export) -> None:
    """Download the finished MP4 once and store it, moving the job to READY.

    A transient download failure (or a not-yet-present URL) leaves the job in EXPORTING so
    the next poll retries — the export itself has already succeeded, so no rework is billed.
    """
    url = export.download_url
    if not url:
        job.progress = 99.0
        job.save(update_fields=["progress", "updated_at"])
        return

    try:
        content = vg.download_bytes(url)
    except VideoGenUnavailableError as exc:
        logger.warning("MP4 download deferred for job %s: %s", job.public_id, exc)
        job.progress = 99.0
        job.save(update_fields=["progress", "updated_at"])
        return

    filename = f"article-{job.page_id}-{job.public_id}.mp4"
    job.mp4.save(filename, ContentFile(content), save=False)
    job.state = VideoJobState.READY
    job.progress = 100.0
    job.error_code = ""
    job.error_message = ""
    job.save(
        update_fields=["mp4", "state", "progress", "error_code", "error_message", "updated_at"]
    )


def _fail(job: VideoJob, code: str, message: str) -> None:
    job.state = VideoJobState.FAILED
    job.error_code = code
    job.error_message = message
    job.save(update_fields=["state", "error_code", "error_message", "updated_at"])


def _fail_from_exception(job: VideoJob, exc: VideoGenError) -> None:
    if isinstance(exc, VideoGenAPIError):
        code = exc.code or f"http_{exc.status_code}"
        message = exc.message or "VideoGen API error"
    else:
        code = "provider_error"
        message = str(exc)
    _fail(job, code, message)
