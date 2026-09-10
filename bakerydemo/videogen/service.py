"""Orchestration for producing one video per article.

The flow is deliberately *poll-driven* and infrastructure-free: there is no task
queue, broker or background worker. Instead:

* ``start_job`` is called from the POST endpoint. It idempotently reserves a
  single job per article and makes the one cheap, fast provider call that starts
  a Script-to-Video run (VideoGen returns ``202`` immediately).
* ``advance_job`` is called from the GET endpoint. Each poll nudges the job's
  state machine forward by reading the provider's own reported state and, when
  generation has succeeded, starting exactly one export and finally downloading
  and storing the finished MP4.

Spend safety is the overriding concern, so *mutating* provider calls (starting a
workflow, starting an export) are made at most once and never retried on
failure - a retry could produce a second video or a second export. Read-only
polls, by contrast, are retried on transient errors.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from .client import VideoGenClient
from .exceptions import (
    VideoGenConnectionError,
    VideoGenError,
    VideoGenRateLimitError,
    VideoGenServerError,
)
from .models import JobStatus, VideoJob
from .narration import build_narration_script

logger = logging.getLogger("bakerydemo.videogen")

# Provider workflow/export terminal states, per the VideoGen docs.
_RUN_SUCCEEDED = "succeeded"
_RUN_FAILED = {"failed", "cancelled"}
_RUN_PENDING = {"pending", "running"}

# Export resolution tier. The VideoGen docs document the ordered quality enum
# LOW < STANDARD < HIGH < MAX (the pixel label "720p" is *not* accepted - the API
# rejects it with 400). The docs give no tier->pixel mapping, so we choose the
# tier that best honours the spend mandate ("at 720p, never 4K, cheaper is
# better"): STANDARD - the default tier, the natural 720p/standard-HD rung, and
# safely below the top MAX (4K) tier.
EXPORT_QUALITY = "STANDARD"


def get_client() -> VideoGenClient:
    """Build a client from settings, which read the credentials from the env."""
    return VideoGenClient(
        api_key=getattr(settings, "VIDEOGEN_API_KEY", "") or "",
        base_url=getattr(settings, "VIDEOGEN_BASE_URL", "") or None,
    )


def _is_retryable(exc: VideoGenError) -> bool:
    """Transient failures that a later poll can recover from."""
    return isinstance(
        exc,
        (VideoGenConnectionError, VideoGenRateLimitError, VideoGenServerError),
    )


# ---------------------------------------------------------------------------
# Start (POST)
# ---------------------------------------------------------------------------


def start_job(page, *, client: VideoGenClient | None = None) -> VideoJob:
    """Idempotently start (or return) the video job for ``page``.

    If a job already exists for the article it is returned untouched - no second
    video is produced and nothing is billed again. Otherwise a new job is
    created and a single Script-to-Video run is started.
    """
    with transaction.atomic():
        job, created = VideoJob.objects.select_for_update().get_or_create(page=page)
    if not created:
        return job

    # We own a brand-new job row: build the narration from the article's own
    # words and start exactly one workflow run.
    script = build_narration_script(page)
    job.touch(script=script, status=JobStatus.PENDING)

    client = client or get_client()
    try:
        result = client.script_to_video(script=script)
    except VideoGenError as exc:
        # A start failure is never retried (spend safety): the row stays FAILED.
        logger.warning("VideoGen start failed for page %s: %s", page.pk, exc)
        job.mark_failed(f"Failed to start video generation: {exc}")
        return job

    workflow_run_id = result.get("workflowRunId")
    if not workflow_run_id:
        job.mark_failed("VideoGen did not return a workflowRunId.")
        return job

    job.touch(
        workflow_run_id=workflow_run_id,
        project_id=result.get("projectId", "") or "",
        status=JobStatus.GENERATING,
        progress_percentage=1,
    )
    logger.info("Started VideoGen run %s for page %s", workflow_run_id, page.pk)
    return job


# ---------------------------------------------------------------------------
# Advance (GET)
# ---------------------------------------------------------------------------


def advance_job(job: VideoJob, *, client: VideoGenClient | None = None) -> VideoJob:
    """Move ``job`` one step further by consulting the provider.

    Terminal jobs are returned unchanged. Transient provider errors leave the
    job active for the next poll; definitive provider errors fail it.
    """
    if not job.is_active:
        return job

    client = client or get_client()
    try:
        if job.status == JobStatus.GENERATING:
            _poll_generation(job, client)
        elif job.status in (JobStatus.EXPORTING, JobStatus.STORING):
            _poll_export(job, client)
    except VideoGenError as exc:
        if _is_retryable(exc):
            logger.warning(
                "Transient VideoGen error for job %s (will retry): %s",
                job.pk,
                exc,
            )
            return job
        logger.warning("VideoGen error failed job %s: %s", job.pk, exc)
        job.mark_failed(str(exc))
    return job


def _poll_generation(job: VideoJob, client: VideoGenClient) -> None:
    data = client.get_workflow_run(job.workflow_run_id)
    status = data.get("status")

    if status in _RUN_PENDING or status is None:
        job.touch(progress_percentage=_generation_progress(data))
        return

    if status in _RUN_FAILED:
        job.mark_failed(_provider_error(data, f"Generation {status}"))
        return

    if status == _RUN_SUCCEEDED:
        project_id = job.project_id or data.get("projectId") or ""
        if not project_id:
            job.mark_failed("VideoGen run succeeded but returned no projectId.")
            return
        if not job.project_id:
            job.touch(project_id=project_id)
        _start_export(job, client)
        return

    job.mark_failed(f"Unexpected VideoGen run status: {status!r}")


def _start_export(job: VideoJob, client: VideoGenClient) -> None:
    """Start exactly one export, guarded by an atomic state transition."""
    # Compare-and-swap: only the caller that flips GENERATING->EXPORTING starts
    # the export. This prevents concurrent polls from starting a second (billed)
    # export.
    claimed = VideoJob.objects.filter(pk=job.pk, status=JobStatus.GENERATING).update(
        status=JobStatus.EXPORTING
    )
    if not claimed:
        job.refresh_from_db()
        return

    job.status = JobStatus.EXPORTING
    try:
        result = client.export_project(job.project_id, quality=EXPORT_QUALITY)
    except VideoGenError as exc:
        # Never retry an export start: a retry risks a second billed export.
        logger.warning("Export start failed for job %s: %s", job.pk, exc)
        job.mark_failed(f"Failed to start export: {exc}")
        return

    export_id = result.get("exportId")
    if not export_id:
        job.mark_failed("VideoGen did not return an exportId.")
        return

    job.touch(export_id=export_id, progress_percentage=70)
    logger.info("Started export %s for job %s", export_id, job.pk)


def _poll_export(job: VideoJob, client: VideoGenClient) -> None:
    if not job.export_id:
        # The owning poll has flipped to EXPORTING but not yet recorded the
        # export id; nothing to poll until it does.
        return

    data = client.get_export(job.project_id, job.export_id)
    status = data.get("status")

    if status in _RUN_PENDING or status is None:
        job.touch(progress_percentage=_export_progress(data))
        return

    if status in _RUN_FAILED:
        job.mark_failed(_provider_error(data, f"Export {status}"))
        return

    if status == _RUN_SUCCEEDED:
        _store_finished_video(job, client, data)
        return

    job.mark_failed(f"Unexpected VideoGen export status: {status!r}")


def _store_finished_video(
    job: VideoJob, client: VideoGenClient, export_data: dict
) -> None:
    """Download the finished MP4 once and store it locally.

    Downloading is not billed, so - unlike the mutating calls - a transient
    download failure releases the job back to EXPORTING to be retried on the
    next poll rather than failing it.
    """
    claimed = VideoJob.objects.filter(pk=job.pk, status=JobStatus.EXPORTING).update(
        status=JobStatus.STORING, progress_percentage=99
    )
    if not claimed:
        job.refresh_from_db()
        return
    job.status = JobStatus.STORING

    download_url = export_data.get("downloadUrl")
    if not download_url:
        # Export reported succeeded without a URL yet: release for a re-poll.
        _release_to_exporting(job)
        return

    try:
        content = client.download_file(download_url)
    except VideoGenError as exc:
        logger.warning("Download failed for job %s (will retry): %s", job.pk, exc)
        _release_to_exporting(job)
        return

    job.video_file.save("video.mp4", ContentFile(content), save=False)
    job.status = JobStatus.READY
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
    logger.info("Stored finished video for job %s", job.pk)


def _release_to_exporting(job: VideoJob) -> None:
    VideoJob.objects.filter(pk=job.pk, status=JobStatus.STORING).update(
        status=JobStatus.EXPORTING
    )
    job.refresh_from_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _generation_progress(data: dict) -> int:
    """Map run progress (0-100) into the 1-69 band of overall progress."""
    raw = data.get("progressPercentage") or 0
    return _clamp(1 + int(raw * 0.68), 1, 69)


def _export_progress(data: dict) -> int:
    """Map export progress (0-100) into the 70-99 band of overall progress."""
    raw = data.get("progressPercentage") or 0
    return _clamp(70 + int(raw * 0.29), 70, 99)


def _provider_error(data: dict, fallback: str) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return f"{fallback}: {message}"
    elif isinstance(error, str) and error:
        return f"{fallback}: {error}"
    return fallback
